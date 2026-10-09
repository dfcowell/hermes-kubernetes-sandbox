from kubernetes_sandbox.provider import KubernetesProvider


def _write(home, text):
    (home / "kubernetes-sandbox.yaml").write_text(text)


def test_attributes():
    p = KubernetesProvider()
    assert p.name == "kubernetes" and p.is_remote and p.is_container
    assert p.skip_container_guards is False
    assert p.session_isolated_when_nonpersistent is True


def test_is_available_needs_settings_and_token_without_network(_isolated_hermes_home, tmp_path):
    p = KubernetesProvider()
    assert p.is_available() is False  # no settings file
    tok = tmp_path / "tok"
    tok.write_text("x")
    _write(_isolated_hermes_home, f"docker: false\ntoken_path: {tok}\n")
    assert p.is_available() is True


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
