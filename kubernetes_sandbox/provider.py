"""Hermes terminal backend provider: `terminal.backend: kubernetes`."""
from __future__ import annotations

import logging
import pathlib

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .naming import Identity
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
        # v1 forwards no credentials into the Pod, so approvals stay on (design K12).
        return False

    @property
    def env_description(self) -> str:
        return "a Kubernetes sandbox Pod (Linux)"

    def _settings(self):
        return load_settings(_home())

    def is_available(self) -> bool:
        try:
            if not (_home() / "kubernetes-sandbox.yaml").exists():
                return False
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
        return True

    def probe(self):
        try:
            s = self._settings()
        except Exception as e:
            return ("needs_setup", str(e))
        msg = interlock_error(s)
        if msg:
            return ("needs_setup", msg)
        return ("ready", f"namespace {s.namespace}") if self.is_available() else (
            "needs_setup", f"no API token at {s.token_path}")

    def setup_instructions(self):
        return ["Write $HERMES_HOME/kubernetes-sandbox.yaml (see examples/kata/README.md),",
                "mount a ServiceAccount token for the sandbox namespace, then: hermes config set terminal.backend kubernetes"]

    def create_environment(self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs):
        from .environment import KubernetesEnvironment
        home = _home()
        s = self._settings()
        if image:
            import dataclasses
            s = dataclasses.replace(s, work_image=image)
        return KubernetesEnvironment(settings=s, identity=Identity(_profile(home), task_id or "default"),
                                     cwd=cwd or "/workspace", timeout=timeout)
