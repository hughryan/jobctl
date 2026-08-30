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
- **The daemon binds `127.0.0.1` only.** It has no authentication and assumes a single local
  user. Multi-machine use goes through SSH via `--host`, which keeps authentication in SSH
  where it belongs. Never bind another interface or add a network-exposed mode.
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

- `python3 -m py_compile jobctl jobd.py` passes.
- Local smoke test against the real daemon: `submit` a trivial job, then `list`, `status`,
  `logs`, `wait`, `stop`. The daemon auto-starts, so no setup is needed.
- If you touched `--host`: the remote path cannot be tested end-to-end without a reachable
  host. Test command construction directly instead, and explicitly assert that `ensure_daemon`
  is never called when `--host` is set. Say plainly in your report what remains unverified.
- **Prefer testing against the running daemon.** When you genuinely need an isolated one, set
  `JOBCTL_STATE_DIR` to a scratch directory — that gives the daemon its own `daemon.port`,
  `daemon.pid`, `daemon.log` and `jobs/`, and the CLI reads the same variable, so nothing you
  do touches the user's `~/.jobctl/`. `JOBCTL_PORT` alone is **not** isolation: it changes
  which port gets recorded in the shared `daemon.port`, not which file gets written, so it
  redirects the user's own CLI to your throwaway daemon.

## What NOT to touch

- `~/.jobctl/` on the local machine — that is live user state (running jobs, logs), not
  repository content.
- Commit identity. Never set `user.name`/`user.email` or `GIT_AUTHOR_*`/`GIT_COMMITTER_*`;
  use whatever git resolves. Never add `Co-Authored-By` trailers.
