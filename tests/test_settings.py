import pytest
from kubernetes_sandbox.settings import Settings, SettingsError, interlock_error, load_settings


def test_defaults_without_file(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    s = load_settings(_isolated_hermes_home)
    assert s.runtime_class_name is None
    assert s.namespace == "agent-sandbox"
    assert s.docker is False


def test_yaml_then_env_override(_isolated_hermes_home, monkeypatch):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        "runtime_class_name: kata\ndocker_disk_storage_class: fast-block\nmax_per_profile: 3\n")
    monkeypatch.setenv("HERMES_K8S_MAX_PER_PROFILE", "5")
    s = load_settings(_isolated_hermes_home)
    assert s.runtime_class_name == "kata"
    assert s.max_per_profile == 5


def test_docker_requires_block_storage_class(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text("runtime_class_name: kata\n")
    with pytest.raises(SettingsError, match="docker_disk_storage_class"):
        load_settings(_isolated_hermes_home)


def test_unknown_docker_cli_mode_rejected(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        "docker: false\ndocker_cli: copy-please\n")
    with pytest.raises(SettingsError, match="docker_cli"):
        load_settings(_isolated_hermes_home)


def test_unknown_key_rejected(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text("docker: false\nruntimeClass: kata\n")
    with pytest.raises(SettingsError, match="runtimeClass"):
        load_settings(_isolated_hermes_home)


def test_interlock_refuses_privileged_docker_without_runtime_class():
    msg = interlock_error(Settings(docker=True, docker_disk_storage_class="x"))
    assert msg and "root on the node" in msg and "allow_privileged_host_runtime" in msg


def test_interlock_allows_explicit_override_or_runtime_class_or_no_docker():
    assert interlock_error(Settings(docker=True, docker_disk_storage_class="x",
                                    allow_privileged_host_runtime=True)) is None
    assert interlock_error(Settings(docker=True, docker_disk_storage_class="x",
                                    runtime_class_name="kata")) is None
    assert interlock_error(Settings(docker=False)) is None
