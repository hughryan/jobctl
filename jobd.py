#!/usr/bin/python3
"""Local job daemon: run detached long-running commands, track status, tail logs, stop them.

Bound to 127.0.0.1 only — not for remote/multi-user use. State lives on disk under
$JOBCTL_STATE_DIR/jobs/<id>/ (default ~/.jobctl) as meta.json + log, so it survives daemon
restarts; a background watcher reconciles process liveness so nothing goes untracked if the
daemon itself gets restarted while jobs are running.
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
popens = {}  # job_id -> subprocess.Popen, only for jobs launched by this daemon instance


def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def read_meta(job_id):
    path = os.path.join(job_dir(job_id), "meta.json")
    with open(path) as f:
        return json.load(f)


def write_meta(job_id, meta):
    path = os.path.join(job_dir(job_id), "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, path)


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


def submit_job(name, cmd, cwd, env_overrides):
    job_id = f"{name}-{uuid.uuid4().hex[:8]}" if name else uuid.uuid4().hex[:12]
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)
    log_path = os.path.join(d, "log")
    env = dict(os.environ)
    env.update(env_overrides or {})

    meta = {
        "id": job_id,
        "name": name,
        "cmd": cmd,
        "cwd": cwd,
        "status": "running",
        "pid": None,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
    }
    write_meta(job_id, meta)

    logf = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group -> can killpg cleanly, survives daemon's own signals
    )
    meta["pid"] = proc.pid
    write_meta(job_id, meta)
    with lock:
        popens[job_id] = proc
    return job_id


def reconcile_job(job_id):
    """Update on-disk status for a job if its process has exited, however we find out."""
    meta = read_meta(job_id)
    if meta["status"] not in ("running", "stopping"):
        return meta
    with lock:
        proc = popens.get(job_id)
    if proc is not None:
        rc = proc.poll()
        if rc is not None:
            meta["status"] = "stopped" if meta["status"] == "stopping" else "exited"
            meta["exit_code"] = rc
            meta["ended_at"] = time.time()
            write_meta(job_id, meta)
        return meta
    # No in-memory handle (daemon restarted since submit) - fall back to PID liveness.
    if meta["pid"] and not pid_alive(meta["pid"]):
        meta["status"] = "exited"
        meta["exit_code"] = None  # unknown - daemon wasn't attached to reap it
        meta["ended_at"] = meta.get("ended_at") or time.time()
        write_meta(job_id, meta)
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
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass

        def escalate():
            time.sleep(STOP_GRACE_SECS)
            if pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            reconcile_job(job_id)

        threading.Thread(target=escalate, daemon=True).start()
    else:
        meta["status"] = "exited"
        meta["ended_at"] = time.time()
        write_meta(job_id, meta)
    return meta


def watcher_loop():
    while True:
        for job_id in list_job_ids():
            try:
                reconcile_job(job_id)
            except Exception:
                pass
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
    sys.exit(main())
