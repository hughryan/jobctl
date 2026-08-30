# jobctl

A tiny daemon and CLI for shell commands that need to outlive the thing that started them.

`jobctl` launches a long-running command — a training run, a big build, a batch job, a slow test
suite — detaches it from the caller entirely, and gives you a stable handle to check on it, tail its
logs, wait for it, or stop it later. Jobs are parented to a small persistent local daemon, not to
your terminal, your SSH session, or the tool call that submitted them.

It is useful for any long-running shell work, but the case that motivated it is **AI coding agents**:
harnesses like Claude Code run shell commands per conversational turn, and work that outlives a turn
needs somewhere durable to live.

Python 3 standard library only. No dependencies, no install step beyond a symlink, no configuration.

```bash
$ jobctl submit --name train -- python train.py --config run-1.yaml
train-9f3c1a02

$ jobctl list
ID              STATUS   RUNTIME  CMD
train-9f3c1a02  running  4m12s    python train.py --config run-1.yaml

$ jobctl logs train-9f3c1a02 --tail 3
epoch 12/50  loss 0.4417
epoch 13/50  loss 0.4310
epoch 14/50  loss 0.4229

$ jobctl wait train-9f3c1a02 --timeout 540
```

---

## Why this exists

### Long work does not fit inside a turn

An AI coding agent executes shell commands one turn at a time. A turn is short — seconds to a couple
of minutes. Real work often is not. A fine-tuning run takes hours. A full build takes twenty minutes.
A dataset job takes as long as it takes. The moment a command outlives the turn that issued it, you
need an answer to the question: *what owns this process now?*

Harnesses offer an obvious-looking answer — a "background" mode for shell commands, such as Claude
Code's `run_in_background: true`. The command returns immediately, the harness keeps a handle, and
you poll it later. That is the right shape, but in practice it has a failure mode serious enough to
be worth building around.

### Harness-tracked background shells get silently reaped

Background shells tracked by the harness itself have been observed to be **killed mid-run, with no
error surfaced anywhere in the process's own output**. The log simply stops. The job did not crash,
did not exit non-zero, did not write a traceback — it was reaped by the bookkeeping layer above it.

Three things make this worse than an ordinary flaky-infrastructure problem:

1. **It is silent.** There is no exception to catch and no exit code to check. You discover it later,
   when you go looking for a result that never arrived — often after wasting hours of wall-clock time
   waiting on a process that stopped existing early on.
2. **It is not simply a function of duration.** It has been seen independent of how long the job had
   been running, and appears more likely when several background tasks are alive at once. So "keep
   jobs short" is not a workaround, and parallelism — exactly what you want when running a sweep of
   experiments — makes it more likely, not less.
3. **It is worse inside subagents.** A common and useful pattern is to dispatch a subagent whose only
   purpose is to kick off a long job. But the subagent's lifetime is *shorter* than the work it
   started, and when the subagent's turn ends, background work it owns can go with it. The very
   structure that makes delegation useful is the structure that guarantees the owner disappears first.

Put together: anything whose lifetime is tied to a turn, a session, or a subagent is the wrong owner
for a long-running job.

### Polling is expensive in the wrong currency

There is a second, quieter problem. Once you have a detached job, an agent naturally checks on it by
re-invoking a tool call every 30–60 seconds. Each check costs a full model round trip — tokens,
latency, a slot in the context window — while advancing wall-clock time by under a minute. Waiting
out a two-hour training run that way costs on the order of a hundred tool calls that collectively
learn one bit of information: "still running."

The fix is to make waiting *block* rather than *poll*: one call that sits in the daemon until the job
actually reaches a terminal state, and returns then. That is `jobctl wait`.

### The design consequence

Both problems point at the same requirement. Jobs must be parented to something that outlives every
turn, every session, and every subagent — a process whose only job is to be there later. That is what
`jobd` is. Once a job belongs to the daemon, none of the above applies: the number of concurrent jobs
does not matter, subagent lifetimes do not matter, and a job stops when it exits, when you stop it,
or when the machine reboots — and for no other reason.

---

## What it is

Two files and a web page:

| File | Role |
| --- | --- |
| `jobd.py` | The daemon. Owns running jobs, tracks their state, serves a small JSON HTTP API. |
| `jobctl` | The CLI. Talks to the daemon over HTTP, or to a remote machine over SSH. |
| `static/index.html` | A single-page dashboard served by the daemon. |

Everything below follows from a handful of deliberate choices.

**Zero dependencies.** Both programs import only the Python 3 standard library — `http.server`,
`subprocess`, `json`, `threading`, `urllib`. There is nothing to `pip install`, no virtualenv to
activate, and nothing that can break when an unrelated environment changes. On a machine that has
Python 3, `jobctl` works.

**The daemon auto-starts.** You never start it manually. Any `jobctl` command first pings
`/api/health`; if nothing answers, it spawns `jobd.py` detached, waits up to five seconds for it to
come up, and proceeds. First use is indistinguishable from every subsequent use.

**Local only.** `jobd` binds `127.0.0.1` and nothing else. It is a personal job runner for one user
on one machine, not a scheduler, not a queue, and explicitly not something to expose to a network.
Remote execution is handled by SSH (see below), not by opening the port.

**Every job runs under a supervisor.** The daemon does not spawn your command directly. It spawns
a tiny supervisor process (`jobd.py --supervise <id>`, started in its own session so signals aimed
at the daemon never reach it), and the supervisor spawns the command inside that same process
group, waits on it, and records the exit code to disk the moment it exits. The supervisor lives
exactly as long as the job — so the exit code is recorded by something guaranteed to still be
there when the job ends, whatever has happened to the daemon in between. Restarting or upgrading
the daemon while jobs are running is therefore lossless: the new daemon picks the recorded results
up off disk. In job metadata, `pid` is the supervisor — the process-group leader that signals and
liveness checks are aimed at — and `job_pid` is the command itself. Stopping a job `killpg`s that
group: `SIGTERM` first (which the supervisor ignores and the job does not, so the real exit code —
typically `-15` — still gets recorded), escalating to `SIGKILL` after a 10 second grace period, so
a job that spawns children does not leave orphans behind.

