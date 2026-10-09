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


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
def test_non_mapping_yaml_is_a_settings_error(_isolated_hermes_home, text):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(text)
    with pytest.raises(SettingsError, match="must be a mapping"):
        load_settings(_isolated_hermes_home)


def test_invalid_yaml_is_a_settings_error(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text("docker: [false\n")
    with pytest.raises(SettingsError, match="kubernetes-sandbox.yaml"):
        load_settings(_isolated_hermes_home)


def test_extra_pod_annotations_from_env(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    monkeypatch.setenv("HERMES_K8S_EXTRA_POD_ANNOTATIONS", '{"io.katacontainers.config.hypervisor.default_vcpus": "2"}')
    s = load_settings(_isolated_hermes_home)
    assert s.extra_pod_annotations == {"io.katacontainers.config.hypervisor.default_vcpus": "2"}
    monkeypatch.setenv("HERMES_K8S_EXTRA_POD_ANNOTATIONS", "a: b\n")
    assert load_settings(_isolated_hermes_home).extra_pod_annotations == {"a": "b"}


@pytest.mark.parametrize("raw", ["[1, 2]", "{a: 1}", "{a: [x]}", "{a: b", "plain"])
def test_extra_pod_annotations_must_be_a_string_mapping(_isolated_hermes_home, monkeypatch, raw):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    monkeypatch.setenv("HERMES_K8S_EXTRA_POD_ANNOTATIONS", raw)
    with pytest.raises(SettingsError, match="extra_pod_annotations"):
        load_settings(_isolated_hermes_home)


def test_extra_pod_annotations_yaml_values_checked(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text("docker: false\nextra_pod_annotations: {a: 1}\n")
    with pytest.raises(SettingsError, match="extra_pod_annotations"):
        load_settings(_isolated_hermes_home)


@pytest.mark.parametrize("raw,val", [("1", True), ("TRUE", True), ("on", True), ("0", False), ("no", False),
                                     ("Off", False), ("false", False)])
def test_bool_env_values(_isolated_hermes_home, monkeypatch, raw, val):
    monkeypatch.setenv("HERMES_K8S_DOCKER", raw)
    monkeypatch.setenv("HERMES_K8S_DOCKER_DISK_STORAGE_CLASS", "b")
    assert load_settings(_isolated_hermes_home).docker is val


def test_unrecognised_bool_env_value_raises(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "flase")
    with pytest.raises(SettingsError, match="docker"):
        load_settings(_isolated_hermes_home)


@pytest.mark.parametrize("yaml_value,seconds", [("604800", 604800), ("90", 90), ("7d", 604800), ("36h", 129600),
                                               ("15m", 900), ("45s", 45), ("'7d'", 604800)])
def test_workspace_retention_yaml(_isolated_hermes_home, yaml_value, seconds):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        f"docker: false\nworkspace_retention: {yaml_value}\n")
    assert load_settings(_isolated_hermes_home).workspace_retention_seconds == seconds


def test_workspace_retention_default_is_seven_days(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    assert load_settings(_isolated_hermes_home).workspace_retention_seconds == 604800
    assert Settings().workspace_retention_seconds == 604800


@pytest.mark.parametrize("raw,seconds", [("36h", 129600), ("2d", 172800), ("3600", 3600)])
def test_workspace_retention_env_override(_isolated_hermes_home, monkeypatch, raw, seconds):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text("docker: false\nworkspace_retention: 7d\n")
    monkeypatch.setenv("HERMES_K8S_WORKSPACE_RETENTION", raw)
    assert load_settings(_isolated_hermes_home).workspace_retention_seconds == seconds


@pytest.mark.parametrize("yaml_value", ["0", "0d", "-5", "7w", "abc", "true", "1.5h", "''", "[7]"])
def test_workspace_retention_invalid(_isolated_hermes_home, yaml_value):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        f"docker: false\nworkspace_retention: {yaml_value}\n")
    with pytest.raises(SettingsError, match="workspace_retention"):
        load_settings(_isolated_hermes_home)


def test_workspace_retention_invalid_env(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_DOCKER", "false")
    monkeypatch.setenv("HERMES_K8S_WORKSPACE_RETENTION", "soon")
    with pytest.raises(SettingsError, match="workspace_retention"):
        load_settings(_isolated_hermes_home)


def test_workspace_retention_seconds_is_not_a_yaml_key(_isolated_hermes_home):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        "docker: false\nworkspace_retention_seconds: 60\n")
    with pytest.raises(SettingsError, match="unknown setting"):
        load_settings(_isolated_hermes_home)


def test_workspace_storage_class_yaml_env_and_empty(_isolated_hermes_home, monkeypatch):
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        "docker: false\nworkspace_storage_class: fast-fs\n")
    assert load_settings(_isolated_hermes_home).workspace_storage_class == "fast-fs"
    monkeypatch.setenv("HERMES_K8S_WORKSPACE_STORAGE_CLASS", "other")
    assert load_settings(_isolated_hermes_home).workspace_storage_class == "other"
    monkeypatch.setenv("HERMES_K8S_WORKSPACE_STORAGE_CLASS", "")
    assert load_settings(_isolated_hermes_home).workspace_storage_class is None
    assert Settings().workspace_storage_class is None


def test_workspace_access_mode_default_yaml_env_and_invalid(_isolated_hermes_home, monkeypatch):
    assert Settings().workspace_access_mode == "ReadWriteOncePod"
    (_isolated_hermes_home / "kubernetes-sandbox.yaml").write_text(
        "docker: false\nworkspace_access_mode: ReadWriteOnce\n")
    assert load_settings(_isolated_hermes_home).workspace_access_mode == "ReadWriteOnce"
    monkeypatch.setenv("HERMES_K8S_WORKSPACE_ACCESS_MODE", "ReadWriteOncePod")
    assert load_settings(_isolated_hermes_home).workspace_access_mode == "ReadWriteOncePod"
    for bad in ("ReadWriteMany", "rwop", "ReadOnlyMany"):
        monkeypatch.setenv("HERMES_K8S_WORKSPACE_ACCESS_MODE", bad)
        with pytest.raises(SettingsError, match="workspace_access_mode"):
            load_settings(_isolated_hermes_home)
