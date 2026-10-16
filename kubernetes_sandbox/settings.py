"""Plugin settings: <HERMES_HOME>/kubernetes-sandbox.yaml, overridden by HERMES_K8S_* env vars."""
from __future__ import annotations

import dataclasses
import importlib.util
import ipaddress
import os
import pathlib
import re
import urllib.parse

# Tested against Hermes v0.22.1 (pinned in CI). On Hermes v0.22+ settings are parsed with Hermes's
# internal `hermes_yaml` module (ruamel.yaml; not a stable plugin API -- a rename is caught by the
# registration test at the next pin bump); on older Hermes, and in the examples tooling that renders
# Pods outside Hermes, with PyYAML. The choice is by availability, never by import failure: a
# hermes_yaml that is present but broken must fail with its own cause, not switch parsers.
# Both loaders reject duplicate keys.
if importlib.util.find_spec("hermes_yaml") is not None:
    from hermes_yaml import YAMLError, safe_load
elif importlib.util.find_spec("yaml") is not None:
    import yaml

    YAMLError = yaml.YAMLError

    class _UniqueKeySafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:  # unhashable key: the base constructor rejects it with its own error
                    break
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found duplicate key {key!r}", key_node.start_mark)
                seen.add(key)
            return super().construct_mapping(node, deep)

    def safe_load(text: str):
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
else:
    raise ImportError("kubernetes-sandbox needs a YAML loader: Hermes's hermes_yaml module (Hermes v0.22+) "
                      "or PyYAML (older Hermes); neither is installed")


def _default_work_image() -> str:
    try:
        from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE  # newer Hermes
        return DEFAULT_SANDBOX_IMAGE
    except Exception:
        return "nousresearch/hermes-sandbox:desktop"


class SettingsError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class GitCredential:
    """One forge for in-sandbox `git`: answered with `username` and the value of the forwarded `password_env`."""
    url: str
    username: str
    password_env: str


@dataclasses.dataclass(frozen=True)
class Settings:
    namespace: str = "agent-sandbox"
    runtime_class_name: str | None = None
    docker: bool = True
    allow_privileged_host_runtime: bool = False
    work_image: str = dataclasses.field(default_factory=_default_work_image)
    dind_image: str = "docker:29.8.1-dind"
    docker_cli: str = "image-volume"
    cli_image: str = "docker:29.8.1-cli"
    formatter_image: str = "debian:bookworm-slim"
    docker_disk_storage_class: str | None = None
    docker_disk_size: str = "30Gi"
    workspace_size: str = "10Gi"
    workspace_storage_class: str | None = None
    workspace_access_mode: str = "ReadWriteOncePod"
    workspace_retention_seconds: int = 604800
    work_cpu: str = "100m"
    work_memory: str = "512Mi"
    dind_cpu_request: str = "500m"
    dind_cpu_limit: str = "2"
    dind_memory: str = "1Gi"
    profile_configmap: str = "hermes-sandbox-profile"
    max_per_profile: int = 2
    ready_timeout_seconds: int = 180
    max_lifetime_seconds: int = 86400
    api_url: str = "https://kubernetes.default.svc"
    token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path: str | None = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    extra_pod_annotations: dict[str, str] = dataclasses.field(default_factory=dict)
    extra_pod_labels: dict[str, str] = dataclasses.field(default_factory=dict)
    forward_env: tuple[str, ...] = ()
    git_credentials: tuple[GitCredential, ...] = ()


_FIELDS = {f.name: f for f in dataclasses.fields(Settings)}
# Settings whose YAML key (and HERMES_K8S_<KEY> variable) differs from the field name.
_KEY_TO_FIELD = {"workspace_retention": "workspace_retention_seconds"}
_KEYS = {**{k: k for k in _FIELDS if k not in _KEY_TO_FIELD.values()}, **_KEY_TO_FIELD}
_DURATION = re.compile(r"(\d+)([smhd]?)")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
# Validators are applied with fullmatch: `$` would also accept a trailing newline.
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LABEL_NAME = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_PLUGIN_LABELS = ("app.kubernetes.io/managed-by", "app.kubernetes.io/component")
_PLUGIN_LABEL_PREFIX = "hermes-sandbox/"
# Characters a git_credentials url may never contain: it lands in a git config key and an env value.
_GIT_URL_BAD_CHAR = re.compile(r"[\s\"'\\\x00-\x1f\x7f]")
_GIT_USER = re.compile(r"[A-Za-z0-9._@+-]{1,128}")
# Variables a sandbox must never receive from the gateway: they change how the shell, the dynamic
# loader or Python start (BASH_ENV, LD_PRELOAD, PYTHONSTARTUP, ...) or point the sandbox at the
# gateway's cluster or Hermes itself; bash's readonly and special variables, whose assignment fails the
# runner's environment section (it fails closed, so every command would); and the runner's own `__hs_`
# variables; and LLM proxy and memory keys Hermes does not classify (WITHHELD_ENV_PREFIXES, also withheld
# per exec).
# Secrets Hermes knows about (provider API keys, GITHUB_TOKEN and the like) are withheld per exec by the
# environment, with Hermes's own credential check, not by this list.
_DENIED_ENV = frozenset({"BASH_ENV", "ENV", "PATH", "IFS", "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND",
                         "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "POSIXLY_CORRECT",
                         "UID", "EUID", "PPID", "BASHPID", "BASH_VERSINFO", "GROUPS", "BASH_ARGV0",
                         "BASH_SUBSHELL"})
