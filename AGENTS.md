# jobctl — AI agent guide

This file tells an AI coding agent (Claude Code, Codex, Cursor, etc.) about this repository
and how to assist with changes to it. `README.md` explains what `jobctl` is and why it
exists — read it first. This file covers what you need to know before changing it.

## Repo structure

```
jobctl                  # CLI — talks to the daemon over HTTP, or to a remote host over SSH
jobd.py                 # daemon — owns jobs, tracks state, serves the JSON API + dashboard
static/index.html       # single-page dashboard served by the daemon
skills/jobctl/SKILL.md  # Claude Code skill — teaches an agent when and how to use the tool
tests/test_jobctl.py    # test suite — one test per invariant, plus golden records in fixtures/
README.md               # human-facing docs: the problem, the design, full usage
AGENTS.md               # this file (CLAUDE.md symlinks here)
```

## Invariants — do not break these

These are not style preferences. Each one is load-bearing, and each is the kind of thing a
well-intentioned refactor breaks.

- **Python 3 standard library only.** No `pip install`, no virtualenv, no third-party
  imports — not `requests`, not `click`, not `rich`. Zero dependencies is why the two files
  can be copied to any machine with Python 3 and simply work, including remote boxes reached
  over SSH. Adding a dependency defeats the tool's main deployment story.
- **Arguments are hand-parsed; do not introduce `argparse`.** The CLI walks `sys.argv`
  directly so that everything after `--` reaches the job verbatim, without `jobctl`
  interpreting flags meant for the job. Converting to `argparse` looks like a cleanup and
  quietly breaks that guarantee.
- **`--host` must never fall through to local execution.** A `--host` invocation short-circuits
  ahead of `ensure_daemon()` and never contacts the local daemon. If it could fall through, a
  job meant for a remote GPU box would silently run on a laptop instead — with no error, and
  usually discovered hours later. Two properties enforce this: the dispatch tests `host is not
  None` (not truthiness, so an empty alias cannot slip past), and an empty alias is rejected
  outright so `--host "$UNSET_VAR"` fails loudly. Both branches return, making local execution
  structurally unreachable once a host is given. Preserve all of this.
- **`cmd` is a list passed to `subprocess.Popen`, never a shell string.** There is no shell
  between `jobctl` and the job, so there is no quoting or word-splitting layer to get wrong.
  Joining it into a string would introduce an injection and quoting surface that does not
  currently exist.
- **One writer per file: the daemon owns `meta.json`; the supervisor owns `supervisor.json` and
  the log.** The daemon folds the supervisor's record into `meta.json` during reconciliation; the
  supervisor never touches `meta.json`, and the daemon never writes `supervisor.json`. Writes are
  atomic (tmp + `os.replace`), so single-writer means no cross-process locking is needed at all.
  "Why not have the supervisor update `meta.json` directly?" looks like a simplification and
  reintroduces a read-modify-write race — e.g. `stop_job`'s `status: "stopping"` landing after the
  supervisor's `exited` and stranding the job as "stopping" forever.
- **`meta.json` outlives the code that wrote it; add fields only through `apply_meta_defaults()`.**
  A job submitted weeks ago is still listed and still reconciled by whatever daemon is running
  today, so a record on disk can be older than every field the current code expects. `read_meta`
  routes every record through `apply_meta_defaults()`, the single definition of the record's
  shape — `submit_job` builds new records through it too. Adding a field anywhere else makes
  every existing record raise `KeyError` in the next thing that reads it. The CLI reads job
  records defensively for the mirror-image reason: `daemon restart` exists to replace a daemon
  older than the CLI invoking it, so it must not require that daemon to speak the current schema.
- **The supervisor composes a job's environment in one order: inherited environment, then
  jobctl's own defaults, then `meta["env"]` last.** `--env` is the caller's final word, so the
  values it supplies must be applied on top of everything jobctl sets for itself — today that
  is `PYTHONUNBUFFERED=1`, added so a file-backed stdout does not block-buffer a running job's
  logs into invisibility. Reversing the last two steps still looks correct and still passes a
  casual test, but silently strips `--env` of the ability to override a default. The concrete
  casualty is the opt-out: `--env PYTHONUNBUFFERED=` works only because an empty value lands
  after the default and Python honours only a non-empty one. Applied first, it is overwritten,
  and the flag does nothing with no error to say so.
