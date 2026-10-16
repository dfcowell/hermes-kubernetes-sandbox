import json
import pytest
from isolated import LOADERS, run_isolated
from kubernetes_sandbox.settings import GitCredential, Settings, SettingsError, interlock_error, load_settings


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


_DUPLICATES = r"""
import pathlib, sys, tempfile
from kubernetes_sandbox.settings import SettingsError, load_settings
for text in ["docker: false\nnamespace: a\nnamespace: b\n",
             "docker: false\nextra_pod_annotations: {team: a, team: b}\n"]:
    with tempfile.TemporaryDirectory() as home:
        pathlib.Path(home, "kubernetes-sandbox.yaml").write_text(text)
        try:
            load_settings(pathlib.Path(home))
        except SettingsError as e:
            print(" ".join(str(e).split()))
        else:
            print("ACCEPTED")
"""


@pytest.mark.parametrize("block", LOADERS)
def test_duplicate_keys_are_a_settings_error_with_either_loader(block):
    """A repeated key is almost always an edit gone wrong; neither loader may silently keep the last value."""
    r = run_isolated(_DUPLICATES, block=block)
    assert r.returncode == 0, r.stderr[-3000:]
    top, nested = r.stdout.splitlines()
    assert "not valid YAML" in top and "duplicate key" in top and "namespace" in top, top
    assert "not valid YAML" in nested and "duplicate key" in nested and "team" in nested, nested


def _load(home, text):
    (home / "kubernetes-sandbox.yaml").write_text("docker: false\n" + text)
    return load_settings(home)


def test_credential_settings_default_empty(_isolated_hermes_home):
    s = _load(_isolated_hermes_home, "")
    assert s.forward_env == () and s.extra_pod_labels == {} and s.git_credentials == ()


def test_credential_settings_from_yaml(_isolated_hermes_home):
    s = _load(_isolated_hermes_home,
              "forward_env: [FORGE_TOKEN, OTHER, FORGE_TOKEN]\n"
              "extra_pod_labels: {example.com/forge-egress: \"true\"}\n"
              "git_credentials:\n  - {url: \"https://forge.example.com/\", username: my-bot, password_env: FORGE_TOKEN}\n")
    assert s.forward_env == ("FORGE_TOKEN", "OTHER")
    assert s.extra_pod_labels == {"example.com/forge-egress": "true"}
    assert s.git_credentials == (GitCredential("https://forge.example.com", "my-bot", "FORGE_TOKEN"),)


def test_credential_settings_from_env(_isolated_hermes_home, monkeypatch):
    monkeypatch.setenv("HERMES_K8S_FORWARD_ENV", "A, B C")
    monkeypatch.setenv("HERMES_K8S_EXTRA_POD_LABELS", '{"example.com/x": "y"}')
    monkeypatch.setenv("HERMES_K8S_GIT_CREDENTIALS",
                       '[{"url": "https://f.example.com", "username": "u", "password_env": "A"}]')
    s = _load(_isolated_hermes_home, "")
    assert s.forward_env == ("A", "B", "C")
    assert s.extra_pod_labels == {"example.com/x": "y"}
    assert s.git_credentials == (GitCredential("https://f.example.com", "u", "A"),)


@pytest.mark.parametrize("names", ["[1BAD]", "[A-B]", "[\"A B\"]", "{A: 1}"])
def test_forward_env_rejects_non_names(_isolated_hermes_home, names):
    with pytest.raises(SettingsError, match="forward_env"):
        _load(_isolated_hermes_home, f"forward_env: {names}\n")


@pytest.mark.parametrize("labels,why", [
    ("{hermes-sandbox/profile: x}", "set by the plugin"),
    ("{app.kubernetes.io/managed-by: x}", "set by the plugin"),
    ("{app.kubernetes.io/component: x}", "set by the plugin"),
    ("{\"bad key!\": x}", "not a valid label key"),
    ("{example.com/ok: \"" + "v" * 64 + "\"}", "not a valid label value"),
    ("{example.com/ok: 1}", "string keys to string values"),
])
def test_extra_pod_labels_rejected(_isolated_hermes_home, labels, why):
    with pytest.raises(SettingsError, match=why):
        _load(_isolated_hermes_home, f"extra_pod_labels: {labels}\n")


