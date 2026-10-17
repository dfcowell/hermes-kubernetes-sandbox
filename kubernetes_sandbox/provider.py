"""Hermes terminal backend provider: `terminal.backend: kubernetes`."""
from __future__ import annotations

import logging
import os
import pathlib

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .naming import WORKSPACE, Identity
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
_DEFAULT_CWD_UNKNOWN: set[str] = set()  # profiles already told the default-cwd mapping is off


def _configured_cwd() -> str | None:
    """The profile's TERMINAL_CWD (terminal.cwd), "" when unset, None under a refusal scope (policy unreadable).

    Reads Hermes's profile-scoped terminal policy; only an import failure (an older Hermes without
    terminal_scope) falls back to the process env, as Hermes's own reader does."""
    try:
        from tools.terminal_scope import TerminalPolicyUnavailable, terminal_env
    except ImportError:
        return (os.environ.get("TERMINAL_CWD") or "").strip()
    try:
        return (terminal_env("TERMINAL_CWD", "") or "").strip()
    except TerminalPolicyUnavailable:
        return None


def _unmapped(reason: str) -> None:
    profile = _profile(_home())
    if profile not in _DEFAULT_CWD_UNKNOWN:  # once per profile: it is asked at every environment creation
        _DEFAULT_CWD_UNKNOWN.add(profile)
        log.warning("kubernetes sandbox: %s; Hermes's default cwd is not mapped to %s (set terminal.cwd: %s)",
                    reason, WORKSPACE, WORKSPACE)


def _resolve_with_hermes(env_type: str) -> tuple[str | None, str]:
    """(Hermes's own planned cwd for *env_type* from the profile's terminal.cwd, why not), the cwd unstripped.

    None when the resolver is unobtainable or returns something other than its (cwd, host_cwd) tuple."""
    try:
        from tools.terminal_tool import _resolve_config_cwd
        result = _resolve_config_cwd(env_type, False)
    except Exception as e:  # ImportError (a renamed resolver), TerminalPolicyUnavailable, a changed signature
        log.debug("kubernetes sandbox: Hermes's cwd resolver is unavailable: %r", e)
        return None, "Hermes's cwd resolver is unavailable"
    if not isinstance(result, tuple) or not result or not isinstance(result[0], str):
        log.debug("kubernetes sandbox: Hermes's cwd resolver returned %r", type(result))
        return None, "Hermes's cwd resolver returned an unexpected value"
    return result[0], ""


def hermes_default_cwd(env_type: str) -> str | None:
    """The cwd Hermes plans for this backend in place of terminal.cwd (unset, or unusable in a container),
    or None when Hermes is using the configured terminal.cwd or the default cannot be known.

    Asked of Hermes itself, never copied: its own resolver (tools/terminal_tool.py _resolve_config_cwd, which
    reads TERMINAL_CWD through the profile's terminal scope; read-only, it only logs when it discards an
    unusable terminal.cwd). Without a usable answer (a refusal scope, a renamed resolver, an unexpected return
    value, an empty terminal.cwd) it does not guess: None, logged once per profile. The cwd Hermes passes at
    creation is never taken as the default: it can be an ACP/gateway override or the session's recorded cwd."""
    configured = _configured_cwd()  # stripped
    if configured is None:
        _unmapped("the profile's terminal policy is unreadable")
        return None
    resolved, why = _resolve_with_hermes(env_type)
    if resolved is None:
        _unmapped(why)
        return None
    resolved = resolved.strip()  # compared stripped, like the configured side
    if not resolved:
        _unmapped("terminal.cwd is set but empty, so Hermes resolves no cwd")
        return None
    if configured and resolved == os.path.expanduser(configured):
        return None  # Hermes uses terminal.cwd as given: there is no default in play
    return resolved


def cwd_warning(env_type: str = "kubernetes") -> str | None:
    """Why the configured terminal.cwd is a poor fit for this backend, or None when it is fine."""
    cwd = _configured_cwd()
    if cwd is None:
        return None
    if not cwd:
        default = (_resolve_with_hermes(env_type)[0] or "").strip()
        return (f"terminal.cwd is unset: Hermes then runs commands in its default cwd"
                f"{f' ({default})' if default else ''}, which the sandbox user cannot enter (the plugin maps it "
                f"to {WORKSPACE}); set terminal.cwd: {WORKSPACE}")
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
                f"and: hermes config set terminal.cwd {WORKSPACE} (Hermes's default cwd for this backend "
                "is not usable by the sandbox user)"]

    def create_environment(self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs):
        if is_backend_probe(task_id, kwargs):
            return ProbeEnvironment(cwd=cwd, timeout=timeout)  # no Pod, no PVC (kubernetes_sandbox/probe.py)
        from .environment import KubernetesEnvironment
        home = _home()
        s = self._settings()  # `image` (Hermes's per-backend image) is ignored: work_image is the setting
        return KubernetesEnvironment(settings=s, identity=Identity(_profile(home), task_id or "default"),
                                     cwd=cwd, timeout=timeout,
                                     default_cwd=hermes_default_cwd(self.name))  # mapped to /workspace
