#!/usr/bin/python3
"""Local job daemon: run detached long-running commands, track status, tail logs, stop them.

Bound to 127.0.0.1 only — not for remote/multi-user use. State lives on disk under
$JOBCTL_STATE_DIR/jobs/<id>/ (default ~/.jobctl) as meta.json + log, so it survives daemon
restarts; a background watcher reconciles process liveness so nothing goes untracked if the
daemon itself gets restarted while jobs are running.

Each job runs under a supervisor — this same file re-invoked as `jobd.py --supervise <id>` —
that spawns the command, waits on it, and records the exit code to supervisor.json. The
supervisor lives exactly as long as the job, so exit codes survive daemon restarts and
crashes instead of depending on the daemon holding a Popen handle.

One writer per file: the daemon is the only writer of meta.json, and the supervisor is the
only writer of supervisor.json; the daemon composes the supervisor's record into meta.json
during reconciliation. Two writers of one file would lose updates to read-modify-write races.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# $JOBCTL_STATE_DIR gives a daemon its own port file, pid file, log and jobs — everything
# that makes one instance distinct from another. Setting $JOBCTL_PORT alone does not: the
# port is still recorded in the shared directory, so a second daemon would redirect the CLI
# that the first one's jobs belong to.
STATE_DIR = os.path.expanduser(os.environ.get("JOBCTL_STATE_DIR") or "~/.jobctl")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("JOBCTL_PORT", "8787"))
STOP_GRACE_SECS = 10

os.makedirs(JOBS_DIR, exist_ok=True)  # creates STATE_DIR too, wherever it points

lock = threading.Lock()
# job_id -> the *supervisor's* subprocess.Popen, only for jobs launched by this daemon
# instance. It exists to poll()-reap supervisors so they never linger as zombies, and as a
# liveness signal. It is NOT a source of exit codes — poll() returns the supervisor's exit
# status, not the job's; the job's exit code comes only from supervisor.json.
popens = {}
# job_ids whose process group this daemon SIGKILLed (stop escalation). SIGKILL cannot be
# ignored, so it takes the supervisor down with the job and no result gets recorded; this
# set lets reconcile_job report the -9 the daemon itself inflicted instead of "unknown".
sigkilled = set()


def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def read_meta(job_id):
    path = os.path.join(job_dir(job_id), "meta.json")
    with open(path) as f:
        return json.load(f)


def write_meta(job_id, meta):
    write_json_atomic(os.path.join(job_dir(job_id), "meta.json"), meta)


def supervisor_path(job_id):
    return os.path.join(job_dir(job_id), "supervisor.json")


def read_supervisor(job_id):
    """The supervisor's record for a job, or {} if none exists (yet, or ever — old jobs)."""
    try:
        with open(supervisor_path(job_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def list_job_ids():
    if not os.path.isdir(JOBS_DIR):
        return []
    return sorted(os.listdir(JOBS_DIR))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def supervise(job_id):
    """Run as a job's supervisor: spawn the command, wait for it, record its exit code.

    The daemon spawns this (`jobd.py --supervise <id>`) instead of the command itself, so
    the exit code is recorded by a process that lives exactly as long as the job — a daemon
    restart or crash between submit and exit no longer loses it. This function writes
    supervisor.json and only supervisor.json; meta.json belongs to the daemon.
    """
    # Ignore SIGTERM before spawning anything: stop_job SIGTERMs the whole group, and the
    # supervisor must outlive the child by a moment to record how it died.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    meta = read_meta(job_id)
    env = dict(os.environ)
    env.update(meta.get("env") or {})
    logf = open(os.path.join(job_dir(job_id), "log"), "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            meta["cmd"],  # a list, straight to Popen — no shell in between, ever
            cwd=meta["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            # No start_new_session: the child shares the supervisor's process group, so
            # stop_job's killpg on the supervisor reaches the job and its descendants.
            # SIG_IGN dispositions survive exec, so the child must reset SIGTERM to
            # default or it would inherit the supervisor's immunity and every stop would
            # escalate to SIGKILL. preexec_fn runs post-fork in the child; it is unsafe
            # only in threaded processes, and the supervisor is single-threaded.
            preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL),
        )
    except Exception as exc:
        # Exec failure is asynchronous now — submit already returned an id. Fail as a job:
        # explain in the log, record 127 (the shell's "command not found" convention).
        logf.write(f"jobctl: could not execute {meta['cmd'][0]!r}: {exc}\n".encode())
        write_json_atomic(supervisor_path(job_id), {"exit_code": 127, "ended_at": time.time()})
        return 0
    write_json_atomic(supervisor_path(job_id), {"job_pid": proc.pid})
    rc = proc.wait()
    write_json_atomic(supervisor_path(job_id),
                      {"job_pid": proc.pid, "exit_code": rc, "ended_at": time.time()})
    return 0


def submit_job(name, cmd, cwd, env_overrides):
    job_id = f"{name}-{uuid.uuid4().hex[:8]}" if name else uuid.uuid4().hex[:12]
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)

    meta = {
        "id": job_id,
        "name": name,
        "cmd": cmd,
        "cwd": cwd,
        "env": env_overrides or {},  # applied by the supervisor on top of its inherited env
        "status": "running",
        "pid": None,
        "job_pid": None,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
    }
    write_meta(job_id, meta)
    # Touch the log so reads never 404 in the moment before the supervisor opens it. The
    # supervisor writes it; the daemon keeps no handle on it.
    open(os.path.join(d, "log"), "ab").close()

    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--supervise", job_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # group leader: pgid == pid, and signals aimed at the daemon don't reach it
    )
    # "pid" is the *supervisor's* pid, deliberately: stop_job needs the process-group leader
    # for killpg, and liveness checks need a process that lives exactly as long as the job.
    # The command's own pid surfaces as "job_pid", folded in from supervisor.json.
    meta["pid"] = proc.pid
    write_meta(job_id, meta)
    with lock:
        popens[job_id] = proc
    return job_id


