"""Hermes terminal backend provider: `terminal.backend: kubernetes`."""
from __future__ import annotations

import logging
import pathlib

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .naming import HERMES_DEFAULT_CWD, WORKSPACE, Identity
from .probe import ProbeEnvironment, is_backend_probe
from .settings import SettingsError, interlock_error, load_settings

log = logging.getLogger(__name__)


def _home() -> pathlib.Path:
    from hermes_constants import get_hermes_home
    return pathlib.Path(get_hermes_home())


def _profile(home: pathlib.Path) -> str:
    try:
        from hermes_constants import profile_name_for_home
        return profile_name_for_home(home) or "default"
    except Exception:
        return "default"


_CWD_WARNED: set[str] = set()  # profiles already warned about their terminal.cwd


def _configured_cwd() -> str | None:
    """The profile's TERMINAL_CWD (terminal.cwd), "" when unset, None under a refusal scope (policy unreadable).

    Reads Hermes's profile-scoped terminal policy; only an import failure (an older Hermes without
    terminal_scope) falls back to the process env, as Hermes's own reader does."""
    try:
        from tools.terminal_scope import TerminalPolicyUnavailable, terminal_env
    except ImportError:
        import os
        return (os.environ.get("TERMINAL_CWD") or "").strip()
    try:
        return (terminal_env("TERMINAL_CWD", "") or "").strip()
    except TerminalPolicyUnavailable:
        return None


def cwd_warning() -> str | None:
    """Why the configured terminal.cwd is a poor fit for this backend, or None when it is fine."""
    cwd = _configured_cwd()
    if cwd is None:
        return None
    if not cwd:
        return (f"terminal.cwd is unset: Hermes then runs commands in {HERMES_DEFAULT_CWD}, which the sandbox "
                f"user cannot enter (the plugin maps it to {WORKSPACE}); set terminal.cwd: {WORKSPACE}")
    if cwd != WORKSPACE and not cwd.startswith(WORKSPACE + "/"):
        return (f"terminal.cwd is {cwd}, outside the persistent workspace {WORKSPACE}: files written relative "
                f"to it are lost with the Pod")
    return None


def _ca_problem(s) -> str | None:
    if s.ca_path and not pathlib.Path(s.ca_path).exists():
        return (f"ca_path {s.ca_path} does not exist (the API server would be verified against the "
                "system trust store instead); fix it, or set ca_path to an empty value to use system trust")
    return None


class KubernetesProvider(TerminalEnvironmentProvider):
    name = "kubernetes"
    display_name = "Kubernetes"
    is_remote = True
    is_container = True
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return "Run each session's commands in its own Kubernetes Pod (optional Docker sidecar)."

    @property
    def skip_container_guards(self) -> bool:
        # Hermes skips its dangerous-command approval prompts for backends it treats as isolated.
        # Keep them on: the session's workspace persists across Pods and the sandbox has
        # internet egress, so a destructive command here still has consequences.
        return False

    @property
    def env_description(self) -> str:
        return "a Kubernetes sandbox Pod (Linux)"

    def _settings(self):
        return load_settings(_home())

    def is_available(self) -> bool:
        try:  # env-only configuration (HERMES_K8S_*) is valid: the YAML file is optional
            s = self._settings()
            return pathlib.Path(s.token_path).exists()
        except Exception:
            return False

    def check_requirements(self, config) -> bool:
        try:
            s = self._settings()
        except SettingsError as e:
            log.error("kubernetes sandbox: %s", e)
            return False
        msg = interlock_error(s)
        if msg:
            log.error(msg)
            return False
        if not pathlib.Path(s.token_path).exists():
            log.error("kubernetes sandbox: no API token at %s", s.token_path)
            return False
        ca = _ca_problem(s)
        if ca:
            log.error("kubernetes sandbox: %s", ca)
            return False
        cw = cwd_warning()
        if cw:
            profile = _profile(_home())
            if profile not in _CWD_WARNED:  # once per profile: the check runs about once per turn
                _CWD_WARNED.add(profile)
                log.warning("kubernetes sandbox: %s", cw)  # a warning, not a failure: commands still run
        return True

    def probe(self):
        try:
            s = self._settings()
        except Exception as e:
            return ("needs_setup", str(e))
        msg = interlock_error(s)
        if msg:
            return ("needs_setup", msg)
        ca = _ca_problem(s)
        if ca:
            return ("needs_setup", ca)
        return ("ready", f"namespace {s.namespace}") if self.is_available() else (
            "needs_setup", f"no API token at {s.token_path}")

    def doctor_checks(self):
        rows = super().doctor_checks()
        cw = cwd_warning()
        rows.append((cw is None, "Kubernetes terminal.cwd", cw or f"({_configured_cwd()})"))
        return rows

    def setup_instructions(self):
        return ["Write $HERMES_HOME/kubernetes-sandbox.yaml (see examples/kata/README.md),",
                "mount a ServiceAccount token for the sandbox namespace, then: hermes config set terminal.backend kubernetes",
                f"and: hermes config set terminal.cwd {WORKSPACE} (Hermes's default, {HERMES_DEFAULT_CWD}, "
                "is not usable by the sandbox user)"]

    def create_environment(self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs):
        if is_backend_probe(task_id, kwargs):
            return ProbeEnvironment(cwd=cwd, timeout=timeout)  # no Pod, no PVC (kubernetes_sandbox/probe.py)
        from .environment import KubernetesEnvironment
        home = _home()
        s = self._settings()  # `image` (Hermes's per-backend image) is ignored: work_image is the setting
        return KubernetesEnvironment(settings=s, identity=Identity(_profile(home), task_id or "default"),
                                     cwd=cwd, timeout=timeout)  # it maps Hermes's default /root to /workspace
