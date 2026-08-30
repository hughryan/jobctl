---
name: jobctl
description: Run and monitor long-running or background shell commands (training runs, builds, batch jobs) that must survive beyond a single turn or subagent's lifetime - use instead of Bash's run_in_background whenever a task needs to keep running after the current turn ends, be checked on later, or be stopped early.
---

## What this is

`jobctl` is a local daemon + CLI (invoked as `jobctl` on PATH) for launching arbitrary shell commands that need to run detached from any single Claude Code turn or session. It exists because background shells started via Bash's `run_in_background: true` inside a subagent can be silently killed when that subagent's turn ends (a known Claude Code limitation) - `jobctl` processes are parented to a persistent local daemon instead, so they run to completion regardless of which turn, subagent, or session started them.

Use it whenever a task:
- Will run longer than a couple of minutes (training runs, long builds, batch/data jobs, test suites)
- Needs to be dispatched from a subagent but checked on or collected later
- Should be monitorable or stoppable independent of the agent that started it

## Commands

```bash
jobctl submit [--name NAME] [--cwd DIR] -- <any command and args>   # returns a job id immediately
jobctl list [--all] [--since DURATION]                               # table of jobs + status: last 24h plus every active job
jobctl status <id>                                                   # full JSON: status, pid, exit_code, timestamps
jobctl logs <id> [--tail N] [--follow]                               # read, or tail, combined stdout+stderr; --follow is bounded (see below)
jobctl stop <id>                                                     # SIGTERM, escalates to SIGKILL after 10s
jobctl wait <id> [--timeout SECONDS] [--poll SECONDS]                # block until terminal state or timeout (default 540s/2s, clamped to JOBCTL_MAX_WAIT)
jobctl ui                                                            # prints the dashboard URL (http://127.0.0.1:8787)
jobctl daemon status                                                 # is the daemon healthy? pid, port, uptime, the jobd.py it is running from
jobctl daemon restart                                                # stop + start the daemon; running jobs are NOT affected
jobctl daemon stop                                                   # stop the daemon; running jobs are NOT affected
jobctl --host <ssh-alias> <any of the above>                         # run that command on a remote machine over SSH
```

`jobctl list` deliberately shows only the last 24 hours of jobs plus every job still `running` or `stopping`, so a long-lived daemon's hundreds of finished jobs don't flood the output - a running job is never hidden, however old it is. Widen the window with `--since 7d` (`s`/`m`/`h`/`d`, a bare number meaning seconds) or drop it with `--all`; a footer tells you how many rows were hidden. `jobctl status <id>` still works for any job, hidden or not.

`jobctl logs <id> --follow` prints the last `--tail` lines (default 200) and then streams only new output, like `tail -f` - it does **not** replay the whole log. It ends with exit 0 when the job reaches a terminal state, or exit 1 when the `JOBCTL_MAX_WAIT` blocking ceiling elapses (45 minutes by default; explained under *Sizing `--timeout`* below) with the job still running. `--tail 0` is therefore the resume mode - new output only, nothing you have already seen:

```bash
while ! jobctl logs "$JOB_ID" --follow --tail 0; do :; done
```

Prefer `--tail N` for a bounded look at a running job (`--tail 20`) over `--follow` when you only need a snapshot; following spends the whole window blocked and returns everything the job emits in it.

The daemon auto-starts on first use of any `jobctl` command - no setup required. It binds to `127.0.0.1` only. `--cwd` is passed straight to the daemon as structured data (not shell-interpreted), so it's the way to run a job in a specific directory without a `cd &&` shell construct - useful since some harness guards refuse "complex" multi-part commands and worktree-pinned sessions can't always `cd` into an arbitrary path.

`submit` confirms the command actually started before it returns, so a bad executable or `--cwd` fails the `submit` call itself - non-zero exit, no job id, and the OS error on stderr (only if that confirmation takes over 10 seconds is an id returned unconfirmed). Each job runs under a per-job supervisor that records the exit code from outside the daemon's lifetime, so exit codes survive daemon restarts - a `null` exit_code no longer means "the daemon restarted", only that the supervisor itself was killed without recording.

`jobctl daemon status` reports on the daemon itself and never starts one, so it is safe to run just to look. Its point is the `source:` line: a daemon whose `jobd.py` has been moved or deleted keeps serving the API from code in memory while the dashboard silently 404s, and that is otherwise invisible. `jobctl daemon restart` fixes it. Restarting does **not** stop running jobs - they run in their own sessions under their own supervisors, which record exit codes regardless of the daemon - so it is not something to avoid while work is in flight.

