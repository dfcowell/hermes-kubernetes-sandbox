"""A Pod-less stand-in for Hermes's system-prompt backend probe.

Hermes describes a remote backend in the system prompt by building a THROWAWAY environment with
``task_id="prompt-backend-probe"``, running ``uname`` in it with a 4 s timeout and tearing it down
(``agent/prompt_builder.py`` ``_run_backend_probe``, Hermes v0.22.1). It does this once per profile per
gateway process, before the first LLM call. For this backend that would mean a whole sandbox: a Pod
cold start (~40 s on Kata, blocking the prompt build), a workspace PVC that stays behind for the
janitor, and a second sandbox next to the session's own. Hermes passes ``probe_only=True`` for the
probe, but v0.22.1 drops it before it reaches a plugin provider (``tools/terminal_tool_backends.py``
``_build_plugin_env(..., **_)``), so the literal task_id is the only signal a plugin gets.

The stub answers the probe without touching the cluster: ``os=Linux``, ``kernel=unknown`` (Hermes
renders ``OS: Linux`` and skips the unknown kernel). The sandbox image is Linux by construction; the
kernel is not known without a Pod (and under Kata it is the guest's, not the node's).

A Hermes bump must re-check PROBE_TASK_ID against ``agent/prompt_builder.py``: if the literal changes,
the probe silently goes back to creating a real sandbox.
"""
from __future__ import annotations

PROBE_TASK_ID = "prompt-backend-probe"
PROBE_OUTPUT = "os=Linux\nkernel=unknown"


def is_backend_probe(task_id, kwargs) -> bool:
    """True for Hermes's prompt-time backend probe: its literal task_id, or ``probe_only`` once forwarded."""
    return task_id == PROBE_TASK_ID or bool(kwargs.get("probe_only"))


class ProbeEnvironment:
    """Duck-typed environment for the probe: the probe's uname command gets the static OS line; no Pod, no PVC."""

    def __init__(self, *, cwd: str, timeout: int):
        self.cwd, self.timeout = cwd, timeout

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        # Hermes's _BACKEND_PROBE_CMD is a printf of `uname -s` and `uname -r`. Anything else is not the probe
        # (a future Hermes reusing this env): fail it rather than fake a success.
        if "uname -s" in command and "uname -r" in command:
            return {"output": PROBE_OUTPUT, "returncode": 0}
        return {"output": "", "returncode": 1}

    def cleanup(self) -> None:
        return None
