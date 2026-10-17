"""The two Hermes code paths that reach this backend outside a session's own terminal calls, run through Hermes itself:

- the system-prompt backend probe (``agent/prompt_builder.py``): must be answered by the Pod-less stub, never by a
  real sandbox (a Pod cold start blocking the prompt build, plus a workspace PVC left for the janitor);
- ``terminal_tool`` with no ``terminal.cwd``: Hermes plans its default cwd for the backend (``/root`` in v0.22.1) as
  every command's cwd, which the sandbox user (UID 1000) cannot enter; the command must run in the workspace instead.
  The plugin keeps no copy of that default: it asks Hermes's own resolver (``provider.hermes_default_cwd``).
"""
import inspect
import json
import os

import pytest
from fakes import FakeKube, LocalExec
from kubernetes_sandbox import environment as envmod
from kubernetes_sandbox.naming import WORKSPACE
from kubernetes_sandbox.probe import PROBE_OUTPUT, PROBE_TASK_ID, ProbeEnvironment
from kubernetes_sandbox.provider import KubernetesProvider, hermes_default_cwd


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


@pytest.fixture
def sandbox_settings(tmp_path, _isolated_hermes_home, monkeypatch):
    tok = tmp_path / "tok"
    tok.write_text("x")
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(f"docker: false\ntoken_path: {tok}\n")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)


@pytest.fixture
def hermes_default(monkeypatch):
    """Changes Hermes's built-in default cwd for this backend, as an upstream patch could."""
    from tools import terminal_tool

    def set_default(value):
        monkeypatch.setattr(terminal_tool, "_DEFAULT_CWD_BY_BACKEND",
                            {**terminal_tool._DEFAULT_CWD_BY_BACKEND, "kubernetes": value})
    return set_default


@pytest.fixture
def no_hermes_resolver(monkeypatch):
    """A Hermes whose resolver was renamed or removed: `from tools.terminal_tool import ...` fails."""
    from tools import terminal_tool
    monkeypatch.delattr(terminal_tool, "_resolve_config_cwd")


def test_the_default_comes_from_hermes_s_own_resolver(sandbox_settings):
    from tools import terminal_tool
    assert hermes_default_cwd("kubernetes") == terminal_tool._resolve_config_cwd("kubernetes", False)[0] == "/root"


def test_a_changed_hermes_default_is_followed_without_a_code_change(sandbox_settings, hermes_default):
    hermes_default("/home/hermes")
    assert hermes_default_cwd("kubernetes") == "/home/hermes"
    env = KubernetesProvider().create_environment(cwd="/home/hermes", timeout=60, task_id="t")
    assert env.cwd == WORKSPACE
    assert "cd -- /workspace" in env._wrap_command("pwd", "/home/hermes")
    assert "cd -- /root" in env._wrap_command("pwd", "/root")  # no longer Hermes's default: passes as given


@pytest.mark.parametrize("given,want", [("/root", WORKSPACE), ("", WORKSPACE), (None, WORKSPACE),
                                        ("/workspace/proj", "/workspace/proj"), ("/tmp", "/tmp")])
def test_create_environment_maps_the_hermes_default_cwd(sandbox_settings, given, want):
    env = KubernetesProvider().create_environment(cwd=given, timeout=60, task_id="t")
    assert env.cwd == want and env._initial_cwd == want