- **The daemon binds `127.0.0.1` only.** It has no authentication and assumes a single local
  user. Multi-machine use goes through SSH via `--host`, which keeps authentication in SSH
  where it belongs. Never bind another interface or add a network-exposed mode.
- **`--host` runs `ssh -n`; never remove the `-n`.** `ssh` reads stdin by default, so without
  it the first `--host` call inside a script fed from stdin consumes the rest of that script as
  input for the remote command, and every later line silently never runs. That breaks this
  tool's own documented polling idiom — `while ! jobctl --host h wait ...; do :; done` piped
  into a shell, which both `README.md` and the agent skill instruct people to use — and it
  fails without an error, so the loop simply appears to have worked. No subcommand ever needs
  local stdin: the job's own stdin is `DEVNULL`, set by the supervisor.
- **A pause SIGSTOPs the whole process group, supervisor included, and `paused` is not a
  terminal state.** `SIGSTOP` can be neither caught nor ignored, so `jobctl pause` freezes the
  supervisor mid-`proc.wait()` along with the job. That is by design and harmless: nothing is
  waiting on the supervisor, and its `wait` resumes on `SIGCONT` with the job's exit code still
  recorded by the one process guaranteed to outlive it. Liveness is unaffected, because nothing
  here reaps with `WUNTRACED` — `Popen.poll()` and `os.kill(pid, 0)` both read a stopped process
  as alive, which is why a paused job is not misreported as dead. `paused` therefore belongs in
  `ACTIVE_STATUSES` in all three places that define it (`jobd.py`, `jobctl`, `static/index.html`):
  a frozen job must never read as finished to `wait`, `list`, `logs --follow`, `daemon restart`
  or the dashboard.
- **`stop_job` must `SIGCONT` a paused job before it `SIGTERM`s it.** A stopped process acts on
  nothing: the `SIGTERM` stays pending until something continues it, so the whole 10-second grace
  period would elapse with the job still frozen and every stop of a paused job would escalate to
  `SIGKILL` — losing the recorded exit code that the graceful path exists to preserve. Removing
  the `SIGCONT` still passes any test that only asserts the job eventually goes terminal; the test
  asserts the exit code is `-15` and that it arrives inside the grace window, which is what makes
  the difference visible.
- **Runtime excludes paused intervals.** `paused_secs` accumulates closed intervals and
  `paused_at` holds an open one; both are subtracted wherever runtime is rendered (`jobctl list`
  and the dashboard), and an open interval is closed by `resume_job`, by `stop_job`, and by
  `reconcile_job` when a job dies while paused. Without that last one a job killed while frozen
  would count the frozen time as work forever. Runtime is meant to answer "how long has this job
  been working", so a run parked overnight to free the GPU must not report the parking.
- **A `queued` job is active, has no pid and no `started_at`; the watcher starts it on the
  dependency's terminal state.** `--after` makes deferred work a record rather than a process, so
  the job it waits on is the only thing that can start it: `submit_job` writes the record and
  returns, and `start_queued_jobs` in the watcher spawns it — through the same `start_job` a
  synchronous submit uses — on the first tick where the dependency is no longer active. `queued`
  therefore belongs in `ACTIVE_STATUSES` in all three places (`jobd.py`, `jobctl`,
  `static/index.html`): a job that has not started must never read as finished to `wait` or
  `list`. The consequence to preserve everywhere else is that **`started_at` can be `None`** —
  it is the field that distinguishes a job that has begun from one still waiting, so it is set
  at the moment of the spawn and not before, and every reader of it (the `/api/jobs` sort key,
  `list`'s window filter and runtime column, the dashboard's) must tolerate `None` rather than
  arithmetic on it. Two more properties follow from having no process: `reconcile_job` returns a
  queued record untouched (there is no liveness to check and no supervisor record to fold in),
  and `stop_job` marks it `stopped` without signalling anything — which is also what stops the
  watcher from ever starting it. The dependency's exit code is deliberately irrelevant: a job
  queued behind a run that fails still gets its turn, because `--after` chains work and does not
  express success. `test_after_starts_when_the_dependency_ends` pins the chain and the `-`
  runtime, `test_stop_on_a_queued_job_never_starts_it` the stop.