**A returned job id proves the command started.** The supervisor stands between the daemon and your
command, so the daemon's own spawn succeeding no longer proves anything — it only proves that Python
started. Letting `submit` return on that would mean `jobctl submit -- definitely-not-a-command`
printing a job id for a job that was already dead, the kind of failure you discover an hour later.
So the daemon and the supervisor share a pipe: the daemon keeps the read end, the supervisor gets
the write end, and the supervisor either closes it (the command is running) or writes the exec error
to it (the command never started). Closure means started, contents are the error — which is why it
is a pipe and not a poll: there is no interval to guess at, nothing to retry, and the answer arrives
the instant it exists rather than one sleep later. A bad executable or a missing `--cwd` directory
therefore fails the `submit` call itself — non-zero exit, no job id, and the OS error printed as a
plain `error:` line — exactly as it did before there was a supervisor. The job is recorded terminal (`exit_code`
127, the shell's "command not found" convention, with the reason on the first line of its log)
*before* the error travels back, so a command that never ran can never sit in the list as `running`.
The one loose end is a confirmation that takes more than ten seconds: `submit` then returns the id
unconfirmed rather than throwing away a job that is very probably running.

**State lives on disk.** Each job gets a directory under `~/.jobctl/jobs/<id>/` containing
`meta.json` (id, name, command, cwd, pid, status, timestamps, exit code), `supervisor.json` (the
supervisor's record: the command's real pid, then its exit code once it ends), and `log` (combined
stdout and stderr, unbuffered). Each file has exactly one writer — the daemon owns `meta.json`, the
supervisor owns `supervisor.json` and the log — so no two processes ever race over the same file.
The daemon is not the source of truth — the filesystem is. Restart the daemon, reboot into it,
attach from a fresh shell: the history is still there. A background watcher thread reconciles every
job every two seconds, folding the supervisor's record into `meta.json` and comparing recorded
state against actual process liveness, so a job that ended while the daemon was down is still
marked `exited` — with its real exit code — rather than sitting in a permanent, wrong `running`
state.

**`wait` blocks server-side.** `jobctl wait <id>` sits in a single foreground process until the job
reaches a terminal state or the timeout elapses. Exit `0` means finished (the full job JSON is
printed); exit `1` with `"timed_out": true` means still running. That makes the caller's loop trivial
and cheap:

```bash
while ! jobctl wait "$JOB_ID" --timeout 540; do :; done
```

The 540-second default is deliberately just under the 600-second ceiling that a typical harness puts
on a single shell call, so the wait returns on its own terms rather than being hard-killed from
above. A longer `--timeout` is clamped to `JOBCTL_MAX_WAIT` (45 minutes by default) for a reason
that has nothing to do with the job — see [`JOBCTL_MAX_WAIT`](#jobctl_max_wait).

**Remote hosts are first-class.** A leading `--host <ssh-alias>` re-runs *any* subcommand on that
machine over SSH — same CLI, same flags, same exit codes, output streamed live. Importantly, a
`--host` invocation **never starts or contacts the local daemon**. That check happens before
`ensure_daemon()`, and an empty alias is rejected rather than ignored, so `--host "$UNSET_VAR"` fails
loudly instead of quietly running your GPU job on your laptop. Silent wrong-machine execution is
precisely what the flag exists to prevent.

**A dashboard for humans.** `jobctl ui` prints `http://127.0.0.1:8787`. Open it for a live table of
recent jobs with status, runtime, command, working directory, a click-to-expand log viewer, and a
Stop button. It refreshes every two seconds. Like `jobctl list`, it shows the last 24 hours plus
everything still active, with a **Show all** toggle for the full history. Useful when several runs
are in flight and you want to see all of them at a glance, or kill one by hand.

**Progress bars render correctly.** Tools like `tqdm` redraw a single line using bare carriage
returns, which turns a naive log capture into one enormous unreadable line. `jobctl` splits on `\r`
as well as `\n`, so a progress bar reads as clean successive lines in both `jobctl logs` and the
dashboard. No `tr '\r' '\n'` incantation needed.

### The HTTP API

The CLI is a thin client over this; use it directly if you want to build something else on top.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness probe — `{"ok": true}`. |
| `GET` | `/api/jobs` | All jobs, newest first, reconciled. |
| `POST` | `/api/jobs` | Submit. Body: `{"name", "cmd": [...], "cwd", "env": {...}}`. Returns `{"id"}`. |
| `GET` | `/api/jobs/<id>` | One job's full metadata. |
| `GET` | `/api/jobs/<id>/log?tail=N` | Last `N` lines of combined output (default 200). |
| `POST` | `/api/jobs/<id>/stop` | `SIGTERM` the process group, `SIGKILL` after 10s. |

Note that `cmd` is a **list**, not a string. It is passed to `subprocess.Popen` as structured data
and never handed to a shell, so there is no quoting or word-splitting layer to get wrong.

---

## How to use it

### Requirements

- Python 3 (any reasonably modern 3.x — standard library only)
- A POSIX operating system: Linux or macOS

There is nothing platform-specific in either file — no macOS-only APIs, no Linux-only ones. It relies
only on POSIX process semantics (`setsid`, process groups, `kill`), so it behaves the same on both.

### Install

Clone the repo somewhere permanent, then symlink the CLI onto your `PATH`:

```bash
git clone https://github.com/hughryan/jobctl.git ~/src/jobctl
ln -sf ~/src/jobctl/jobctl ~/.local/bin/jobctl
```

Or, from inside the clone:

```bash
ln -sf "$PWD/jobctl" ~/.local/bin/jobctl
```

Make sure `~/.local/bin` is on your `PATH`. That is the whole installation. `jobctl` resolves its own
symlink to find `jobd.py` next to it, so the clone can live anywhere as long as you do not move
`jobd.py` or `static/` out of it.

Verify:

```bash
jobctl list      # starts the daemon on first run
# (no jobs)
```

The daemon writes its bookkeeping to `~/.jobctl/`: `daemon.port`, `daemon.pid`, `daemon.log`, and the
per-job directories under `jobs/`. If the daemon ever fails to come up, `daemon.log` is where the
traceback will be.

To use a different port, set `JOBCTL_PORT` for both the daemon and the CLI (the CLI reads the port
the running daemon recorded in `~/.jobctl/daemon.port`, so in practice you set it once and restart
the daemon).

### `JOBCTL_STATE_DIR`

`JOBCTL_STATE_DIR` moves that bookkeeping directory somewhere else. Point it at a scratch path and
you get a completely independent daemon — its own `daemon.port`, `daemon.pid`, `daemon.log` and
`jobs/` — whose jobs are invisible to your usual `jobctl`, and which cannot see your usual jobs
either. That is what you want for testing a change to `jobctl` itself, or for keeping one project's
jobs off the main list:

```bash
JOBCTL_STATE_DIR=/tmp/jobctl-scratch JOBCTL_PORT=8799 jobctl list
```

Setting it on the CLI is enough: the daemon the CLI starts inherits the environment. It is not
forwarded over `--host`, since a local directory path means nothing on another machine.

`JOBCTL_PORT` alone is not isolation. It changes which port gets recorded in `daemon.port`, not
which file gets written, so a second daemon started that way would quietly redirect the CLI your
real jobs are running under, and leave it pointing at a dead port once you killed it. Two things
now prevent that: a daemon refuses to start if another live daemon already owns its state directory
(the recorded pid must be alive *and* the recorded port must answer a health check, so a stale pid
file does not lock you out), and it binds its port before writing the port file, so a start that
fails on an in-use port leaves the working daemon's state untouched.

### `JOBCTL_MAX_WAIT`

`JOBCTL_MAX_WAIT` caps how long any single `jobctl` invocation blocks — both `jobctl wait` and
`jobctl logs --follow`. It defaults to 2700 seconds (45 minutes) and takes the same duration syntax
as `--since`, so `JOBCTL_MAX_WAIT=45m` and `JOBCTL_MAX_WAIT=2700` are the same thing. A malformed
value warns on stderr and falls back to the default rather than failing the command.

That number is not about anything inside `jobctl`. It is sized against the *caller's* prompt cache:
an AI coding agent's cached context has a 60-minute TTL that is refreshed on each API request the
session makes, and it makes none while blocked inside a single tool call. Block for longer than the
TTL and the session's entire context — often hundreds of thousands of tokens — is re-read cold. 45
minutes leaves margin for the tool return and the model turn that follow the block.

A `--timeout` larger than the cap is clamped, with a note on stderr so the JSON on stdout stays
parseable. Exit codes are unaffected: a clamped wait that expires is an ordinary timeout, so the
documented `while ! jobctl wait "$JOB_ID"; do :; done` loop simply wakes up and re-blocks more
often — which is the point. Raise the value if you
want longer blocks; nothing here imposes an upper bound. With `--host`, a locally set value is
forwarded to the remote `jobctl`, which would otherwise silently apply its own default.

### Commands

| Command | What it does |
| --- | --- |
| `jobctl submit [--name NAME] [--cwd DIR] [--env KEY=VALUE]... -- <cmd> [args...]` | Launch a detached job; prints its id immediately and returns. `--env` is repeatable and sets one variable in the job's environment on top of the daemon's own (last wins on a repeated key). |
| `jobctl list [--all] [--since DURATION]` | Table of jobs — id, status, runtime, command. Shows the last 24 hours plus every still-active job; `--since 7d` widens the window (`s`/`m`/`h`/`d`, bare number = seconds), `--all` drops it. |
| `jobctl status <id>` | Full JSON for one job: status, pid, exit code, timestamps, cwd, command. |
| `jobctl logs <id> [--tail N] [--follow]` | Print combined stdout+stderr. `--tail` defaults to 200; `--follow` prints those last lines, then streams new output until the job ends (exit 0) or `JOBCTL_MAX_WAIT` elapses (exit 1 — resume with `--follow --tail 0`). |
| `jobctl stop <id>` | `SIGTERM` the job's process group, escalating to `SIGKILL` after 10 seconds. |
| `jobctl wait <id> [--timeout SECONDS] [--poll SECONDS]` | Block until terminal state or timeout. Defaults: 540s timeout, 2s poll; a longer `--timeout` is clamped to `JOBCTL_MAX_WAIT` (45 minutes). Exit 0 = done, exit 1 = timed out. |
| `jobctl ui` | Print the dashboard URL. |
| `jobctl daemon status` | Report on the daemon itself: pid, port, state directory, uptime, the `jobd.py` it is running from, and how many jobs are active. Exits 0 whether or not one is running. |
| `jobctl daemon restart` | Stop the daemon and start a fresh one. Running jobs are not affected; it says which ones are alive before it does anything. |
| `jobctl daemon stop` | Stop the daemon. Jobs keep running. Stopping an already-stopped daemon is a success. |
| `jobctl --host <ssh-alias> <any of the above>` | Run that command on a remote machine over SSH. |

Job statuses are `running`, `stopping`, `exited` (the process finished on its own — check
`exit_code`), and `stopped` (it was terminated by `jobctl stop`).

The `--` before the command is required. Everything after it is the command and its arguments, taken
verbatim; everything before it belongs to `jobctl`. This is what lets a job take flags of its own
without `jobctl` trying to interpret them.

### A worked example

Submit a job. It returns instantly with an id — the shell is free again immediately:

```bash
$ jobctl submit --name build -- make -j8 all
build-1c4de8a7
```

See what is running:

```bash
$ jobctl list
ID              STATUS   RUNTIME  CMD
build-1c4de8a7  running  38s      make -j8 all
train-9f3c1a02  exited   1h12m    python train.py --config run-1.yaml

121 older jobs hidden — use --all or --since 7d
```

`list` shows the last 24 hours plus everything still active, because a daemon that has been up
for weeks accumulates hundreds of finished jobs and printing all of them buries the few that
matter. A running job is never hidden, however old it is. Widen the window with `--since 7d`, or
drop it entirely with `--all`.

Look at recent output, or follow it live:

```bash
$ jobctl logs build-1c4de8a7 --tail 20
$ jobctl logs build-1c4de8a7 --follow      # last 200 lines, then streams until the job ends
$ jobctl logs build-1c4de8a7 --follow --tail 0   # new output only — the way to resume a follow
```

Wait for it, then act on the result:

```bash
$ jobctl wait build-1c4de8a7 --timeout 300
{
  "id": "build-1c4de8a7",
  "name": "build",
  "cmd": ["make", "-j8", "all"],
  "cwd": "/path/to/project",
  "status": "exited",
  "pid": 48213,
  "started_at": 1785000000.0,
  "ended_at": 1785000241.7,
  "exit_code": 0
}
```

For a job that may outlast a single wait window, loop — each iteration is one cheap blocking call:

```bash
while ! jobctl wait "$JOB_ID" --timeout 540; do :; done
jobctl status "$JOB_ID"
```

Change your mind:

```bash
$ jobctl stop build-1c4de8a7
```

Run a job in a specific directory without a `cd &&` construct — `--cwd` is passed to the daemon as
structured data, not shell-interpreted:

```bash
$ jobctl submit --name test --cwd /path/to/project -- pytest -x tests/
```

Set variables in the job's environment with `--env KEY=VALUE`, repeated once per variable. The job
inherits the daemon's environment and these are applied on top of it. The value is split on the
first `=` only, so a value may itself contain `=`, and a repeated key takes its last value:

```bash
$ jobctl submit --name train --env CUDA_VISIBLE_DEVICES=1 --env WANDB_MODE=offline -- python train.py
```

Nothing about any of this is tied to a particular language or tool. `jobctl submit -- <anything>`
works for any command you would otherwise run in a terminal.

### Log buffering, and why logs used to look empty

A job's stdout is a file, not a terminal. Programs check that: seeing a pipe or a file rather than a
tty, most switch from line buffering to 4KB block buffering, and their output sits in their own
memory until a block fills or they exit. The visible symptom was a healthy twelve-hour training run
whose `jobctl logs` showed 49 bytes after ninety minutes — nothing wrong with the job, and no way to
watch it.

`jobctl` now sets `PYTHONUNBUFFERED=1` for every job, so Python programs write straight through and
`jobctl logs` and `--follow` are usable on a running job from the first line. Opt out by setting the
variable to empty — Python honours only a non-empty value, so this restores the default buffering:

```bash
$ jobctl submit --env PYTHONUNBUFFERED= -- python train.py
```

That fixes Python and only Python. The buffer lives inside the job's own C library, so nothing
outside the process can flush it: a block-buffering program in any other language needs its own
answer, either a flag of its own or an external nudge.

```bash
$ jobctl submit -- stdbuf -oL -eL ./my-program     # force line buffering on a program that has no flag
```

### Running jobs on a remote host

`--host <ssh-alias>` is a global flag that goes **before** the subcommand. Every pattern above works
identically against a remote machine:

```bash
jobctl --host gpu-box submit --name run-1 --cwd /srv/experiments -- python train.py --config run-1.yaml
jobctl --host gpu-box list
jobctl --host gpu-box logs run-1-3ab29ff4 --follow
while ! jobctl --host gpu-box wait "$JOB_ID" --timeout 540; do :; done
```

Requirements and behavior:

- **The alias must be defined in `~/.ssh/config`.** `jobctl` does not manage connections, keys, ports,
  or usernames — it shells out to `ssh <alias>` and lets your SSH configuration do its job.
- **Enable connection multiplexing.** This matters more than it might seem. A monitoring loop issues
  many short-lived SSH invocations, and paying a full TCP + key exchange handshake for each one is
  slow enough to be noticeable. Add to `~/.ssh/config`:

  ```
  Host gpu-box
      HostName gpu-box.example.com
      User <your-user>
      ControlMaster auto
      ControlPath ~/.ssh/cm-%r@%h:%p
      ControlPersist 10m
  ```

  The first connection sets up a master; subsequent ones reuse it and return near-instantly.
- **The remote machine needs `jobctl` installed** at `~/.local/bin/jobctl`. The SSH command explicitly
  prepends `$HOME/.local/bin` to `PATH`, because a non-interactive `ssh host cmd` does not get your
  login shell's environment. Install it on the remote the same way you did locally.
- **The local daemon is never involved.** A `--host` invocation does not start or contact this
  machine's `jobd`. The remote `jobctl` auto-starts its own daemon over there.
- **`--cwd` is a *remote* path.** Locally, omitting `--cwd` defaults to your current directory. With
  `--host`, the default is resolved on the remote host — where your local paths do not exist. Pass
  `--cwd` explicitly whenever the job needs a particular directory.
- **Job ids, logs, and artifacts stay on the remote host.** An id from `--host gpu-box submit` is only
  meaningful to `--host gpu-box`. Anything the job writes to disk lives on that machine; fetch it with
  `scp` or `rsync`.
- **SSH failures are loud.** If the host is unreachable or authentication fails, `jobctl` reports the
  alias that failed and exits non-zero. It never falls back to running locally.

### The dashboard

```bash
$ jobctl ui
http://127.0.0.1:8787
```

Open it for a live table of every job — status, runtime, command, working directory — with
click-to-expand log viewing and a Stop button per running job. It polls every two seconds and adapts
to your system light/dark preference.

For a remote instance, `jobctl --host <alias> ui` prints the remote URL plus the exact tunnel command
to reach it from your own browser:

```bash
$ jobctl --host gpu-box ui
http://127.0.0.1:8787
ssh -N -L 8788:127.0.0.1:8787 gpu-box   # then open http://127.0.0.1:8788
```

The local end of the tunnel is 8788 rather than 8787 so it can coexist with your own machine's
dashboard — you can watch both at once in two tabs.

### Inspecting and restarting the daemon

The daemon is meant to be invisible — it starts itself and then runs for weeks. That is usually the
right amount of attention to pay it, but it means the one failure mode it does have is silent.

A running daemon serves its API from code the interpreter loaded at start-up. If you move or delete
the `jobd.py` it was started from, the daemon does not notice: the API keeps working, jobs keep
running, and nothing anywhere reports a problem. But `jobd.py` resolves `static/` against its own
`__file__` at import, so the dashboard starts returning 404 and stays that way until the daemon is
restarted. Nothing in the tool used to be able to tell you that had happened.

`jobctl daemon status` is the answer to "is this daemon actually fine?":

```
$ jobctl daemon status
daemon:  running (pid 48370)
port:    8787
state:   /Users/you/.jobctl
uptime:  28h33m
source:  /Users/you/src/old-location/jobd.py  ** MISSING **
         this file no longer exists. The daemon is still serving its API from
         code held in memory, but its STATIC_DIR was resolved against that path
         at import, so the dashboard will 404 until you run `jobctl daemon restart`.
jobs:    2 running
```

The source path is read from the daemon's own command line via `ps`, not asked over the API, so it
works against a daemon of any age — including one started long before this command existed, which is
exactly the daemon you need it for. If no daemon is running, `daemon status` says so and still exits
0; it never starts one just to report on it.

`jobctl daemon restart` stops the daemon and starts a new one. **Jobs are not affected.** Each job
runs under its own supervisor in its own session, so no signal from the restart reaches it, and the
supervisor records the exit code whether or not a daemon happens to be alive when the job finishes.
The command says what is running before it touches anything:

```
$ jobctl daemon restart
2 jobs active. A restart does not stop running jobs - each runs in its own session, so no signal from this command reaches one:
  smart-hunt-v2-8f1a9e57 running  job pid (none recorded)
  nightly-build-3c02aa17 running  job pid 51188

1 of those has no job pid recorded. If it was submitted seconds ago that is momentary - the daemon
folds in the supervisor's record on its next reconciliation. If it is older, the job predates the
per-job supervisor: nothing is watching it exit, so it will record exit_code: null whenever it
finishes, restart or no restart.

stopping daemon pid 48370 (port 8787)
started daemon pid 62104 (port 8787)
```

That warning is informational — nothing blocks and nothing prompts. It exists because a job old
enough to predate the per-job supervisor loses its exit code no matter what you do, and a restart is
the moment you are most likely to blame for it.

The restart waits for the old daemon to be fully gone — process exited *and* port no longer
answering — before starting the new one, because a daemon refuses to start over a state directory
another live daemon still owns. `jobctl daemon stop` is the same stop without the start, for when you
want the jobs to carry on unattended.

### Optional: using it from an AI coding agent

This repo ships a [Claude Code](https://claude.com/claude-code) skill at `skills/jobctl/SKILL.md`
that teaches an agent when to reach for `jobctl` instead of a harness background shell, and how to
wait on jobs without burning tool calls. Install it by symlinking the directory:

```bash
ln -sfn ~/src/jobctl/skills/jobctl ~/.claude/skills/jobctl
```

The agent will then load it automatically when a task involves long-running or background work. The
skill also documents the dispatch patterns that motivated the tool — a subagent that submits a job
and returns immediately with the id, and a lead session that checks on it later.

None of this is required. `jobctl` is a standalone command-line tool and is entirely useful without
any agent involved; the skill is just how you hand that usefulness to one.

---

## Design notes and limitations

- **One user, one machine.** `jobd` binds `127.0.0.1`, has no authentication, and assumes the only
  client is you. Do not expose the port. Multi-machine usage goes through SSH via `--host`, which
  keeps authentication where it belongs.
- **Jobs do not survive a reboot.** The daemon is not a supervisor and will not restart jobs. On-disk
  state survives — you will see the historical record — but a running job ends when the machine goes
  down.
- **Exit codes survive daemon restarts.** Each job's supervisor records the exit code from outside
  the daemon's lifetime, so restarting — or crashing — the daemon while jobs run loses nothing: the
  job keeps running, and the restarted daemon reads the recorded code off disk. The one way
  `exit_code` can still be `null` is a supervisor that died without recording — its own crash, or a
  `SIGKILL` from something other than `jobctl stop` (stop's own escalation is recorded as `-9`,
  since the daemon that sent it knows exactly how the job died).
- **Jobs are unbounded.** There is no queue, no concurrency limit, and no scheduling. Submit ten jobs
  and ten jobs start. That is intentional — it is a job *runner*, not a job *scheduler*.
- **Old jobs accumulate.** Nothing prunes `~/.jobctl/jobs/`. Delete directories under it whenever the
  history gets long; the daemon will not miss them.

## License

MIT. See [LICENSE](LICENSE).
