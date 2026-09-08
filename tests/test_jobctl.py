#!/usr/bin/env python3
"""Test suite for jobctl and jobd.py.

Standard library only, in keeping with the repository's headline invariant: the tests must
run wherever the tool itself runs, which is anywhere with a Python 3 and nothing installed.

The classes map onto the axes that have actually broken this tool:

  TestInvariants          one test per bullet in AGENTS.md, "Invariants — do not break these"
  TestLegacyRecords       a meta.json older than the code reading it — the KeyError axis
  TestVersionSkew         a new CLI rendering what an older daemon served — the same axis, upstream
  TestInvocationContext   how a command is invoked, not what its argv says — the `ssh -n` axis

Every subprocess runs against an isolated daemon: a fresh $JOBCTL_STATE_DIR *and* a free
$JOBCTL_PORT. Both are required. The state dir alone gives the daemon its own files but not
its own port, so it would still try to bind 8787, collide with the user's real daemon, and
fail to start. Nothing here reads or writes ~/.jobctl.

Run with `python3 tests/test_jobctl.py` or `python3 -m unittest discover tests`.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
JOBCTL_PATH = os.path.join(REPO_ROOT, "jobctl")
JOBD_PATH = os.path.join(REPO_ROOT, "jobd.py")
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")
# The golden records in there are captured artifacts: their *shape* — which keys exist, and
# which do not — comes from real records written by daemons that predate the fields now being
# read, and that is the whole reason they can falsify anything. Their *values* are sanitized,
# because this is a public repository and nothing here may carry a developer's local paths or
# job names. Sanitize a value freely; never "simplify" the key set.

# Files ensure_daemon() creates on its way to starting a local daemon. Their absence is the
# structural proof that a --host invocation never reached it.
DAEMON_ARTIFACTS = ("daemon.pid", "daemon.port", "daemon.log", "jobs")

# Stand-in for ssh, dropped on PATH so the --host path is exercised with no remote host. It
# records the argv it was handed and exits with whatever the test asked for.
#
# It also reproduces the one behavior of real ssh that this suite exists to pin down: ssh
# forwards its own stdin to the remote command, so it reads stdin to EOF unless -n redirects
# that from /dev/null. A shim that never touched stdin would let the stdin-fed-script tests
# pass with or without the -n, which is precisely the blind spot that let the bug ship.
FAKE_SSH_SOURCE = '''#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
log_path = os.environ.get("FAKE_SSH_LOG")
if log_path:
    with open(log_path, "a") as f:
        f.write(json.dumps(argv) + "\\n")
if "-n" not in argv:
    try:
        sys.stdin.buffer.read()  # what eats the rest of a stdin-fed script
    except OSError:
        pass
print("fake-ssh invoked: " + " ".join(argv))
sys.exit(int(os.environ.get("FAKE_SSH_EXIT") or "0"))
'''

# Prints the environment the *job* actually sees, so the supervisor's composition order can
# be asserted from the outside rather than by reading supervise().
ENV_PROBE = ("import json, os; "
             "print(json.dumps({k: os.environ.get(k) for k in ('PYTHONUNBUFFERED', 'FOO')}))")


def free_port():
    """A port nothing is listening on, for an isolated daemon to bind."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def isolated_env(state_dir, port, **extra):
    """An environment whose jobctl/jobd cannot touch the user's real ~/.jobctl daemon.

    Both variables, always: JOBCTL_STATE_DIR redirects the files, JOBCTL_PORT redirects the
    socket. With only the first, jobd still binds 8787 and dies on the real daemon's port.
    """
    env = os.environ.copy()
    env["JOBCTL_STATE_DIR"] = state_dir
    env["JOBCTL_PORT"] = str(port)
    env.update(extra)
    return env