# LiteLLM proxy keys (LITELLM_API_KEY, LITELLM_MASTER_KEY, ...) and Hindsight memory keys
# (HINDSIGHT_API_TENANT_API_KEY, ...): Hermes v0.22.1 does not classify them all (R-H20, R-H21).
WITHHELD_ENV_PREFIXES = ("LITELLM_", "HINDSIGHT_")
_DENIED_ENV_PREFIXES = ("LD_", "KUBERNETES_", "HERMES_", "__hs_", *WITHHELD_ENV_PREFIXES)


def _env_name(where: str, name) -> str:
    if not isinstance(name, str) or not _SHELL_NAME.fullmatch(name):
        raise SettingsError(f"{where} must be an environment variable name ([A-Za-z_][A-Za-z0-9_]*), "
                            f"got {name!r}")
    if name in _DENIED_ENV or name.startswith(_DENIED_ENV_PREFIXES):
        raise SettingsError(f"{where}: {name} cannot be forwarded into a sandbox (it changes how the "
                            "shell, loader or Python start, belongs to Kubernetes or Hermes, or is an LLM "
                            "or memory key)")
    return name


def _yaml_text(key: str, raw):
    """Environment overrides arrive as text: JSON or YAML."""
    if not isinstance(raw, str):
        return raw
    try:
        return safe_load(raw)
    except YAMLError as e:
        raise SettingsError(f"{key} is not valid JSON/YAML: {e}") from None


def _annotations(raw) -> dict[str, str]:
    raw = _yaml_text("extra_pod_annotations", raw)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise SettingsError("extra_pod_annotations must be a mapping of string keys to string values "
                            "(quote numbers and booleans)")
    return raw


def _labels(raw) -> dict[str, str]:
    raw = _yaml_text("extra_pod_labels", raw)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise SettingsError("extra_pod_labels must be a mapping of string keys to string values "
                            "(quote numbers and booleans)")
    for k, v in raw.items():
        prefix, _, name = k.rpartition("/")
        bad_prefix = prefix and (len(prefix) > 253 or not all(_DNS_LABEL.fullmatch(p) for p in prefix.split(".")))
        if bad_prefix or not _LABEL_NAME.fullmatch(name):
            raise SettingsError(f"extra_pod_labels: {k!r} is not a valid label key")
        if v and not _LABEL_NAME.fullmatch(v):
            raise SettingsError(f"extra_pod_labels: value {v!r} of {k!r} is not a valid label value")
        if k in _PLUGIN_LABELS or k.startswith(_PLUGIN_LABEL_PREFIX):
            raise SettingsError(f"extra_pod_labels: {k!r} is set by the plugin itself and cannot be overridden")
    return dict(raw)


def _forward_env(raw) -> tuple[str, ...]:
    if isinstance(raw, str) and not raw.strip().startswith("["):  # HERMES_K8S_FORWARD_ENV="A,B" or "A B"
        raw = [x for x in re.split(r"[\s,]+", raw.strip()) if x]
    raw = _yaml_text("forward_env", raw)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SettingsError("forward_env must be a list of environment variable names ([A-Za-z_][A-Za-z0-9_]*)")
    return tuple(dict.fromkeys(_env_name("forward_env", x) for x in raw))


def _git_url(url) -> str | None:
    """`https://<host>[:port]<path>` without a trailing slash, the host lower-cased (host names are
    case-insensitive, so duplicates are caught) and an IPv6 literal bracketed; None if not acceptable. Parsed
    with the standard library's urlsplit (no external dependency), then every part is checked explicitly."""
    if not isinstance(url, str) or _GIT_URL_BAD_CHAR.search(url) or not url.isascii():
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # ValueError: not a number, or outside 0-65535
    except ValueError:
        return None
    if (parts.scheme != "https" or not url.startswith("https://") or "@" in parts.netloc
            or "?" in url or "#" in url or parts.netloc.endswith(":") or port == 0):
        return None
    host = parts.hostname or ""
    if parts.netloc.startswith("["):
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            return None
    elif not all(_DNS_LABEL.fullmatch(label) for label in host.removesuffix(".").split(".")):
        return None
    if any(urllib.parse.unquote(seg) == ".." for seg in parts.path.split("/")):
        return None
    return f"https://{host}{f':{port}' if port is not None else ''}{parts.path}".rstrip("/")


