"""Plugin settings: <HERMES_HOME>/kubernetes-sandbox.yaml, overridden by HERMES_K8S_* env vars."""
from __future__ import annotations

import dataclasses
import os
import pathlib

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
_TRUE = {"1", "true", "yes", "on"}


def _coerce(name: str, raw):
    ftype = _FIELDS[name].type
    if isinstance(raw, str):
        if ftype == "bool":
            return raw.strip().lower() in _TRUE
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
    if s.docker and not s.docker_disk_storage_class:
        raise SettingsError("docker: true needs docker_disk_storage_class (a StorageClass that supports "
                            "volumeMode: Block; /var/lib/docker must not live on a shared filesystem)")
    for name in ("max_per_profile", "ready_timeout_seconds", "max_lifetime_seconds"):
        if getattr(s, name) <= 0:
            raise SettingsError(f"{name} must be positive")
    return s


def load_settings(home: pathlib.Path) -> Settings:
    values: dict = {}
    path = pathlib.Path(home) / "kubernetes-sandbox.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        unknown = sorted(set(data) - set(_FIELDS))
        if unknown:
            raise SettingsError(f"unknown setting(s) in {path}: {', '.join(unknown)}")
        values.update(data)
    for name in _FIELDS:
        env = os.environ.get(f"HERMES_K8S_{name.upper()}")
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