def run_cli(env, *args, timeout=60, cwd=REPO_ROOT):
    """Run the worktree's jobctl, returning (returncode, stdout, stderr).

    Invoked as [sys.executable, JOBCTL_PATH] rather than by name: no dependence on the
    executable bit or on PATH, and jobctl resolves jobd.py next to its own realpath, so this
    also guarantees the jobd.py under test is this worktree's.
    """
    proc = subprocess.run([sys.executable, JOBCTL_PATH, *args],
                          env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def parse_runtime(text):
    """Seconds from what fmt_duration prints — "45s", "2m5s", "1h3m"."""
    units = {"h": 3600, "m": 60, "s": 1}
    total, digits = 0, ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        else:
            total += int(digits) * units[ch]
            digits = ""
    return total


def write_fake_ssh(directory):
    """Write the ssh stand-in into `directory` and return the directory."""
    path = os.path.join(directory, "ssh")
    with open(path, "w") as f:
        f.write(FAKE_SSH_SOURCE)
    os.chmod(path, 0o755)
    return directory


def read_ssh_log(path):
    """The argv lists the fake ssh recorded, one per invocation."""
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def import_isolated(name, path, state_dir):
    """Import jobctl or jobd.py in-process, with their module-level state pointed at scratch.

    Both resolve STATE_DIR and their port from the environment at import time, so it has to
    be right before exec_module rather than before the first call. Importing either starts
    nothing — both guard their entry point on __main__ — and the environment is put back
    afterwards so no later subprocess inherits a value it did not ask for.

    `jobctl` has no `.py` extension, so spec_from_file_location cannot infer a loader for it
    from the suffix; naming the loader explicitly is what makes the CLI importable at all.
    """
    previous = {key: os.environ.get(key) for key in ("JOBCTL_STATE_DIR", "JOBCTL_PORT")}
    os.environ["JOBCTL_STATE_DIR"] = state_dir
    os.environ["JOBCTL_PORT"] = str(free_port())
    try:
        loader = importlib.machinery.SourceFileLoader(name, path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def reap_process(proc):
    """Kill a helper process and wait for it, so nothing spawned by a test outlives it."""
    try:
        proc.kill()
    except OSError:
        pass
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def plant_fixture(state_dir, fixture_name):
    """Install a golden meta.json as an on-disk job, as if a much older daemon wrote it."""
    with open(os.path.join(FIXTURES_DIR, fixture_name)) as f:
        meta = json.load(f)
    d = os.path.join(state_dir, "jobs", meta["id"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    open(os.path.join(d, "log"), "a").close()
    return meta["id"]


def stop_isolated_daemon(env, state_dir):
    """Stop a scratch daemon and delete its state dir, whatever went wrong before now.

    The backstop matters more than the happy path: a leaked jobd.py would outlive the whole
    test run, and the point of this suite is that it leaves nothing behind.
    """
    try:
        try:
            rc, _, _ = run_cli(env, "daemon", "stop", timeout=30)
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc != 0:
            try:
                with open(os.path.join(state_dir, "daemon.pid")) as f:
                    os.kill(int(f.read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def reap_stray_daemon(env, state_dir):
    """Stop a daemon that a --host test should never have started.

    Nothing to do in the passing case — the whole point of those tests is that no daemon
    exists here — so this costs a stat. It exists because the failing case is the dangerous
    one: a regression that lets --host fall through starts a real daemon per invocation, and
    without this the run that detected the regression would also strand those daemons on the
    machine for the rest of the session.
    """
    if os.path.exists(os.path.join(state_dir, "daemon.pid")):
        stop_isolated_daemon(env, state_dir)


class DaemonHarness:
    """Mixin giving a TestCase its own daemon in a scratch state dir.

    Not a TestCase itself, so unittest does not collect it. start_daemon/stop_daemon are
    called from setUpClass/tearDownClass; stop_daemon is written so that a failing test can
    never leave a daemon process behind.
    """

    @classmethod
    def start_daemon(cls, prepare=None):
        cls.state_dir = tempfile.mkdtemp(prefix="jobctl-test-")
        cls.port = free_port()
        cls.env = isolated_env(cls.state_dir, cls.port)
        if prepare is not None:
            prepare(cls.state_dir)
        # `list` is enough: everything but `daemon` runs ensure_daemon() first.
        rc, out, err = run_cli(cls.env, "list")
        if rc != 0:
            stop_isolated_daemon(cls.env, cls.state_dir)
            raise AssertionError(f"isolated daemon did not start: rc={rc} out={out!r} err={err!r}")

    @classmethod
    def stop_daemon(cls):
        stop_isolated_daemon(cls.env, cls.state_dir)

    def submit(self, *args, name="t", timeout=60):
        """Submit a job and return its id, failing the test if the submit failed."""
        rc, out, err = run_cli(self.env, "submit", "--name", name, *args, timeout=timeout)
        self.assertEqual(rc, 0, f"submit failed: {err}")
        return out.strip()


class TestInvariants(DaemonHarness, unittest.TestCase):
    """One test per bullet in AGENTS.md, "Invariants — do not break these"."""

    @classmethod
    def setUpClass(cls):
        cls.start_daemon()

    @classmethod
    def tearDownClass(cls):
        cls.stop_daemon()

    # --- helpers -------------------------------------------------------------------

    def host_env(self, exit_code=0):
        """A fresh, empty state dir plus a fake ssh on PATH: (env, state_dir, ssh_log).

        The state dir is deliberately untouched by any daemon, so the absence of
        ensure_daemon()'s artifacts in it is a fact about this invocation alone.
        """
        state_dir = tempfile.mkdtemp(prefix="jobctl-host-")
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        bin_dir = tempfile.mkdtemp(prefix="jobctl-fakessh-")
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        write_fake_ssh(bin_dir)
        ssh_log = os.path.join(bin_dir, "ssh.log")
        env = isolated_env(state_dir, free_port(),
                           PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
                           FAKE_SSH_LOG=ssh_log, FAKE_SSH_EXIT=str(exit_code))
        self.addCleanup(reap_stray_daemon, env, state_dir)
        return env, state_dir, ssh_log

    def assertNoLocalDaemon(self, state_dir):
        for name in DAEMON_ARTIFACTS:
            self.assertFalse(
                os.path.exists(os.path.join(state_dir, name)),
                f"--host fell through to local execution: ensure_daemon() created {name}")

    def job_log(self, job_id):
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, f"job {job_id} did not finish: {out} {err}")
        rc, out, err = run_cli(self.env, "logs", job_id)
        self.assertEqual(rc, 0, err)
        return out

    def env_probe(self, *submit_opts):
        """Submit the env probe with `submit_opts` and return what the job saw, as a dict."""
        job_id = self.submit(*submit_opts, "--", sys.executable, "-c", ENV_PROBE, name="envprobe")
        lines = [line for line in self.job_log(job_id).splitlines() if line.strip()]
        self.assertTrue(lines, "env probe produced no output")
        return json.loads(lines[-1])

    def assertRejected(self, args, needle):
        rc, out, err = run_cli(self.env, *args)
        self.assertEqual(rc, 2, f"expected exit 2 for {args}, got {rc}: {out} {err}")
        self.assertIn(needle, err)
        self.assertNotIn("Traceback", err, f"{args} reported itself as a crash, not an error")

    # --- invariant: --host must never fall through to local execution ---------------

    def test_host_never_falls_through_to_local_execution(self):
        """AGENTS.md: a --host invocation must never start or contact the local daemon."""
        invocations = [
            ["list"],
            ["status", "somejob"],
            ["logs", "somejob"],
            ["stop", "somejob"],
            ["wait", "somejob"],
            ["submit", "--name", "n", "--", "true"],
        ]
        for args in invocations:
            with self.subTest(subcommand=args[0]):
                env, state_dir, ssh_log = self.host_env()
                rc, out, err = run_cli(env, "--host", "fakehost", *args)
                self.assertEqual(rc, 0, err)
                self.assertEqual(len(read_ssh_log(ssh_log)), 1,
                                 "the invocation did not go out over ssh exactly once")
                self.assertNoLocalDaemon(state_dir)

    def test_host_empty_alias_is_rejected(self):
        """`--host "$UNSET_VAR"` must fail loudly rather than degrade into a local run."""
        env, state_dir, ssh_log = self.host_env()
        rc, out, err = run_cli(env, "--host", "", "list")
        self.assertEqual(rc, 2)
        self.assertIn("non-empty", err)
        self.assertIn("alias", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(read_ssh_log(ssh_log), [], "a rejected alias still reached ssh")
        self.assertNoLocalDaemon(state_dir)

    # --- invariant: --host runs `ssh -n` --------------------------------------------

    def test_host_passes_dash_n_to_ssh(self):
        """Without -n, ssh eats its caller's stdin; see TestInvocationContext for the effect."""
        env, state_dir, ssh_log = self.host_env()
        rc, out, err = run_cli(env, "--host", "fakehost", "list")
        self.assertEqual(rc, 0, err)
        recorded = read_ssh_log(ssh_log)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0], "-n", f"ssh argv lost its -n: {recorded[0]}")

    # --- invariant: exit codes are a contract ---------------------------------------

    def test_host_propagates_remote_exit_code_verbatim(self):
        """`while ! jobctl --host h wait ...` depends on the remote code arriving unchanged."""
        env, state_dir, ssh_log = self.host_env(exit_code=7)
        rc, out, err = run_cli(env, "--host", "fakehost", "list")
        self.assertEqual(rc, 7, f"remote exit code was remapped: {out} {err}")
        self.assertIn("fake-ssh invoked", out, "remote stdout did not reach the caller")

    def test_wait_exits_zero_on_terminal_state(self):
        job_id = self.submit("--", "true", name="waitok")
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, f"{out} {err}")
        self.assertEqual(json.loads(out)["status"], "exited")

    def test_wait_exits_one_on_timeout(self):
        job_id = self.submit("--", "sleep", "30", name="waittimeout")
        self.addCleanup(self.stop_and_reap, job_id)
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "2", "--poll", "0.2")
        self.assertEqual(rc, 1, f"a timed-out wait must exit 1: {out} {err}")
        meta = json.loads(out)
        self.assertIs(meta["timed_out"], True)
        self.assertEqual(meta["status"], "running")

    def stop_and_reap(self, job_id):
        """Stop a job and block until it is terminal, so no supervisor outlives the suite."""
        run_cli(self.env, "stop", job_id, timeout=30)
        run_cli(self.env, "wait", job_id, "--timeout", "30", "--poll", "0.2", timeout=45)

    # --- invariant: a pause freezes the whole group, and paused is not terminal -------

    def ticker(self, name):
        """Submit a job that appends a line to a file every 0.1s; return (job_id, path).

        A file the job writes, rather than its own log, because file size is a direct
        measure of whether the process is executing at all — which is the only question
        SIGSTOP raises. The path is passed as an argument, not interpolated into the
        script, so a temp path can never be re-read as shell syntax.
        """
        tick_dir = tempfile.mkdtemp(prefix="jobctl-tick-")
        self.addCleanup(shutil.rmtree, tick_dir, ignore_errors=True)
        path = os.path.join(tick_dir, "ticks")
        job_id = self.submit("--", "sh", "-c", 'while true; do echo tick >> "$1"; sleep 0.1; done',
                             "ticker", path, name=name)
        self.addCleanup(self.stop_and_reap, job_id)
        return job_id, path

    def tick_size(self, path):
        return os.path.getsize(path) if os.path.exists(path) else 0

    def wait_for_ticks(self, path, since=0, timeout=15):
        """Block until the tick file has grown past `since`, and return its new size."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            size = self.tick_size(path)
            if size > since:
                return size
            time.sleep(0.05)
        self.fail(f"the job wrote nothing past {since} bytes within {timeout}s")

    def job_meta(self, job_id):
        rc, out, err = run_cli(self.env, "status", job_id)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def list_row(self, job_id):
        """The `jobctl list` row for a job, as fields: [id, status, runtime, *cmd]."""
        rc, out, err = run_cli(self.env, "list")
        self.assertEqual(rc, 0, err)
        for line in out.splitlines():
            fields = line.split()
            if fields[:1] == [job_id]:
                return fields
        self.fail(f"{job_id} is missing from `jobctl list`:\n{out}")

    def pause(self, job_id):
        rc, out, err = run_cli(self.env, "pause", job_id)
        self.assertEqual(rc, 0, f"pause failed: {out} {err}")
        self.assertEqual(json.loads(out)["status"], "paused")

    def test_pause_freezes_the_job_and_resume_thaws_it(self):
        """SIGSTOP the group and it stops computing; SIGCONT and it picks up where it was."""
        job_id, path = self.ticker("pause")
        self.wait_for_ticks(path)
        self.pause(job_id)
        self.assertEqual(self.job_meta(job_id)["status"], "paused")
        time.sleep(0.2)  # let the signal land before sampling what "frozen" means
        frozen = self.tick_size(path)
        time.sleep(0.7)  # seven ticks' worth, had anything in the group still been running
        self.assertEqual(self.tick_size(path), frozen, "a paused job kept running")

        rc, out, err = run_cli(self.env, "resume", job_id)
        self.assertEqual(rc, 0, f"resume failed: {out} {err}")
        self.assertEqual(json.loads(out)["status"], "running")
        self.wait_for_ticks(path, since=frozen, timeout=5)

    def test_wait_keeps_blocking_on_a_paused_job(self):
        """A pause is not an ending: paused is an active status, so `wait` must time out."""
        job_id, path = self.ticker("pausewait")
        self.wait_for_ticks(path)
        self.pause(job_id)
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "1", "--poll", "0.2")
        self.assertEqual(rc, 1, f"wait treated a paused job as terminal: {out} {err}")
        self.assertEqual(json.loads(out)["status"], "paused")

    def test_stop_terminates_a_paused_job(self):
        """Without a SIGCONT first the SIGTERM only sits pending, and stop escalates to -9."""
        job_id, path = self.ticker("pausestop")
        self.wait_for_ticks(path)
        self.pause(job_id)
        rc, out, err = run_cli(self.env, "stop", job_id)
        self.assertEqual(rc, 0, f"stop failed: {out} {err}")

        started = time.time()
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, f"a stopped-while-paused job never went terminal: {out} {err}")
        meta = json.loads(out)
        self.assertEqual(meta["status"], "stopped")
        self.assertEqual(meta["exit_code"], -signal.SIGTERM,
                         "the SIGTERM did not reach the frozen job; stop escalated instead")
        self.assertLess(time.time() - started, 8,
                        "the job died only after the 10s SIGKILL escalation")

    def test_pause_rejects_a_job_that_is_not_running(self):
        job_id = self.submit("--", "true", name="pausefinished")
        rc, out, err = run_cli(self.env, "wait", job_id, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, err)
        rc, out, err = run_cli(self.env, "pause", job_id)
        self.assertEqual(rc, 1, f"pausing a finished job succeeded: {out}")
        self.assertIn("exited", err, "the refusal did not name the job's actual state")
        self.assertNotIn("Traceback", err)

    def test_resume_rejects_a_job_that_is_not_paused(self):
        job_id = self.submit("--", "sleep", "30", name="resumerunning")
        self.addCleanup(self.stop_and_reap, job_id)
        rc, out, err = run_cli(self.env, "resume", job_id)
        self.assertEqual(rc, 1, f"resuming a running job succeeded: {out}")
        self.assertIn("running", err, "the refusal did not name the job's actual state")
        self.assertNotIn("Traceback", err)

    # --- invariant: runtime excludes paused intervals ---------------------------------

    def test_runtime_excludes_time_spent_paused(self):
        job_id, path = self.ticker("pausedruntime")
        self.wait_for_ticks(path)
        self.pause(job_id)
        time.sleep(1.5)
        rc, out, err = run_cli(self.env, "resume", job_id)
        self.assertEqual(rc, 0, f"resume failed: {out} {err}")

        meta = self.job_meta(job_id)
        self.assertGreaterEqual(meta["paused_secs"], 1.4)
        self.assertIsNone(meta["paused_at"], "the resumed interval was left open")
        elapsed = time.time() - meta["started_at"]
        runtime = parse_runtime(self.list_row(job_id)[2])
        self.assertLess(runtime, elapsed - 1.4,
                        f"runtime {runtime}s counted the pause; {elapsed:.1f}s have elapsed")

    # --- invariant: a queued job is active, has no process, and starts on its dependency ---

    def test_after_starts_when_the_dependency_ends(self):
        """A `--after` job waits as a first-class record, then runs once the dependency ends."""
        first = self.submit("--", "sleep", "2", name="afterdep")
        second = self.submit("--after", first, "--", "sh", "-c", "echo started", name="afterjob")

        queued = self.job_meta(second)
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["started_at"], "a queued job must not carry a start time")
        self.assertIsNone(queued["pid"], "a queued job must not carry a pid")
        self.assertEqual(queued["after"], first)
        self.assertEqual(self.list_row(second)[1:3], ["queued", "-"],
                         "`list` rendered a runtime for a job that has not started")

        rc, out, err = run_cli(self.env, "wait", first, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, f"the dependency never finished: {out} {err}")
        dependency = json.loads(out)

        rc, out, err = run_cli(self.env, "wait", second, "--timeout", "8", "--poll", "0.2")
        self.assertEqual(rc, 0, f"the queued job never started: {out} {err}")
        meta = json.loads(out)
        self.assertEqual(meta["status"], "exited")
        self.assertEqual(meta["exit_code"], 0)
        self.assertIn("started", self.job_log(second), "the queued job never ran its command")
        self.assertGreaterEqual(meta["started_at"], dependency["ended_at"],
                                "the queued job started before its dependency ended")

    def test_after_starts_regardless_of_the_dependency_exit_code(self):
        """`--after` chains work, not success: a failed dependency still releases the queue."""
        first = self.submit("--", "sh", "-c", "exit 3", name="afterfaildep")
        second = self.submit("--after", first, "--", "true", name="afterfailjob")

        rc, out, err = run_cli(self.env, "wait", first, "--timeout", "30", "--poll", "0.2")
        self.assertEqual(rc, 0, f"the dependency never finished: {out} {err}")
        self.assertEqual(json.loads(out)["exit_code"], 3)

        rc, out, err = run_cli(self.env, "wait", second, "--timeout", "8", "--poll", "0.2")
        self.assertEqual(rc, 0, f"a job queued behind a failure never started: {out} {err}")
        meta = json.loads(out)
        self.assertEqual(meta["status"], "exited")
        self.assertEqual(meta["exit_code"], 0)

    def test_stop_on_a_queued_job_never_starts_it(self):
        """Stopping a queued job is a record change, and the watcher must never undo it."""
        first = self.submit("--", "sleep", "30", name="queuedstopdep")
        self.addCleanup(self.stop_and_reap, first)
        second = self.submit("--after", first, "--", "sh", "-c", "echo started",
                             name="queuedstopjob")

        rc, out, err = run_cli(self.env, "stop", second)
        self.assertEqual(rc, 0, f"stop failed: {out} {err}")
        meta = json.loads(out)
        self.assertEqual(meta["status"], "stopped")
        self.assertIsNone(meta["pid"], "stopping a queued job spawned something to signal")
        self.assertEqual(self.job_log(second).strip(), "")

        self.stop_and_reap(first)
        time.sleep(3)  # more than one watcher tick past the dependency going terminal
        self.assertEqual(self.job_meta(second)["status"], "stopped",
                         "the watcher started a job that had already been stopped")
        self.assertEqual(self.job_log(second).strip(), "")

    def test_after_rejects_an_unknown_job(self):
        """A dependency that does not exist is an error at submit, and leaves no record."""
        jobs_dir = os.path.join(self.state_dir, "jobs")
        before = sorted(os.listdir(jobs_dir))
        rc, out, err = run_cli(self.env, "submit", "--after", "nope", "--", "true")
        self.assertEqual(rc, 1, f"submitting after an unknown job succeeded: {out} {err}")
        self.assertIn("nope", err, "the error did not name the job that was not found")
        self.assertNotIn("Traceback", err)
        self.assertEqual(sorted(os.listdir(jobs_dir)), before,
                         "a rejected submit left a job record behind")

    def test_wait_blocks_on_a_queued_job(self):
        """Queued is not terminal: `wait` must time out on it exactly as on a running job."""
        first = self.submit("--", "sleep", "30", name="queuedwaitdep")
        self.addCleanup(self.stop_and_reap, first)
        second = self.submit("--after", first, "--", "true", name="queuedwaitjob")
        self.addCleanup(self.stop_and_reap, second)
        rc, out, err = run_cli(self.env, "wait", second, "--timeout", "1", "--poll", "0.2")
        self.assertEqual(rc, 1, f"wait treated a queued job as terminal: {out} {err}")
        self.assertEqual(json.loads(out)["status"], "queued")

    # --- invariant: everything after `--` reaches the job verbatim -------------------

    def test_everything_after_double_dash_reaches_the_job_verbatim(self):
        """The hand-rolled parser must not interpret a flag that belongs to the job."""
        command = ["echo", "--tail", "--host", "-n", "--env", "KEY=VALUE", "a b", "--"]
        job_id = self.submit("--", *command, name="verbatim")
        rc, out, err = run_cli(self.env, "status", job_id)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["cmd"], command)

    # --- invariant: supervisor env composition order ---------------------------------

    def test_supervisor_sets_pythonunbuffered_by_default(self):
        self.assertEqual(self.env_probe()["PYTHONUNBUFFERED"], "1")

    def test_env_flag_can_opt_out_of_pythonunbuffered(self):
        """The documented opt-out: it works only because meta["env"] is applied last."""
        seen = self.env_probe("--env", "PYTHONUNBUFFERED=")
        self.assertIn(seen["PYTHONUNBUFFERED"], ("", None),
                      "--env PYTHONUNBUFFERED= was overwritten by jobctl's own default")

    def test_env_flag_values_reach_the_job_alongside_the_defaults(self):
        seen = self.env_probe("--env", "FOO=bar")
        self.assertEqual(seen["FOO"], "bar")
        self.assertEqual(seen["PYTHONUNBUFFERED"], "1")

    # --- invariant: hand-parsed arguments report their own errors ---------------------

    def test_env_flag_rejects_malformed_assignments(self):
        for args, needle in [
            (["submit", "--env", "NOEQUALS", "--", "true"], "--env expects KEY=VALUE"),
            (["submit", "--env", "=value", "--", "true"], "--env expects KEY=VALUE"),
            (["submit", "--env", "--", "true"], "--env expects a value"),
        ]:
            with self.subTest(args=args):
                self.assertRejected(args, needle)

    def test_submit_option_values_are_required_and_non_empty(self):
        for args, needle in [
            (["submit", "--name", "--", "true"], "--name expects a value"),
            (["submit", "--cwd", "--", "true"], "--cwd expects a value"),
            (["submit", "--after", "--", "true"], "--after expects a value"),
            (["submit", "--name", "", "--", "true"], "--name expects a value"),
            (["submit", "--cwd", "", "--", "true"], "--cwd expects a value"),
            (["submit", "--after", "", "--", "true"], "--after expects a value"),
        ]:
            with self.subTest(args=args):
                self.assertRejected(args, needle)

    def test_submit_requires_a_command_after_the_separator(self):
        self.assertRejected(["submit", "echo", "hi"], "expected `--` before the command")
        self.assertRejected(["submit", "--"], "no command given after --")


class TestLegacyRecords(DaemonHarness, unittest.TestCase):
    """A meta.json can be older than every field the code reading it expects.

    This is the axis that produced a real `KeyError: 'job_pid'` in production, and a state
    dir the test itself just created structurally cannot reach it: every record in one was
    written by the current code. The fixtures are real records, planted on disk.
    """

    LEGACY = "meta-legacy-pre-supervisor.json"
    FUTURE = "meta-unknown-future-field.json"

    @classmethod
    def setUpClass(cls):
        cls.ids = {}

        def prepare(state_dir):
            cls.ids[cls.LEGACY] = plant_fixture(state_dir, cls.LEGACY)
            cls.ids[cls.FUTURE] = plant_fixture(state_dir, cls.FUTURE)

        cls.start_daemon(prepare=prepare)
        cls.jobd = import_isolated("jobd_under_test", JOBD_PATH, cls.state_dir)

    @classmethod
    def tearDownClass(cls):
        cls.stop_daemon()

    def fixture(self, name):
        with open(os.path.join(FIXTURES_DIR, name)) as f:
            return json.load(f)

    def assertClean(self, args):
        rc, out, err = run_cli(self.env, *args)
        self.assertEqual(rc, 0, f"{args} failed: {out} {err}")
        self.assertNotIn("Traceback", err, f"{args} crashed on an old record")
        return out

    def test_reading_commands_survive_an_old_record(self):
        for name in (self.LEGACY, self.FUTURE):
            job_id = self.ids[name]
            for args in (["list", "--all"], ["status", job_id], ["logs", job_id],
                         ["daemon", "status"]):
                with self.subTest(fixture=name, args=args):
                    self.assertClean(args)

    def test_daemon_restart_survives_an_active_legacy_job(self):
        """A *running* job whose record predates `job_pid` — the production crash's shape.

        The terminal fixtures cannot reach this: warn_before_restart returns immediately on
        an empty list, so with only exited records neither it nor print_job_lines ever runs,
        and the restart test would prove the daemon's backfill rather than the CLI's survival.
        A record only stays "running" through reconciliation if its recorded pid is genuinely
        alive, so the pid has to be a real process — which is why this is not a fixture. It is
        the shape of any job submitted before there were supervisors, in a state directory
        old enough to still hold one.
        """
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(reap_process, sleeper)

        record = self.fixture(self.LEGACY)
        record["id"] = "legacy-active-0badf00d"
        record["status"] = "running"
        record["pid"] = sleeper.pid
        record["ended_at"] = None
        self.assertNotIn("job_pid", record, "the legacy fixture stopped predating job_pid")
        d = os.path.join(self.state_dir, "jobs", record["id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(record, f, indent=2)
        open(os.path.join(d, "log"), "a").close()

        out = self.assertClean(["daemon", "restart"])
        # Assert the warning actually rendered, not merely that nothing crashed: without this
        # the test passes just as happily when the record never reaches the path at all.
        self.assertIn(record["id"], out)
        self.assertIn("job pid", out)
        self.assertIn("(none recorded)", out)
        self.assertIn("no job pid recorded", out)

    def test_daemon_restart_survives_an_old_record(self):
        """The exact invocation that raised KeyError: 'job_pid' against a real ~/.jobctl."""
        out = self.assertClean(["daemon", "restart"])
        self.assertIn("started daemon", out)
        self.assertClean(["list", "--all"])

    def test_apply_meta_defaults_backfills_missing_fields(self):
        meta = self.jobd.apply_meta_defaults(self.fixture(self.LEGACY))
        self.assertEqual(meta["env"], {})
        self.assertIsNone(meta["job_pid"])

    def test_apply_meta_defaults_leaves_present_values_untouched(self):
        original = self.fixture(self.LEGACY)
        meta = self.jobd.apply_meta_defaults(dict(original))
        for key, value in original.items():
            self.assertEqual(meta[key], value, f"{key} was overwritten by its default")

    def test_apply_meta_defaults_preserves_key_order(self):
        """setdefault, not a merge: `jobctl status` output still leads with the id."""
        meta = self.jobd.apply_meta_defaults(self.fixture(self.LEGACY))
        self.assertEqual(list(meta)[0], "id")

    def test_apply_meta_defaults_is_idempotent(self):
        once = self.jobd.apply_meta_defaults(self.fixture(self.LEGACY))
        twice = self.jobd.apply_meta_defaults(dict(once))
        self.assertEqual(once, twice)
        self.assertEqual(list(once), list(twice))

    def test_apply_meta_defaults_does_not_share_default_values(self):
        """Defaults are built per call, so one record's env cannot leak into another's."""
        first = self.jobd.apply_meta_defaults(self.fixture(self.LEGACY))
        first["env"]["LEAKED"] = "1"
        second = self.jobd.apply_meta_defaults(self.fixture(self.LEGACY))
        self.assertEqual(second["env"], {})

    def test_apply_meta_defaults_keeps_unknown_fields(self):
        meta = self.jobd.apply_meta_defaults(self.fixture(self.FUTURE))
        self.assertEqual(meta["some_future_field"], "x")


class TestVersionSkew(unittest.TestCase):
    """A new CLI rendering records served by a daemon older than itself.

    This is how the production KeyError actually arose, and it sits upstream of the on-disk
    axis: the old daemon predated apply_meta_defaults, so it served records with `job_pid`
    genuinely *absent* — not present-and-null. Every test that runs against a current daemon
    gets the key backfilled before the CLI ever sees it, so the defensive `.get()` calls in
    print_job_lines and warn_before_restart are never exercised end to end. `daemon restart`
    exists precisely to retire a daemon that predates the CLI running it, so these two
    functions must render whatever such a daemon returns.

    Called in-process rather than by resurrecting an old jobd.py: the input under test is the
    record's shape, and building it directly is both exact and honest about what is asserted.
    """

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.mkdtemp(prefix="jobctl-skew-")
        cls.jobctl = import_isolated("jobctl_under_test", JOBCTL_PATH, cls.scratch)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def raw_record(self, job_id):
        """A real pre-supervisor record, as an old daemon served it: no `job_pid` key."""
        with open(os.path.join(FIXTURES_DIR, "meta-legacy-pre-supervisor.json")) as f:
            record = json.load(f)
        record["id"] = job_id
        record["status"] = "running"
        record["ended_at"] = None
        self.assertNotIn("job_pid", record, "the legacy fixture stopped predating job_pid")
        return record

    def current_record(self, job_id, job_pid):
        record = self.raw_record(job_id)
        record["env"] = {}
        record["job_pid"] = job_pid
        return record

    def capture(self, func, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            func(*args)
        return buffer.getvalue()

    def test_print_job_lines_renders_a_record_with_no_job_pid_key(self):
        out = self.capture(self.jobctl.print_job_lines, [self.raw_record("old-1a2b3c4d")])
        self.assertIn("old-1a2b3c4d", out)
        self.assertIn("(none recorded)", out)

    def test_warn_before_restart_handles_a_record_with_no_job_pid_key(self):
        out = self.capture(self.jobctl.warn_before_restart, [self.raw_record("old-1a2b3c4d")])
        self.assertIn("1 job active", out)
        self.assertIn("(none recorded)", out)
        self.assertIn("1 of those has no job pid recorded", out)

    def test_warn_before_restart_partitions_old_and_current_records(self):
        """It splits on exactly the key an old daemon omits, so mix both shapes in one list."""
        jobs = [self.raw_record("old-1a2b3c4d"), self.current_record("new-5e6f7a8b", 4242)]
        out = self.capture(self.jobctl.warn_before_restart, jobs)
        self.assertIn("2 jobs active", out)
        self.assertIn("old-1a2b3c4d", out)
        self.assertIn("new-5e6f7a8b", out)
        self.assertIn("(none recorded)", out)
        self.assertIn("4242", out)
        self.assertIn("1 of those has no job pid recorded", out)


class TestInvocationContext(unittest.TestCase):
    """How a command is invoked, not what its argv says.

    `ssh` without `-n` consumed the rest of a script fed to a shell on stdin, so every line
    after the first `--host` call silently never ran. The argv was identical either way,
    which is why no flag-level test could see it: only running jobctl from a stdin-fed
    script reaches this axis.
    """

    def script_env(self, exit_code=0):
        state_dir = tempfile.mkdtemp(prefix="jobctl-ctx-")
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        bin_dir = tempfile.mkdtemp(prefix="jobctl-fakessh-")
        self.addCleanup(shutil.rmtree, bin_dir, ignore_errors=True)
        write_fake_ssh(bin_dir)
        env = isolated_env(state_dir, free_port(),
                           PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
                           FAKE_SSH_LOG=os.path.join(bin_dir, "ssh.log"),
                           FAKE_SSH_EXIT=str(exit_code),
                           PYTHON=sys.executable, JOBCTL=JOBCTL_PATH)
        self.addCleanup(reap_stray_daemon, env, state_dir)
        return env, state_dir

    def run_script(self, body, env, timeout=30):
        """Feed `body` to bash on stdin — the context in which the missing -n bit."""
        directory = tempfile.mkdtemp(prefix="jobctl-script-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "script.sh")
        with open(path, "w") as f:
            f.write(body)
        with open(path) as stdin:
            proc = subprocess.run(["bash"], stdin=stdin, env=env, cwd=REPO_ROOT,
                                  capture_output=True, text=True, timeout=timeout)
        return proc

    def test_host_call_does_not_consume_the_rest_of_a_stdin_fed_script(self):
        env, state_dir = self.script_env()
        proc = self.run_script(
            '"$PYTHON" "$JOBCTL" --host fakehost list\n'
            'echo MARKER-REACHED\n', env)
        self.assertIn("MARKER-REACHED", proc.stdout,
                      "the --host call ate the rest of the script (ssh lost its -n)")
        self.assertFalse(os.path.exists(os.path.join(state_dir, "daemon.pid")))

    def test_documented_polling_loop_survives_a_host_call(self):
        """The idiom README.md and the agent skill both tell people to use."""
        env, state_dir = self.script_env()
        proc = self.run_script(
            'while ! "$PYTHON" "$JOBCTL" --host fakehost wait somejob --timeout 1; do :; done\n'
            'echo MARKER-LOOP-DONE\n', env)
        self.assertIn("MARKER-LOOP-DONE", proc.stdout,
                      "the polling loop's trailing lines never ran")


if __name__ == "__main__":
    unittest.main()