def reconcile_job(job_id):
    """Compose the supervisor's record into meta.json, however this daemon finds the job.

    Precedence: a terminal meta.json is final; an exit_code recorded in supervisor.json is
    authoritative, whichever daemon instance launched the job; otherwise the job is still
    running unless the supervisor is provably gone, which means it died without recording.
    Writes meta.json only when something actually changed — the watcher calls this for
    every job every 2 seconds, and must not continuously rewrite quiescent metadata.
    """
    meta = read_meta(job_id)
    if meta["status"] not in ("running", "stopping"):
        return meta

    # Liveness first, supervisor.json second. A dead supervisor can never write again, so a
    # record read after the liveness check is complete. The opposite order could read "no
    # result yet", then find the supervisor dead, and wrongly conclude the exit code was
    # lost when it was recorded between the two looks.
    with lock:
        proc = popens.get(job_id)
    if proc is not None:
        supervisor_alive = proc.poll() is None  # reaps the supervisor; NOT the job's exit code
    else:
        # Daemon restarted since submit — fall back to PID liveness of the supervisor.
        # A pid of None proves nothing: submit_job may not have recorded it yet.
        supervisor_alive = meta["pid"] is None or pid_alive(meta["pid"])

    sup = read_supervisor(job_id)
    changed = False
    if sup.get("job_pid") is not None and meta.get("job_pid") != sup["job_pid"]:
        meta["job_pid"] = sup["job_pid"]
        changed = True

    if "exit_code" in sup:
        meta["exit_code"] = sup["exit_code"]
        meta["ended_at"] = sup.get("ended_at") or time.time()
    elif not supervisor_alive:
        # Died without recording: a crash, or SIGKILL. If this daemon sent the SIGKILL it
        # knows exactly how the job died; otherwise the code is genuinely unknown.
        with lock:
            killed_by_us = job_id in sigkilled
        meta["exit_code"] = -signal.SIGKILL if killed_by_us else None
        meta["ended_at"] = meta.get("ended_at") or time.time()
    else:
        if changed:
            write_meta(job_id, meta)
        return meta

    meta["status"] = "stopped" if meta["status"] == "stopping" else "exited"
    write_meta(job_id, meta)
    with lock:
        sigkilled.discard(job_id)
    return meta


def stop_job(job_id):
    meta = read_meta(job_id)
    if meta["status"] not in ("running", "stopping"):
        return meta
    pid = meta["pid"]
    if pid and pid_alive(pid):
        meta["status"] = "stopping"
        write_meta(job_id, meta)
        try:
            os.killpg(pid, signal.SIGTERM)  # the supervisor ignores this; the job does not
        except OSError:
            pass

        def escalate():
            time.sleep(STOP_GRACE_SECS)
            if pid_alive(pid):
                # SIGKILL takes the supervisor down too, so no result will be recorded —
                # note that we did it, before sending it, so reconcile can report -9.
                with lock:
                    sigkilled.add(job_id)
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
                # Give the group a moment to die so the reconcile below lands terminal.
                for _ in range(20):
                    if not pid_alive(pid):
                        break
                    time.sleep(0.1)
            reconcile_job(job_id)

        threading.Thread(target=escalate, daemon=True).start()
    else:
        # The supervisor is already gone — let reconciliation pick up whatever it recorded
        # (a finished-but-not-yet-reconciled job still has a real exit code on disk).
        return reconcile_job(job_id)
    return meta