def _git_credentials(raw) -> tuple[GitCredential, ...]:
    raw = _yaml_text("git_credentials", raw)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SettingsError("git_credentials must be a list of {url, username, password_env}")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"url", "username", "password_env"}:
            raise SettingsError(f"git_credentials[{i}] must have exactly the keys url, username, password_env")
        url, user, var = item["url"], item["username"], item["password_env"]
        norm = _git_url(url)
        if norm is None:
            raise SettingsError(f"git_credentials[{i}].url must be an https:// URL with a host, optional port and "
                                f"path, and no user, query, fragment, quotes, spaces or '..' segments, got {url!r}")
        if not isinstance(user, str) or not _GIT_USER.fullmatch(user):
            raise SettingsError(f"git_credentials[{i}].username {user!r} may contain only letters, digits and ._@+-")
        _env_name(f"git_credentials[{i}].password_env", var)
        out.append(GitCredential(norm, user, var))
    if len({c.url for c in out}) != len(out):
        raise SettingsError("git_credentials: each url may appear once")
    return tuple(out)


def _duration(key: str, raw) -> int:
    """Seconds, from an int or `<n>s|m|h|d` ("7d"); bare digits are seconds."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    m = _DURATION.fullmatch(raw.strip()) if isinstance(raw, str) else None
    if not m:
        raise SettingsError(f"{key} must be a whole number of seconds or a duration like 90s, 15m, 36h, 7d; "
                            f"got {raw!r}")
    return int(m.group(1)) * _UNIT[m.group(2)]


def _coerce(name: str, raw):
    ftype = _FIELDS[name].type
    if name == "extra_pod_annotations":
        return _annotations(raw)
    if name == "extra_pod_labels":
        return _labels(raw)
    if name == "forward_env":
        return _forward_env(raw)
    if name == "git_credentials":
        return _git_credentials(raw)
    if name == "workspace_retention_seconds":
        return _duration("workspace_retention", raw)
    if isinstance(raw, str):
        if ftype == "bool":
            v = raw.strip().lower()
            if v in _TRUE or v in _FALSE:
                return v in _TRUE
            raise SettingsError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {raw!r}")
        if ftype == "int":
            try:
                return int(raw)
            except ValueError:
                raise SettingsError(f"{name} must be an integer, got {raw!r}") from None
        if ftype.startswith("str | None") and raw.strip() == "":
            return None
    return raw


def validate(s: Settings) -> Settings:
    if s.docker_cli not in ("image-volume", "init-copy"):
        raise SettingsError(f"docker_cli must be 'image-volume' or 'init-copy', got {s.docker_cli!r}")
    if s.workspace_access_mode not in ("ReadWriteOncePod", "ReadWriteOnce"):
        raise SettingsError("workspace_access_mode must be 'ReadWriteOncePod' (one Pod at a time; needs CSI "
                            f"SINGLE_NODE_SINGLE_WRITER) or 'ReadWriteOnce' (one node), got {s.workspace_access_mode!r}")
    if s.docker and not s.docker_disk_storage_class:
        raise SettingsError("docker: true needs docker_disk_storage_class (a StorageClass that supports "
                            "volumeMode: Block; /var/lib/docker must not live on a shared filesystem)")
    for name in ("max_per_profile", "ready_timeout_seconds", "max_lifetime_seconds"):
        if getattr(s, name) <= 0:
            raise SettingsError(f"{name} must be positive")
    if s.workspace_retention_seconds <= 0:
        raise SettingsError("workspace_retention must be positive")
    for c in s.git_credentials:
        if c.password_env not in s.forward_env:
            raise SettingsError(f"git_credentials: password_env {c.password_env} for {c.url} must also be "
                                "listed in forward_env")
    return s


def load_settings(home: pathlib.Path) -> Settings:
    values: dict = {}
    path = pathlib.Path(home) / "kubernetes-sandbox.yaml"
    if path.exists():
        try:
            data = safe_load(path.read_text(encoding="utf-8"))
        except YAMLError as e:
            raise SettingsError(f"{path} is not valid YAML: {e}") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SettingsError(f"{path} must be a mapping of setting: value, got {type(data).__name__}")
        unknown = sorted(set(data) - set(_KEYS))
        if unknown:
            raise SettingsError(f"unknown setting(s) in {path}: {', '.join(unknown)}")
        values.update({_KEYS[k]: v for k, v in data.items()})
    for key, name in _KEYS.items():
        env = os.environ.get(f"HERMES_K8S_{key.upper()}")
        if env is not None:
            values[name] = env
    values = {k: _coerce(k, v) for k, v in values.items()}
    return validate(Settings(**values))


def interlock_error(s: Settings) -> str | None:
    if s.docker and not s.runtime_class_name and not s.allow_privileged_host_runtime:
        return ("kubernetes sandbox refused: docker: true runs a privileged Docker daemon in every sandbox, "
                "and without a VM-isolated runtime_class_name (e.g. Kata Containers) that daemon is root on "
                "the node — agents could read host disks and other pods' volumes. Set runtime_class_name, or "
                "docker: false, or (only if you accept that) allow_privileged_host_runtime: true.")
    return None
