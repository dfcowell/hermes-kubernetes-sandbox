"""Plugin settings: <HERMES_HOME>/kubernetes-sandbox.yaml, overridden by HERMES_K8S_* env vars."""
from __future__ import annotations

import dataclasses
import os
import pathlib
import re

import yaml


def _default_work_image() -> str:
    try:
        from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE  # newer Hermes
        return DEFAULT_SANDBOX_IMAGE
    except Exception:
        return "nousresearch/hermes-sandbox:desktop"


class SettingsError(ValueError):
    pass


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


_FIELDS = {f.name: f for f in dataclasses.fields(Settings)}
# Settings whose YAML key (and HERMES_K8S_<KEY> variable) differs from the field name.
_KEY_TO_FIELD = {"workspace_retention": "workspace_retention_seconds"}
_KEYS = {**{k: k for k in _FIELDS if k not in _KEY_TO_FIELD.values()}, **_KEY_TO_FIELD}
_DURATION = re.compile(r"^(\d+)([smhd]?)$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _annotations(raw) -> dict[str, str]:
    if isinstance(raw, str):  # HERMES_K8S_EXTRA_POD_ANNOTATIONS: JSON or YAML text
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise SettingsError(f"extra_pod_annotations is not valid JSON/YAML: {e}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise SettingsError("extra_pod_annotations must be a mapping of string keys to string values "
                            "(quote numbers and booleans)")
    return raw


def _duration(key: str, raw) -> int:
    """Seconds, from an int or `<n>s|m|h|d` ("7d"); bare digits are seconds."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    m = _DURATION.match(raw.strip()) if isinstance(raw, str) else None
    if not m:
        raise SettingsError(f"{key} must be a whole number of seconds or a duration like 90s, 15m, 36h, 7d; "
                            f"got {raw!r}")
    return int(m.group(1)) * _UNIT[m.group(2)]


def _coerce(name: str, raw):
    ftype = _FIELDS[name].type
    if name == "extra_pod_annotations":
        return _annotations(raw)
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
    return s


def load_settings(home: pathlib.Path) -> Settings:
    values: dict = {}
    path = pathlib.Path(home) / "kubernetes-sandbox.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
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