def watcher_loop():
    while True:
        for job_id in list_job_ids():
            try:
                reconcile_job(job_id)
            except Exception:
                pass
        # Reap supervisors that exited after their job's meta went terminal: reconcile_job
        # never polls a terminal job's handle again, and an unreaped supervisor would sit
        # as a zombie child of this daemon until it exits. Drop each handle once reaped -
        # otherwise popens grows for the daemon's whole lifetime and this pass re-polls
        # every job ever submitted, every two seconds.
        with lock:
            items = list(popens.items())
        reaped = [job_id for job_id, proc in items if proc.poll() is not None]
        if reaped:
            with lock:
                for job_id in reaped:
                    popens.pop(job_id, None)
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet; state changes are visible via /api/jobs

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        if parsed.path == "/api/health":
            return self._json(200, {"ok": True})

        if parsed.path == "/api/jobs":
            jobs = [reconcile_job(j) for j in list_job_ids()]
            jobs.sort(key=lambda m: m["started_at"], reverse=True)
            return self._json(200, jobs)

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            job_id = parts[2]
            if not os.path.isdir(job_dir(job_id)):
                return self._json(404, {"error": "not found"})
            return self._json(200, reconcile_job(job_id))

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "log":
            job_id = parts[2]
            log_path = os.path.join(job_dir(job_id), "log")
            if not os.path.isfile(log_path):
                return self._text(404, "")
            tail = int(qs.get("tail", ["200"])[0])
            with open(log_path, "rb") as f:
                data = f.read().decode(errors="replace")
            # Not `[-tail:]`: `[-0:]` is the whole list, which would turn `--tail 0` into
            # "everything" when the caller asked for nothing.
            lines = data.splitlines()[-tail:] if tail > 0 else []
            return self._text(200, "\n".join(lines))

        # static UI
        rel = parsed.path.lstrip("/") or "index.html"
        safe = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not safe.startswith(STATIC_DIR) or not os.path.isfile(safe):
            return self._text(404, "not found")
        with open(safe, "rb") as f:
            body = f.read()
        self.send_response(200)
        ctype = "text/html" if safe.endswith(".html") else "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""

        if parsed.path == "/api/jobs":
            try:
                payload = json.loads(raw or b"{}")
                cmd = payload["cmd"]
                if not isinstance(cmd, list) or not cmd:
                    raise ValueError("cmd must be a non-empty list")
                name = payload.get("name", "")
                cwd = payload.get("cwd") or os.getcwd()
                env_overrides = payload.get("env") or {}
                job_id = submit_job(name, cmd, cwd, env_overrides)
                return self._json(200, {"id": job_id})
            except Exception as e:
                return self._json(400, {"error": str(e)})

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "jobs" and parts[3] == "stop":
            job_id = parts[2]
            if not os.path.isdir(job_dir(job_id)):
                return self._json(404, {"error": "not found"})
            return self._json(200, stop_job(job_id))

        return self._json(404, {"error": "not found"})


def live_daemon():
    """Return (pid, port) of a daemon already serving STATE_DIR, or None if the state is stale.

    Two independent signals must agree before we call the directory occupied: the recorded pid
    is alive *and* the recorded port answers a health check. A live pid alone is not enough —
    pid numbers get recycled, so a stale daemon.pid whose number an unrelated process inherited
    would wedge this state directory shut forever, which is worse than the takeover it prevents.
    Anything missing, unreadable or unresponsive therefore means stale: proceed and take over.
    """
    try:
        with open(os.path.join(STATE_DIR, "daemon.pid")) as f:
            pid = int(f.read().strip())
        with open(os.path.join(STATE_DIR, "daemon.port")) as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
            if resp.status != 200 or not json.loads(resp.read()).get("ok"):
                return None
    except Exception:
        return None
    return pid, port


def main():
    owner = live_daemon()
    if owner:
        print(f"error: a jobd daemon (pid {owner[0]}) is already serving {STATE_DIR} on port "
              f"{owner[1]}; set JOBCTL_STATE_DIR to a scratch directory to run an isolated one",
              file=sys.stderr)
        return 1

    # Bind before recording anything. daemon.port is the pointer every CLI follows, so a start
    # that dies on an in-use port must not have overwritten a working daemon's on its way out.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        print(f"error: could not bind 127.0.0.1:{PORT} ({exc}); set JOBCTL_PORT to a free port, "
              "and JOBCTL_STATE_DIR too if you want an independent daemon", file=sys.stderr)
        return 1

    with open(os.path.join(STATE_DIR, "daemon.port"), "w") as f:
        f.write(str(PORT))
    with open(os.path.join(STATE_DIR, "daemon.pid"), "w") as f:
        f.write(str(os.getpid()))

    threading.Thread(target=watcher_loop, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    # Supervisor mode: one process per job, spawned by submit_job. Dispatched ahead of the
    # daemon path so a supervisor never tries to bind the port or take over the state dir.
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise":
        sys.exit(supervise(sys.argv[2]))
    sys.exit(main())
