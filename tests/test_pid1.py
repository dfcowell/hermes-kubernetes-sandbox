"""The work container's PID 1 must reap orphans (F-P3-5/F-P3-7).

Hermes runs a background command on a non-local backend as a wrapper subshell that outlives the
exec that started it, so it is reparented to PID 1, and its poller decides "finished" with
``kill -0 <wrapper pid>``. ``kill -0`` succeeds on a zombie: under a PID 1 that never waits
(``sleep infinity``) the poller never sees the exit and the completion notice is never queued.

The namespace tests run the real PID 1 command in an ``unshare -rpf`` PID namespace (an
unprivileged user namespace; skipped where the host forbids one, e.g. AppArmor-restricted
runners) and inspect it from the host's /proc, so they need no image and no cluster.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from kubernetes_sandbox.podspec import WORK_PID1, _spec, template_hash
from kubernetes_sandbox.settings import Settings

SLEEP_PID1 = ["sleep", "infinity"]  # the old PID 1: the negative control
KATA = Settings(runtime_class_name="kata", docker_disk_storage_class="block-ephemeral")
MODES = [KATA, Settings(runtime_class_name="kata", docker_disk_storage_class="b", docker_cli="init-copy"),
         Settings(docker=False)]


def _work(s):
    return next(c for c in _spec(s, "claim")["containers"] if c["name"] == "work")


@pytest.mark.parametrize("s", MODES, ids=["image-volume", "init-copy", "no-docker"])
def test_work_pid1_is_the_reaper_in_every_mode(s):
    assert _work(s)["command"] == WORK_PID1
    assert WORK_PID1[0] == "bash"  # the runner already needs bash on PATH; no init binary is assumed


def test_work_command_is_a_copy():
    _work(KATA)["command"].append("x")
    assert _work(KATA)["command"] == WORK_PID1


def test_docker_readiness_probe_tolerates_slow_docker_info():
    p = _work(KATA)["readinessProbe"]
    assert p["timeoutSeconds"] == 5  # the 1 s default timed out regularly under Kata
    assert p["periodSeconds"] == 2 and p["failureThreshold"] == 3


@pytest.mark.parametrize("s", MODES, ids=["image-volume", "init-copy", "no-docker"])
def test_new_pid1_changes_the_template_hash(s, monkeypatch):
    # Documented consequence: Pods created before the upgrade keep `sleep infinity` until the
    # plugin replaces them on their next command. Both hashes come from the real template_hash,
    # so a key it adds later cannot make the comparison pass on its own.
    from kubernetes_sandbox import podspec
    new_hash = template_hash(s)
    assert template_hash(s) == new_hash  # positive control: the same template hashes equal
    monkeypatch.setattr(podspec, "WORK_PID1", SLEEP_PID1)
    assert _work(s)["command"] == SLEEP_PID1  # the patch reached the rendered template
    assert template_hash(s) != new_hash


# ---- PID-namespace tests --------------------------------------------------------------------


def _userns_pidns_available() -> str:
    if not (shutil.which("unshare") and shutil.which("nsenter")):
        return "unshare/nsenter not installed"
    r = subprocess.run(["unshare", "-rpf", "true"], capture_output=True, text=True, timeout=10)
    if r.returncode:
        return f"no unprivileged user+PID namespace here: {r.stderr.strip()[:120]}"
    return ""


_SKIP = _userns_pidns_available()
needs_pidns = pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "available")


def _stat(pid: int):
    """(state, ppid) from the host's /proc, or None once the pid is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return rest[0], int(rest[1])
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


def _children(pid: int) -> dict[int, str]:
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            st = _stat(int(d))
            if st and st[1] == pid:
                out[int(d)] = st[0]
    return out