@pytest.mark.parametrize("item,why", [
    ("{url: https://f.example.com, username: u}", "exactly the keys"),
    ("{url: http://f.example.com, username: u, password_env: T}", "https://"),
    ("{url: \"https://u@f.example.com\", username: u, password_env: T}", "https://"),
    ("{url: \"https://f.example.com/a b\", username: u, password_env: T}", "https://"),
    ("{url: https://f.example.com, username: \"u'x\", password_env: T}", "username"),
    ("{url: https://f.example.com, username: u, password_env: \"T X\"}", "password_env"),
])
def test_git_credentials_rejected(_isolated_hermes_home, item, why):
    with pytest.raises(SettingsError, match=why):
        _load(_isolated_hermes_home, f"git_credentials: [{item}]\n")


def test_git_credentials_url_once(_isolated_hermes_home):
    with pytest.raises(SettingsError, match="each url may appear once"):
        _load(_isolated_hermes_home,
              "git_credentials:\n  - {url: https://f.example.com, username: a, password_env: T}\n"
              "  - {url: \"https://f.example.com/\", username: b, password_env: U}\n")


@pytest.mark.parametrize("text,why", [
    ('forward_env: ["A\\n"]\n', "forward_env"),
    ('extra_pod_labels: {"x\\n": a}\n', "not a valid label key"),
    ('extra_pod_labels: {"example.com\\n/x": a}\n', "not a valid label key"),
    ('extra_pod_labels: {x: "a\\n"}\n', "not a valid label value"),
    ('git_credentials: [{url: "https://h\\n", username: u, password_env: T}]\n', "https://"),
    ('git_credentials: [{url: "https://h", username: "u\\n", password_env: T}]\n', "username"),
    ('git_credentials: [{url: "https://h", username: u, password_env: "T\\n"}]\n', "password_env"),
])
def test_trailing_newline_rejected(_isolated_hermes_home, text, why):
    with pytest.raises(SettingsError, match=why):
        _load(_isolated_hermes_home, text)


@pytest.mark.parametrize("name", ["LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "PATH", "IFS", "SHELLOPTS",
                                  "BASHOPTS", "PS4", "PROMPT_COMMAND", "PYTHONPATH", "PYTHONSTARTUP",
                                  "PYTHONHOME", "KUBERNETES_SERVICE_HOST", "HERMES_HOME", "HERMES_K8S_API_URL",
                                  # the runner's own variables (R-H18)
                                  "__hs_env", "__hs_anything",
                                  # bash readonly/special names: assigning one fails the runner's eval (R-H18)
                                  "UID", "EUID", "PPID", "BASHPID", "BASH_VERSINFO", "GROUPS", "BASH_ARGV0",
                                  "BASH_SUBSHELL", "POSIXLY_CORRECT",
                                  # LiteLLM proxy keys, which Hermes does not classify (R-H20)
                                  "LITELLM_API_KEY", "LITELLM_MASTER_KEY",
                                  # Hindsight memory keys, which Hermes does not classify (R-H21)
                                  "HINDSIGHT_API_TENANT_API_KEY", "HINDSIGHT_API_KEY"])
def test_denied_env_names(_isolated_hermes_home, name):
    with pytest.raises(SettingsError, match=f"forward_env: {name} cannot be forwarded"):
        _load(_isolated_hermes_home, f"forward_env: [{name}]\n")
    with pytest.raises(SettingsError, match=rf"git_credentials\[0\].password_env: {name} cannot be forwarded"):
        _load(_isolated_hermes_home, f"git_credentials: [{{url: https://h, username: u, password_env: {name}}}]\n")


def test_git_credential_password_env_must_be_forwarded(_isolated_hermes_home):
    with pytest.raises(SettingsError, match="password_env FORGE_TOKEN .* forward_env"):
        _load(_isolated_hermes_home, "forward_env: [OTHER]\n"
              "git_credentials: [{url: https://h, username: u, password_env: FORGE_TOKEN}]\n")


