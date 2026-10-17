import pytest

from kubernetes_sandbox.provider import KubernetesProvider


def _write(home, text):
    (home / "kubernetes-sandbox.yaml").write_text(text)


def test_attributes():
    p = KubernetesProvider()
    assert p.name == "kubernetes" and p.is_remote and p.is_container
    assert p.skip_container_guards is False
    assert p.session_isolated_when_nonpersistent is True


def test_is_available_needs_valid_settings_and_token_without_network(_isolated_hermes_home, tmp_path):
    p = KubernetesProvider()
    assert p.is_available() is False  # defaults: docker needs a StorageClass, no token
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker: false\ntoken_path: {tok}\n")
    assert p.is_available() is True


def test_env_only_configuration_is_available(_isolated_hermes_home, tmp_path, monkeypatch):
    tok = tmp_path / "tok"
    tok.write_text("x")
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    monkeypatch.setenv("HERMES_K8S_TOKEN_PATH", str(tok))
    assert not (_isolated_hermes_home / "kubernetes-sandbox.yaml").exists()
    assert KubernetesProvider().is_available() is True


def test_missing_ca_file_is_reported(_isolated_hermes_home, tmp_path, caplog):
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker: false\ntoken_path: {tok}\nca_path: {tmp_path}/nope.crt\n")
    p = KubernetesProvider()
    assert p.check_requirements({}) is False
    assert "nope.crt" in caplog.text
    status, detail = p.probe()
    assert status == "needs_setup" and "nope.crt" in detail


def test_create_environment_ignores_image_override(_isolated_hermes_home, tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker: false\ntoken_path: {tok}\nwork_image: mine:1\n")
    env = KubernetesProvider().create_environment(cwd="/workspace", timeout=60, task_id="t", image="other:2")
    assert env._s.work_image == "mine:1"


def test_check_requirements_reports_interlock(_isolated_hermes_home, tmp_path, caplog):
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker_disk_storage_class: b\ntoken_path: {tok}\n")
    assert KubernetesProvider().check_requirements({}) is False
    assert "root on the node" in caplog.text


def test_probe_never_raises_on_bad_settings(_isolated_hermes_home):
    _write(_isolated_hermes_home, "nonsense_key: 1\n")
    status, detail = KubernetesProvider().probe()
    assert status == "needs_setup" and "nonsense_key" in detail


def test_create_environment_uses_profile_and_task_id(_isolated_hermes_home, tmp_path, monkeypatch):
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker: false\ntoken_path: {tok}\n")
    env = KubernetesProvider().create_environment(cwd="/workspace", timeout=60, task_id="thread-7", image=None,
                                                  container_config={}, unknown_future_kwarg=1)
    assert env._id.session_key == "thread-7"
    assert env._id.profile == "default"


def test_name_is_valid_and_not_a_builtin():
    import re
    from agent.terminal_env_registry import BUILTIN_BACKEND_NAMES
    # _reject_builtin_collision raises unconditionally (it is only the collision callback), so check the set.
    assert KubernetesProvider.name not in BUILTIN_BACKEND_NAMES
    assert re.fullmatch(r"[a-z0-9_]+", KubernetesProvider.name)


def test_approvals_stay_on_with_forwarded_credentials(_isolated_hermes_home):
    _write(_isolated_hermes_home, "docker: false\nforward_env: [FORGE_TOKEN]\n"
           "git_credentials: [{url: https://f.example.com, username: u, password_env: FORGE_TOKEN}]\n")
    p = KubernetesProvider()
    p._settings()  # the credential settings load
    assert p.skip_container_guards is False  # K12: approvals stay on (amendment §2.6 item 4)


def _ready_settings(home, tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(home, f"docker: false\ntoken_path: {tok}\nca_path: \"\"\n")


@pytest.mark.parametrize("cwd,warns", [(None, "is unset"), ("", "is unset"), ("/tmp", "outside the persistent workspace"),
                                       ("/workspacex", "outside"), ("/workspace", None), ("/workspace/proj", None)])
def test_terminal_cwd_warning(_isolated_hermes_home, tmp_path, monkeypatch, caplog, cwd, warns):
    _ready_settings(_isolated_hermes_home, tmp_path)
    if cwd is None:
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
    else:
        monkeypatch.setenv("TERMINAL_CWD", cwd)
    p = KubernetesProvider()
    assert p.check_requirements({}) is True  # a warning, never a failure: commands still run
    row = [r for r in p.doctor_checks() if r[1] == "Kubernetes terminal.cwd"]
    assert len(row) == 1
    if warns:
        assert warns in caplog.text and "terminal.cwd" in caplog.text
        assert row[0][0] is False and warns in row[0][2]
    else:
        assert "terminal.cwd" not in caplog.text
        assert row[0][0] is True


def test_cwd_warning_is_logged_once_per_profile(_isolated_hermes_home, tmp_path, monkeypatch, caplog):
    _ready_settings(_isolated_hermes_home, tmp_path)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    p = KubernetesProvider()
    for _ in range(3):
        assert p.check_requirements({}) is True
    assert caplog.text.count("terminal.cwd is unset") == 1
    assert "terminal.cwd is unset" in p.doctor_checks()[-1][2]  # the doctor row is not deduplicated


def test_cwd_warning_is_silent_under_a_refusal_scope(monkeypatch):
    from tools import terminal_scope
    from kubernetes_sandbox.provider import cwd_warning
    token = terminal_scope.set_terminal_scope(terminal_scope.TerminalPolicyRefusal("unreadable"))
    try:
        assert cwd_warning() is None
    finally:
        terminal_scope.reset_terminal_scope(token)


def test_cwd_warning_reads_the_profile_scope_not_the_process_env(monkeypatch):
    """Multiplexed: the routed profile's terminal policy decides, not the gateway process's environment."""
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from kubernetes_sandbox.provider import cwd_warning
    monkeypatch.setenv("TERMINAL_CWD", "/tmp")
    token = set_terminal_scope({"TERMINAL_CWD": "/workspace"})
    try:
        assert cwd_warning() is None
    finally:
        reset_terminal_scope(token)
    assert "outside" in cwd_warning()