Log output already normalizes bare `\r` to line breaks (Python's line-splitting treats `\r` as a boundary), so `tqdm`-style progress bars show as clean successive lines in `jobctl logs` and the dashboard - no manual `tr '\r' '\n'` needed.

## Running jobs on a remote host

`--host <ssh-alias>` is a global flag that goes **before** the subcommand. It re-runs the same command on that host over SSH, so every pattern below works identically against a remote machine (e.g. a GPU box) - same CLI, same exit codes, streamed output:

```bash
jobctl --host cuda submit --name run-1 -- python train.py --config run-1.yaml
jobctl --host cuda logs <id> --follow --tail 20
while ! jobctl --host cuda wait "$JOB_ID" --timeout 540; do :; done
```

The alias must be defined in `~/.ssh/config`; connection multiplexing is configured there too, not in `jobctl`. Without `--host`, behavior is exactly as documented everywhere else in this file.

Things that differ remotely:

- **The local daemon is never involved.** A `--host` invocation does not start or contact this machine's jobd, so a job meant for the remote box can never silently run here. The remote `jobctl` auto-starts its own daemon on the remote side.
- **`--cwd` is a remote path.** Locally, omitting `--cwd` defaults the job to the current directory. With `--host`, that default is resolved *on the remote host* (this machine's paths don't exist there) - pass `--cwd /remote/path` explicitly whenever the job needs a specific directory.
- **The blocking ceiling still applies.** `wait` and `logs --follow` block on the remote side, and a locally set `JOBCTL_MAX_WAIT` is forwarded over the hop - so the bound is the same one you get locally, not the remote machine's default.
- **Logs, artifacts, and job ids live on the remote host.** Job ids from `--host cuda submit` are only meaningful to `--host cuda`, and anything the job writes to disk stays on that machine - fetch it with `scp`/`rsync` if you need it locally.
- **`jobctl --host <alias> ui`** prints the remote dashboard URL plus the `ssh -N -L 8788:127.0.0.1:8787 <alias>` command a human runs to reach it from this machine's browser (local port 8788 so it doesn't collide with this machine's own dashboard on 8787).

If `ssh` itself fails (host unreachable, auth failure), `jobctl` reports the alias that failed and exits non-zero - it never falls back to running locally.

## Pattern for subagents: fire-and-forget

A subagent dispatched only to kick off a long job should do exactly this and then return - never `run_in_background` the actual work:

```bash
jobctl submit --name run-N -- python train.py --config run-N.yaml
```

Report the returned job id back to the caller (typically the lead) and end the turn. The lead - or another subagent later - checks progress independently with `jobctl status <id>` / `jobctl logs <id>` / the dashboard, and dispatches follow-on work (e.g. an eval subagent) once `jobctl status` shows `"status": "exited"`.

## Pattern for subagents: waiting on a job

If a subagent's job explicitly includes waiting for completion (e.g. "wait for training to finish, then evaluate"), do **not** poll in a loop that re-invokes a tool call every ~30-60s - that burns a call per tick while wall time barely passes, and it does not out-wait the harness's own reaping of unmanaged background work. Instead use `jobctl wait`, which blocks inside a single foreground Bash call and returns cleanly on either outcome:

```bash
while ! jobctl wait "$JOB_ID" --timeout 540; do :; done
```

Each iteration is one Bash call that blocks for up to `--timeout` seconds (default 540) and returns exit 0 once the job reaches a terminal state, or exit 1 with `"timed_out": true` if not - loop again in that case.

### Sizing `--timeout` - the cache window, not the harness ceiling

The binding constraint on a single blocking call is not the harness's Bash timeout ceiling. It is your own prompt cache: the cached context has a 60-minute TTL that is refreshed on every API request the session makes, and while you sit blocked inside one foreground Bash call you make none. So what has to stay under 60 minutes is the *request-to-request* interval - block duration, plus the tool return, plus the model turn that follows. Exceed it and the session's whole context (often 250k+ tokens) is re-read cold, which costs far more than the loop iteration you were avoiding.

You cannot get this wrong by accident: `jobctl` enforces the bound rather than trusting advice. Any `--timeout` above `JOBCTL_MAX_WAIT` (default 2700s = 45 minutes, the remainder being margin for the turn) is clamped to it, with a note on stderr; `logs --follow` is bounded the same way. Exit codes are untouched, so a clamped wait that expires is an ordinary timeout and the loop above simply runs another iteration. Do not raise `JOBCTL_MAX_WAIT` to get a longer block - it is the user's setting, sized against the user's cache.

Looping is therefore the intended shape, not a workaround: waking at least every 45 minutes is what keeps the cache warm, and each wake-up is a free checkpoint (see below). Within that bound, still size `--timeout` from evidence rather than guessing:

- With no duration estimate yet, start with the 540s default (or shorter) and use what you observe - log growth rate, epoch/step timestamps, a stated ETA in the tool's own output - to size the next call's `--timeout`, rather than jumping straight to the bound.
- Once a job has an evidenced steady rate (e.g. "3 epochs in 6 minutes, 47 to go"), it's reasonable to size `--timeout` to most of the remaining estimate in fewer, larger calls.
- Never pass a multi-thousand-second `--timeout` as a first guess for a job with no prior evidence of how long it runs.

### Detecting a stall vs. a slow job

Every forced wake-up - a clamped wait expiring, or a `--follow` window elapsing - is a natural checkpoint; use it. `jobctl wait` only tells you the process is still alive - it does not know whether it's making progress. Before looping into another wait, compare the job's log against what you saw last cycle (e.g. `jobctl logs <id> --tail 5`, or note the line count from `jobctl status`'s log if you're tracking it). If the tail hasn't changed across two or three consecutive wait cycles for a job that normally emits periodic output, treat that as a likely hang rather than continuing to block on it - report it up instead of consuming another long `--timeout` window on a process that may never return. Jobs with no periodic output at all (silent until they finish) don't give you this signal; for those, size `--timeout` conservatively from any external estimate you have (dataset size, known step count) rather than assuming it'll finish.

## Why this exists

Background shells started via Bash's `run_in_background: true` are tracked by the harness itself, and that tracking has been observed to silently reap jobs mid-run with no error surfaced in the process's own output - independent of wall-clock duration, and apparently more likely with several background tasks alive at once. `jobctl` jobs are parented to a persistent local daemon instead of the harness's own bookkeeping, so none of that applies: job count doesn't matter, and a job only stops when it exits, is `stop`'d, or the machine reboots.

## Human monitoring

Run `jobctl ui` and open the printed URL for a live table of all jobs with per-job log viewing and a Stop button - useful for watching parallel runs and killing one manually so nothing is left orphaned.