class Namespace:
    """`argv` running as PID 1 of a fresh PID namespace, launched exactly as the container runtime
    launches the work container's command. `prelude` runs in that PID 1 before it execs `argv`."""

    def __init__(self, argv, prelude=":"):
        self.proc = subprocess.Popen(["unshare", "-rpf", "bash", "-c", prelude + '\nexec "$@"', "pid1", *argv],
                                     stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        self.pid1 = None
        while self.pid1 is None and time.monotonic() < deadline:
            kids = _children(self.proc.pid)
            self.pid1 = next(iter(kids), None)
            time.sleep(0.05)
        assert self.pid1, "PID 1 of the namespace did not start"

    def descendants(self) -> dict[int, str]:
        seen, todo = {}, [self.pid1]
        while todo:
            for pid, state in _children(todo.pop()).items():
                seen[pid] = state
                todo.append(pid)
        return seen

    def run(self, cmd: str, out, timeout=30) -> int:
        """One command inside the namespace, like a `kubectl exec`: its parent is outside, so
        whatever it leaves running is reparented to PID 1 when it returns."""
        return subprocess.run(["nsenter", "-t", str(self.pid1), "-U", "-p", "--preserve-credentials",
                               "bash", "-c", cmd], stdin=subprocess.DEVNULL, stdout=out,
                              stderr=subprocess.STDOUT, timeout=timeout).returncode

    def close(self):
        if self.proc.poll() is None:
            try:
                os.kill(self.pid1, signal.SIGKILL)  # PID 1 takes the whole namespace with it
            except ProcessLookupError:
                pass
        self.proc.wait(timeout=10)


@pytest.fixture
def namespace():
    made = []

    def make(argv, prelude=":"):
        made.append(Namespace(argv, prelude))
        return made[-1]
    yield make
    for ns in made:
        ns.close()


def _wait_for(pred, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


# A child of PID 1 that, after PID 1 has exec'd its command, exits and leaves a grandchild behind
# (reparented to PID 1); both then finish. This is Hermes's background wrapper shape.
_ORPHAN_PRELUDE = "( sleep 2 & sleep 1 ) &"


def _cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").split("\0")[:-1]
    except (FileNotFoundError, ProcessLookupError):
        return []


def _orphans_started(ns) -> bool:
    """The prelude's subshell and both its sleeps exist (a reaper adds its own sleep)."""
    return len(ns.descendants()) >= 3


def _only_own_sleep_left(ns) -> bool:
    """All that is left is one live `sleep infinity`, PID 1's own direct child: the orphans have
    finished AND been reaped (a zombie would still be listed)."""
    left = ns.descendants()
    if len(left) != 1:
        return False
    (pid, state), = left.items()
    return state != "Z" and _stat(pid)[1] == ns.pid1 and _cmdline(pid) == ["sleep", "infinity"]


@needs_pidns
def test_reaper_pid1_reaps_adopted_orphans(namespace):
    ns = namespace(WORK_PID1, _ORPHAN_PRELUDE)
    # Wait for the orphans to exist first: before the prelude forks, its subshell can be PID 1's only
    # descendant, and a bare "one descendant left" would pass without testing anything.
    assert _wait_for(lambda: _orphans_started(ns), 10), ns.descendants()
    assert _wait_for(lambda: _only_own_sleep_left(ns), 15), ns.descendants()


@needs_pidns
def test_sleep_pid1_leaves_zombies(namespace):  # negative control: the test can see the bug
    ns = namespace(SLEEP_PID1, _ORPHAN_PRELUDE)
    assert _wait_for(lambda: _orphans_started(ns), 10), ns.descendants()
    assert _wait_for(lambda: list(ns.descendants().values()).count("Z") == 2, 15), ns.descendants()
    assert not _only_own_sleep_left(ns)  # the reaper test's check fails here


# Negative control shaped like the reaper: PID 1 keeps its own `sleep infinity` child but never
# waits, so only reaping (not the process shape) can make the reaper test's check pass.
_NON_REAPING_PID1 = [sys.executable, "-c",
                     "import subprocess, time; subprocess.Popen(['sleep', 'infinity']); time.sleep(1e9)"]


@needs_pidns
def test_non_reaping_pid1_with_its_own_sleep_fails_the_reaper_check(namespace):
    ns = namespace(_NON_REAPING_PID1, _ORPHAN_PRELUDE)
    assert _wait_for(lambda: _orphans_started(ns), 10), ns.descendants()
    assert _wait_for(lambda: list(ns.descendants().values()).count("Z") == 2, 15), ns.descendants()
    assert not _wait_for(lambda: _only_own_sleep_left(ns), 2)


@needs_pidns
def test_reaper_pid1_exits_promptly_on_term(namespace):
    ns = namespace(WORK_PID1)
    time.sleep(0.3)  # let bash install the trap
    os.kill(ns.pid1, signal.SIGTERM)  # sent from outside the namespace, like the kubelet's stop
    assert ns.proc.wait(timeout=5) == 0


@needs_pidns
def test_sleep_pid1_ignores_term(namespace):  # negative control: a deletion waited out its grace
    ns = namespace(SLEEP_PID1)
    os.kill(ns.pid1, signal.SIGTERM)
    time.sleep(1)
    assert ns.proc.poll() is None


# ---- Hermes's own background-process path, offline -------------------------------------------


class NamespaceEnv:
    """The two things Hermes's spawn_via_env/poller need from an environment: execute() and a temp
    dir. Each execute is one `nsenter` into the namespace, as the plugin's is one exec."""

    def __init__(self, ns, tmp):
        self.ns, self.tmp, self.closed = ns, tmp, False

    def get_temp_dir(self):
        return str(self.tmp)

    def execute(self, command, timeout=None, **_kw):
        if self.closed:
            raise RuntimeError("sandbox gone")
        # A file, not a pipe: the backgrounded wrapper keeps the exec's stdout open after the exec
        # returns (the plugin's runner handles that itself).
        with open(self.tmp / "exec.out", "w+b") as out:
            rc = self.ns.run(command, out, timeout=timeout or 30)
            out.seek(0)
            return {"output": out.read().decode(errors="replace"), "returncode": rc}


def _spawn_background(env, tmp, command):
    """Exactly what the terminal tool does for background=True, notify_on_complete=True."""
    from tools.terminal_tool_background import spawn_background_process
    return json.loads(spawn_background_process(
        command=command, env=env, env_type="kubernetes", effective_task_id="t-pid1", task_id="t-pid1",
        session_key="", workdir=str(tmp), cwd=str(tmp), effective_pty=False, notify_on_complete=True,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None))


def _completion(session_id, seconds):
    from tools.process_registry import process_registry
    deadline = time.monotonic() + seconds
    held = []
    try:
        while time.monotonic() < deadline:
            try:
                evt = process_registry.completion_queue.get(timeout=0.2)
            except Exception:
                continue
            if evt.get("session_id") == session_id and evt.get("type") == "completion":
                return evt
            held.append(evt)
        return None
    finally:
        for evt in held:
            process_registry.completion_queue.put(evt)


def _alive_in(env, pid) -> bool:
    return env.execute(f"kill -0 {pid} 2>/dev/null && echo alive || echo gone")["output"].strip() == "alive"


@needs_pidns
def test_hermes_background_notify_completes_under_reaper_pid1(namespace, tmp_path):
    env = NamespaceEnv(namespace(WORK_PID1), tmp_path)
    started = _spawn_background(env, tmp_path, "sleep 1; echo bg-done")
    assert started["notify_on_complete"] is True and started["pid"], started
    try:
        evt = _completion(started["session_id"], 20)
        assert evt is not None, "no completion queued"
        assert evt["exit_code"] == 0 and evt["completion_reason"] == "exited", evt
        assert not _alive_in(env, started["pid"])  # the wrapper is gone, not a zombie
        assert "Z" not in env.ns.descendants().values()
    finally:
        env.closed = True


@needs_pidns
def test_hermes_background_notify_is_lost_under_sleep_pid1(namespace, tmp_path):  # negative control
    from tools.process_registry import process_registry
    env = NamespaceEnv(namespace(SLEEP_PID1), tmp_path)
    started = _spawn_background(env, tmp_path, "sleep 1; echo bg-done")
    try:
        assert _completion(started["session_id"], 8) is None
        assert _alive_in(env, started["pid"])  # kill -0 succeeds on the zombie wrapper
        assert "Z" in env.ns.descendants().values()
    finally:
        # Best-effort cleanup with no assert: a failure here must not mask one above.
        env.closed = True  # the poller's next execute raises: it finishes the session as "lost"
        session = process_registry.get(started["session_id"])
        poller_stopped = _wait_for(lambda: session.exited, 10)
        _completion(started["session_id"], 1)  # drain the "lost" notice
    assert poller_stopped, "the poller kept running after the sandbox was closed"