def test_a_configured_terminal_cwd_disables_the_mapping(sandbox_settings, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", "/tmp")
    assert hermes_default_cwd("kubernetes") is None  # Hermes plans /tmp: no default in play
    env = KubernetesProvider().create_environment(cwd="/tmp", timeout=60, task_id="t")
    assert env.cwd == "/tmp" and "cd -- /root" in env._wrap_command("pwd", "/root")


def test_an_unusable_terminal_cwd_that_hermes_replaces_is_mapped(registered, sandbox_settings, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", "relative/dir")  # Hermes discards it for its (registered) container backend
    assert hermes_default_cwd("kubernetes") == "/root"


def test_the_profile_scope_decides(sandbox_settings, monkeypatch):
    """Multiplexed: the routed profile's terminal policy, not the gateway process's environment."""
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    monkeypatch.setenv("TERMINAL_CWD", "/tmp")
    token = set_terminal_scope({"TERMINAL_ENV": "kubernetes"})  # this profile has no terminal.cwd
    try:
        assert hermes_default_cwd("kubernetes") == "/root"
    finally:
        reset_terminal_scope(token)
    assert hermes_default_cwd("kubernetes") is None


@pytest.mark.parametrize("terminal_cwd", [None, "/workspace"])
@pytest.mark.parametrize("given", ["/root", "/workspace/proj", "/home/hermes"])
def test_without_hermes_s_resolver_nothing_is_guessed(sandbox_settings, no_hermes_resolver, monkeypatch, caplog,
                                                      terminal_cwd, given):
    """The creation cwd is never taken as the default: it can be an ACP/gateway override or a recorded cwd."""
    if terminal_cwd:
        monkeypatch.setenv("TERMINAL_CWD", terminal_cwd)
    for _ in range(3):
        env = KubernetesProvider().create_environment(cwd=given, timeout=60, task_id="t")
        assert env.cwd == given and env._initial_cwd == given
        assert f"cd -- {given}" in env._wrap_command("pwd", given)
        assert "cd -- /root" in env._wrap_command("pwd", "/root")
    assert caplog.text.count("not mapped") == 1  # logged once per profile
    assert "resolver is unavailable" in caplog.text


@pytest.mark.parametrize("value", ["/root", ["/root", None], None, (), (1, None)])
def test_an_unexpected_resolver_result_maps_nothing(sandbox_settings, monkeypatch, caplog, value):
    from tools import terminal_tool
    monkeypatch.setattr(terminal_tool, "_resolve_config_cwd", lambda env_type, mount: value)
    assert hermes_default_cwd("kubernetes") is None
    assert hermes_default_cwd("kubernetes") is None
    assert caplog.text.count("unexpected value") == 1


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_terminal_cwd_maps_nothing_with_its_own_reason(sandbox_settings, monkeypatch, caplog, value):
    monkeypatch.setenv("TERMINAL_CWD", value)  # Hermes's _tenv returns it as is: no default substituted
    assert hermes_default_cwd("kubernetes") is None
    assert caplog.text.count("set but empty") == 1


def test_whitespace_around_terminal_cwd_compares_stripped(sandbox_settings, monkeypatch):
    from tools import terminal_tool
    monkeypatch.setenv("TERMINAL_CWD", " /workspace ")
    monkeypatch.setattr(terminal_tool, "_resolve_config_cwd", lambda env_type, mount: (" /workspace ", None))
    assert hermes_default_cwd("kubernetes") is None  # Hermes uses terminal.cwd: not taken for a default


def test_under_a_refusal_scope_nothing_is_mapped(sandbox_settings, caplog):
    from tools import terminal_scope
    token = terminal_scope.set_terminal_scope(terminal_scope.TerminalPolicyRefusal("unreadable"))
    try:
        assert hermes_default_cwd("kubernetes") is None
        assert hermes_default_cwd("kubernetes") is None
    finally:
        terminal_scope.reset_terminal_scope(token)
    assert caplog.text.count("not mapped") == 1


def test_wrap_command_runs_the_hermes_default_cwd_in_the_initial_cwd():
    from kubernetes_sandbox.settings import Settings
    from kubernetes_sandbox.naming import Identity

    def env(default_cwd):
        return envmod.KubernetesEnvironment(settings=Settings(docker=False), identity=Identity("p", "s"),
                                            cwd="/workspace", timeout=30, default_cwd=default_cwd,
                                            client=FakeKube(), exec_connect=LocalExec())
    e = env("/root")
    assert "cd -- /workspace" in e._wrap_command("pwd", "/root")
    assert "/root" not in e._wrap_command("pwd", "/root")
    assert "cd -- /tmp/x" in e._wrap_command("pwd", "/tmp/x")  # any other cwd (a workdir, the session's) passes
    assert "cd -- /root/x" in e._wrap_command("pwd", "/root/x")
    assert "cd -- /root" in env(None)._wrap_command("pwd", "/root")  # default unknown: nothing is mapped


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


@pytest.mark.parametrize("default", [None, "/home/hermes-nonexistent"])
def test_terminal_tool_without_terminal_cwd_runs_in_the_workspace(registered, local_sandbox, hermes_default, default):
    """Through Hermes's real terminal_tool and resolver; the second case is an upstream patch moving the default."""
    from tools import terminal_tool as tt
    if default is None:
        if os.access("/root", os.X_OK):
            pytest.skip("needs a /root this user cannot enter, like the sandbox user")
    else:
        hermes_default(default)
    task = f"probe-cwd-e2e-{'moved' if default else 'v0.22'}"
    try:
        planned = tt._get_env_config()["cwd"]
        assert planned == (default or "/root")  # the precondition: Hermes plans its default
        assert not os.access(planned, os.X_OK)  # ... which this user cannot enter
        r = json.loads(tt.terminal_tool("pwd; echo marker > f.txt", task_id=task))
        assert r.get("exit_code") == 0, r
        assert r["output"].strip().splitlines()[0] == str(local_sandbox), r
        assert (local_sandbox / "f.txt").read_text() == "marker\n"
        r2 = json.loads(tt.terminal_tool("pwd", task_id=task, workdir="/tmp"))  # an explicit workdir still wins
        assert r2.get("exit_code") == 0 and r2["output"].strip() == "/tmp", r2
    finally:
        from tools.terminal_tool_lifecycle import cleanup_vm
        cleanup_vm(tt._resolve_container_task_id(task))  # the environment's key (here "default")
        cleanup_vm(task)
        tt.clear_session_cwd(task)
        tt.clear_session_cwd("default")