- **Exit codes are a contract.** `jobctl wait` exits `0` on terminal state and `1` on timeout;
  the documented `while ! jobctl wait ...; do :; done` loop depends on it, and `--host`
  propagates the remote code verbatim. Do not remap or swallow exit codes.

## Keep the docs in sync

Any change to CLI behavior, flags, or defaults must be reflected in **both**:

- `README.md` — the human-facing reference, including the command table.
- `skills/jobctl/SKILL.md` — what an agent reads to use the tool correctly. A stale skill
  means agents use the tool wrongly, which is worse than no skill at all.

## Verification

Before committing:

- `python3 tests/test_jobctl.py` passes. The suite is one file, stdlib `unittest` only, and
  every invocation in it runs against its own throwaway daemon — it never reads or writes the
  user's `~/.jobctl/`, and it leaves no daemon behind even when a test fails. There is one
  test per invariant above, named so that a failure says which invariant broke.
- `python3 -m py_compile jobctl jobd.py tests/test_jobctl.py` passes.
- Local smoke test against the real daemon: `submit` a trivial job, then `list`, `status`,
  `logs`, `wait`, `stop`. The daemon auto-starts, so no setup is needed.
- If you touched `--host`: the remote path *is* testable without a reachable host. The suite
  puts a fake `ssh` on `PATH` that records its argv and honours `-n`, which covers command
  construction, the `-n`, verbatim exit-code propagation, and — by asserting no `daemon.pid`,
  `daemon.port`, `daemon.log` or `jobs/` appears in a fresh state directory — the guarantee
  that a `--host` invocation never reaches `ensure_daemon()`. Extend `TestInvariants` rather
  than reasoning about the remote path in your head. What genuinely remains unverified is only
  the far side: that a real remote `jobctl` does the right thing with the command it receives.
  Say so plainly in your report.
- **Prefer testing against the running daemon.** When you genuinely need an isolated one, set
  **both** `JOBCTL_STATE_DIR` and `JOBCTL_PORT` — an isolated daemon needs its own files *and*
  its own socket, and neither variable implies the other. `JOBCTL_STATE_DIR` gives the daemon
  its own `daemon.port`, `daemon.pid`, `daemon.log` and `jobs/`, and the CLI reads the same
  variable, so nothing you do touches the user's `~/.jobctl/` — but `jobd.py` still reads
  `JOBCTL_PORT` (default `8787`) and binds it, so on a machine where the user's daemon already
  holds `8787` your daemon dies with "address already in use" and the CLI spins for five
  seconds and exits 1. `JOBCTL_PORT` alone is **not** isolation either, and is worse: it
  changes which port gets recorded in the shared `daemon.port`, not which file gets written,
  so it redirects the user's own CLI to your throwaway daemon. Bind port 0, read the port back,
  and pass it alongside a `mkdtemp()` state dir, as `isolated_env()` in the suite does.
- **Test the new code against an artifact produced before your change.** When you change the
  shape of a persisted record or the shape of an invocation, a state directory your test just
  created cannot falsify it: every record in one was written by the code under test, and every
  argv in one was synthesised by the test. Plant a real old `meta.json` (there are golden ones
  in `tests/fixtures/`) and reproduce the real invocation context (a stdin-fed script, not just
  an argv list). Three separate bugs shipped past clean-room testing this way — a `KeyError`
  on a record predating a field, and an `ssh` without `-n` that ate the rest of its caller's
  script, which no flag-level test could see because the argv was identical either way.

## What NOT to touch

- `~/.jobctl/` on the local machine — that is live user state (running jobs, logs), not
  repository content.
- Commit identity. Never set `user.name`/`user.email` or `GIT_AUTHOR_*`/`GIT_COMMITTER_*`;
  use whatever git resolves. Never add `Co-Authored-By` trailers.
- Local system detail, in anything checked in. This is a public repository, so no file here may
  carry an absolute path containing a username, a hostname, an ssh alias, or a job record copied
  out of somebody's own `~/.jobctl`. Test fixtures are where this goes wrong, because the honest
  instinct is right: a realistic old `meta.json` has to come from a real one, or it proves
  nothing. Capture the *shape* from real state — which keys exist, and which do not — then
  replace the values with generic ones. Documentation examples need the same care: a pasted
  terminal transcript is a real transcript, and the job names in it are somebody's real work.
  The keys are what the tests turn on; the values are only ever illustration.
