"""The two Hermes code paths that reach this backend outside a session's own terminal calls, run through Hermes itself:

- the system-prompt backend probe (``agent/prompt_builder.py``): must be answered by the Pod-less stub, never by a
  real sandbox (a Pod cold start blocking the prompt build, plus a workspace PVC left for the janitor);
- ``terminal_tool`` with no ``terminal.cwd``: Hermes plans the literal ``/root`` as every command's cwd, which the
  sandbox user (UID 1000) cannot enter; the command must run in the workspace instead.
"""
import inspect
import json
import os

import pytest
from fakes import FakeKube, LocalExec
from kubernetes_sandbox import environment as envmod
from kubernetes_sandbox.naming import HERMES_DEFAULT_CWD, WORKSPACE
from kubernetes_sandbox.probe import PROBE_OUTPUT, PROBE_TASK_ID, ProbeEnvironment
from kubernetes_sandbox.provider import KubernetesProvider


@pytest.fixture
def registered(monkeypatch):
    from agent import terminal_env_registry as reg
    previous, current = reg.snapshot_registration("kubernetes"), KubernetesProvider()
    reg.register_provider(current)
    monkeypatch.setenv("TERMINAL_ENV", "kubernetes")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    yield
    reg.restore_registration("kubernetes", current, previous)


@pytest.fixture
def no_real_sandbox(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a real KubernetesEnvironment was built")
    monkeypatch.setattr(envmod.KubernetesEnvironment, "__init__", refuse)


def test_probe_task_id_is_the_literal_hermes_uses():
    """Pins the literal against the Hermes under test: a bump that renames it makes the probe build a real sandbox
    again. Re-check agent/prompt_builder.py on every Hermes bump (README, "System-prompt probe")."""
    from agent import prompt_builder
    assert PROBE_TASK_ID == "prompt-backend-probe"
    assert f'task_id="{PROBE_TASK_ID}"' in inspect.getsource(prompt_builder._run_backend_probe)


def test_hermes_still_drops_probe_only_for_plugins():
    """Documents why the task_id is the signal: v0.22.1's plugin builder swallows probe_only. When this fails, Hermes
    forwards it — the stub already honours it; the task_id check can then become the fallback."""
    from tools import terminal_tool_backends
    src = inspect.getsource(terminal_tool_backends._build_plugin_env)
    assert "probe_only" not in src


@pytest.mark.parametrize("kwargs", [{"task_id": PROBE_TASK_ID}, {"task_id": "anything", "probe_only": True}])
def test_probe_gets_a_stub_without_settings_or_cluster(no_real_sandbox, kwargs):
    env = KubernetesProvider().create_environment(cwd="/root", timeout=180, image="", container_config={}, **kwargs)
    assert isinstance(env, ProbeEnvironment)
    from agent.prompt_builder import _BACKEND_PROBE_CMD
    assert env.execute(_BACKEND_PROBE_CMD, timeout=4) == {"output": PROBE_OUTPUT, "returncode": 0}
    assert env.execute("echo hi", timeout=4) == {"output": "", "returncode": 1}  # not the probe: no fake success
    assert env.cleanup() is None


def test_a_normal_task_still_gets_a_sandbox(tmp_path, _isolated_hermes_home):
    tok = tmp_path / "tok"
    tok.write_text("x")
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(f"docker: false\ntoken_path: {tok}\n")
    env = KubernetesProvider().create_environment(cwd="/workspace", timeout=60, task_id="session:abc")
    assert isinstance(env, envmod.KubernetesEnvironment)


def test_hermes_prompt_probe_uses_the_stub(registered, no_real_sandbox):
    from agent import prompt_builder
    from tools import terminal_tool
    out = prompt_builder._run_backend_probe("kubernetes", terminal_tool)
    assert out == PROBE_OUTPUT
    assert prompt_builder._format_backend_probe(out) == "  OS: Linux"
    prompt_builder._BACKEND_PROBE_CACHE.clear()
    try:
        assert "OS: Linux" in (prompt_builder._probe_remote_backend("kubernetes") or "")
    finally:
        prompt_builder._BACKEND_PROBE_CACHE.clear()


@pytest.mark.parametrize("given,want", [("/root", WORKSPACE), ("", WORKSPACE), (None, WORKSPACE),
                                        ("/workspace/proj", "/workspace/proj"), ("/tmp", "/tmp")])
def test_create_environment_maps_the_hermes_default_cwd(tmp_path, _isolated_hermes_home, given, want):
    tok = tmp_path / "tok"
    tok.write_text("x")
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(f"docker: false\ntoken_path: {tok}\n")
    env = KubernetesProvider().create_environment(cwd=given, timeout=60, task_id="t")
    assert env.cwd == want and env._initial_cwd == want


def test_wrap_command_runs_the_hermes_default_cwd_in_the_initial_cwd():
    from kubernetes_sandbox.settings import Settings
    from kubernetes_sandbox.naming import Identity
    e = envmod.KubernetesEnvironment(settings=Settings(docker=False), identity=Identity("p", "s"), cwd="/workspace",
                                     timeout=30, client=FakeKube(), exec_connect=LocalExec())
    assert "cd -- /workspace" in e._wrap_command("pwd", HERMES_DEFAULT_CWD)
    assert "/root" not in e._wrap_command("pwd", HERMES_DEFAULT_CWD)
    assert "cd -- /tmp/x" in e._wrap_command("pwd", "/tmp/x")  # any other cwd (a workdir, the session's) passes
    assert "cd -- /root/x" in e._wrap_command("pwd", "/root/x")


@pytest.fixture
def local_sandbox(tmp_path, _isolated_hermes_home, monkeypatch):
    """The provider's environment against a FakeKube, with commands run on THIS host by the real runner; the
    workspace is a temp dir standing in for the Pod's /workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    tok = tmp_path / "tok"
    tok.write_text("x")
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(f"docker: false\ntoken_path: {tok}\n")
    monkeypatch.setattr(envmod, "WORKSPACE", str(ws))
    monkeypatch.setattr(envmod, "KubeClient", lambda s: FakeKube(ready_after=0))
    ex = LocalExec()
    monkeypatch.setattr(envmod, "_default_exec_connect", lambda client: ex)
    return ws


@pytest.mark.skipif(os.access("/root", os.X_OK), reason="needs a /root this user cannot enter, like the sandbox user")
def test_terminal_tool_without_terminal_cwd_runs_in_the_workspace(registered, local_sandbox):
    from tools import terminal_tool as tt
    task = "probe-cwd-e2e"
    try:
        assert tt._get_env_config()["cwd"] == HERMES_DEFAULT_CWD  # the precondition: Hermes plans /root
        r = json.loads(tt.terminal_tool("pwd; echo marker > f.txt", task_id=task))
        assert r.get("exit_code") == 0, r
        assert r["output"].strip().splitlines()[0] == str(local_sandbox), r
        assert (local_sandbox / "f.txt").read_text() == "marker\n"
        r2 = json.loads(tt.terminal_tool("pwd", task_id=task, workdir="/tmp"))  # an explicit workdir still wins
        assert r2.get("exit_code") == 0 and r2["output"].strip() == "/tmp", r2
    finally:
        from tools.terminal_tool_lifecycle import cleanup_vm
        cleanup_vm(task)
        tt.clear_session_cwd("default")