def test_label_key_prefix_dns_label_max_63(_isolated_hermes_home):
    assert _load(_isolated_hermes_home, f"extra_pod_labels: {{{'a' * 63}.example.com/x: v}}\n").extra_pod_labels
    with pytest.raises(SettingsError, match="not a valid label key"):
        _load(_isolated_hermes_home, f"extra_pod_labels: {{{'a' * 64}.example.com/x: v}}\n")


def test_git_credentials_host_case_insensitive(_isolated_hermes_home):
    s = _load(_isolated_hermes_home, "forward_env: [T]\n"
              "git_credentials: [{url: https://Forge.Example.COM/Org, username: u, password_env: T}]\n")
    assert s.git_credentials == (GitCredential("https://forge.example.com/Org", "u", "T"),)
    with pytest.raises(SettingsError, match="each url may appear once"):
        _load(_isolated_hermes_home,
              "git_credentials:\n  - {url: https://h.example.com, username: a, password_env: T}\n"
              "  - {url: \"https://H.example.com/\", username: b, password_env: U}\n")


@pytest.mark.parametrize("url", ["https://h/..", "https://h/a/../b", "https://h/../"])
def test_git_credentials_dotdot_rejected(_isolated_hermes_home, url):
    with pytest.raises(SettingsError, match=r"'\.\.' segments"):
        _load(_isolated_hermes_home, f"git_credentials: [{{url: \"{url}\", username: u, password_env: T}}]\n")


def _git_url(home, url):
    quoted = json.dumps(url).replace("\x7f", "\\x7f")  # a YAML double-quoted scalar; YAML forbids a raw DEL
    return _load(home, "forward_env: [T]\n"
                 f"git_credentials: [{{url: {quoted}, username: u, password_env: T}}]\n").git_credentials[0].url


@pytest.mark.parametrize("url,normalised", [
    ("https://[2001:DB8::1]:3000/org/", "https://[2001:db8::1]:3000/org"),  # IPv6 literal, bracketed
    ("https://[::1]", "https://[::1]"),
    ("https://Forge.Example.com:443/x", "https://forge.example.com:443/x"),  # explicit :443 kept
    ("https://h.example.com/org%20x/r%C3%A9po", "https://h.example.com/org%20x/r%C3%A9po"),  # percent-encoding kept
    ("https://h.example.com./org", "https://h.example.com./org"),  # trailing-dot (absolute) host kept
    ("https://h.example.com:65535", "https://h.example.com:65535"),
])
def test_git_credentials_url_normalised(_isolated_hermes_home, url, normalised):
    assert _git_url(_isolated_hermes_home, url) == normalised


@pytest.mark.parametrize("url", [
    "https://u:p@h.example.com", "https://@h.example.com", "https://u@[::1]/x",  # userinfo
    "https://h.example.com/x?a=1", "https://h.example.com/?", "https://h.example.com/x#f", "https://h#",  # query/fragment
    "https://h.example.com:99999", "https://h.example.com:0", "https://h.example.com:x", "https://h.example.com:",
    "https://h.example.com:-1", "https://[::1]:abc",  # bad port
    "https://", "https:///x", "https://h_x.example.com", "https://h..example.com", "https://[zz::1]",  # bad host
    "https://h.example.com/a\tb", "https://h.example.com/a\"b", "https://h.example.com/a'b",
    "https://h.example.com/a\\b", "https://h.example.com/\x7f", "https://h.example.com/a\u00a0b",  # chars
    "HTTPS://h.example.com", "git://h.example.com", "https:h.example.com",  # scheme
    "https://h.example.com/%2e%2e/x",  # encoded '..'
])
def test_git_credentials_url_rejected(_isolated_hermes_home, url):
    with pytest.raises(SettingsError, match=r"git_credentials\[0\]\.url"):
        _git_url(_isolated_hermes_home, url)


def test_forward_env_bare_scalar(_isolated_hermes_home):
    assert _load(_isolated_hermes_home, "forward_env: FOO\n").forward_env == ("FOO",)
