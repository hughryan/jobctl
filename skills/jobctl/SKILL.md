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
jobctl logs <id> [--tail N] [--follow]                               # read or tail combined stdout+stderr
jobctl stop <id>                                                     # SIGTERM, escalates to SIGKILL after 10s
jobctl wait <id> [--timeout SECONDS] [--poll SECONDS]                # block until terminal state or timeout (default 540s/2s)
jobctl ui                                                            # prints the dashboard URL (http://127.0.0.1:8787)
jobctl --host <ssh-alias> <any of the above>                         # run that command on a remote machine over SSH
```

`jobctl list` deliberately shows only the last 24 hours of jobs plus every job still `running` or `stopping`, so a long-lived daemon's hundreds of finished jobs don't flood the output - a running job is never hidden, however old it is. Widen the window with `--since 7d` (`s`/`m`/`h`/`d`, a bare number meaning seconds) or drop it with `--all`; a footer tells you how many rows were hidden. `jobctl status <id>` still works for any job, hidden or not.

The daemon auto-starts on first use of any `jobctl` command - no setup required. It binds to `127.0.0.1` only. `--cwd` is passed straight to the daemon as structured data (not shell-interpreted), so it's the way to run a job in a specific directory without a `cd &&` shell construct - useful since some harness guards refuse "complex" multi-part commands and worktree-pinned sessions can't always `cd` into an arbitrary path.

Log output already normalizes bare `\r` to line breaks (Python's line-splitting treats `\r` as a boundary), so `tqdm`-style progress bars show as clean successive lines in `jobctl logs` and the dashboard - no manual `tr '\r' '\n'` needed.

## Running jobs on a remote host

`--host <ssh-alias>` is a global flag that goes **before** the subcommand. It re-runs the same command on that host over SSH, so every pattern below works identically against a remote machine (e.g. a GPU box) - same CLI, same exit codes, streamed output:

```bash
jobctl --host cuda submit --name run-1 -- python train.py --config run-1.yaml
jobctl --host cuda logs <id> --follow
while ! jobctl --host cuda wait "$JOB_ID" --timeout 540; do :; done
```

The alias must be defined in `~/.ssh/config`; connection multiplexing is configured there too, not in `jobctl`. Without `--host`, behavior is exactly as documented everywhere else in this file.

Things that differ remotely:

- **The local daemon is never involved.** A `--host` invocation does not start or contact this machine's jobd, so a job meant for the remote box can never silently run here. The remote `jobctl` auto-starts its own daemon on the remote side.
- **`--cwd` is a remote path.** Locally, omitting `--cwd` defaults the job to the current directory. With `--host`, that default is resolved *on the remote host* (this machine's paths don't exist there) - pass `--cwd /remote/path` explicitly whenever the job needs a specific directory.
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

Each iteration is one Bash call that blocks for up to `--timeout` seconds (default 540) and returns exit 0 once the job reaches a terminal state, or exit 1 with `"timed_out": true` if not - loop again in that case. The upper bound on a single call is the harness's own Bash timeout ceiling (`BASH_MAX_TIMEOUT_MS`, 600000ms by default), which is raised in the user's own Claude Code settings rather than by anything here - keep `--timeout` a comfortable buffer under whatever ceiling is configured (e.g. up to ~3540s if the ceiling is raised to an hour) so the wait returns cleanly instead of being hard-killed.

### Sizing `--timeout` - don't reach for the ceiling by default

The point of the raised ceiling is to let a *deliberately* long wait happen in one call, not to make every wait long:

- With no duration estimate yet, start with the 540s default (or shorter) and use what you observe - log growth rate, epoch/step timestamps, a stated ETA in the tool's own output - to size the next call's `--timeout`, rather than jumping straight to the max.
- Once a job has an evidenced steady rate (e.g. "3 epochs in 6 minutes, 47 to go"), it's reasonable to size `--timeout` to most of the remaining estimate in fewer, larger calls.
- Never pass a multi-thousand-second `--timeout` as a first guess for a job with no prior evidence of how long it runs.

### Detecting a stall vs. a slow job

`jobctl wait` only tells you the process is still alive - it does not know whether it's making progress. Before looping into another wait, compare the job's log against what you saw last cycle (e.g. `jobctl logs <id> --tail 5`, or note the line count from `jobctl status`'s log if you're tracking it). If the tail hasn't changed across two or three consecutive wait cycles for a job that normally emits periodic output, treat that as a likely hang rather than continuing to block on it - report it up instead of consuming another long `--timeout` window on a process that may never return. Jobs with no periodic output at all (silent until they finish) don't give you this signal; for those, size `--timeout` conservatively from any external estimate you have (dataset size, known step count) rather than assuming it'll finish.

## Why this exists

Background shells started via Bash's `run_in_background: true` are tracked by the harness itself, and that tracking has been observed to silently reap jobs mid-run with no error surfaced in the process's own output - independent of wall-clock duration, and apparently more likely with several background tasks alive at once. `jobctl` jobs are parented to a persistent local daemon instead of the harness's own bookkeeping, so none of that applies: job count doesn't matter, and a job only stops when it exits, is `stop`'d, or the machine reboots.

## Human monitoring

Run `jobctl ui` and open the printed URL for a live table of all jobs with per-job log viewing and a Stop button - useful for watching parallel runs and killing one manually so nothing is left orphaned.
