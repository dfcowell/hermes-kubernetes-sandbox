"""Hermes BaseEnvironment backed by one Kubernetes Pod per session."""
from __future__ import annotations

import atexit
import datetime as dt
import json
import logging
import os
import shlex
import socket
import threading
import time
import uuid

import httpx
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError, get_activity_callback
from tools.environments.remote_common import resolve_passthrough_env

from .credentials import credential_names, git_config_env
from .exec_stream import ExecDisconnected, ExecFailed, StreamingExecHandle, connect_exec
from .kube import Conflict, KubeClient, KubeError, NotFound, QuotaExceeded
from .naming import (ANN_LAST_USED, ANN_SESSION_KEY, ANN_UNUSED_SINCE, LBL_PROFILE,
                     LBL_TEMPLATE_HASH, MANAGED_BY, WORKSPACE, Identity, slug)
from .podspec import identity_labels, render_pod, render_workspace_claim, template_hash
from .runner import exec_argv, kill_argv, stdin_payload
from .settings import _SHELL_NAME, WITHHELD_ENV_PREFIXES, Settings, interlock_error

log = logging.getLogger(__name__)
_PROCESS_EXITING = False


def _mark_exiting():
    global _PROCESS_EXITING
    _PROCESS_EXITING = True


atexit.register(_mark_exiting)  # registered after Hermes's own hooks, so it runs before them (LIFO)

_LAST_USED_EVERY = 60.0

# The single-Pod guarantee on the workspace is the PVC's ReadWriteOncePod access mode (enforced by
# the scheduler and the kubelet). ReadWriteOnce is per NODE: two Pods on one node can both mount it,
# and only this graceful replace-then-wait guards against that. Either way every plugin-side Pod
# delete is graceful: a force delete (grace 0) removes the API object while the old containers
# still run and hold the volume, so a same-name Pod could co-mount it (RWO) or never be gone in
# time. Re-issuing this delete on a Pod a drain/eviction is already deleting (30 s grace) shortens
# that grace to 10 s; harmless for the volume, since the Pod object outlives the unmount.
_DELETE_GRACE = 10
# How long to wait for a deleted Pod (or PVC) to be gone: the grace period, plus Kata VM teardown
# and the volume unmount/detach that must finish before the API object disappears.
_GONE_TIMEOUT = 90

# Pod name -> uid of the Pods this process has served. Hermes reaches env.cleanup() on gateway stop and on degraded
# eviction as well as from its idle reaper, and a fresh env then starts with no memory of the old
# Pod: creating a known name is a recreate, never a silent workspace reset.
_SERVED_PODS: dict[str, str | None] = {}
_SERVED_LOCK = threading.Lock()
_ACCESS_WARNED: set[str] = set()  # workspace PVCs already warned about an access-mode mismatch
_WITHHELD_WARNED: set[str] = set()  # names already warned about as withheld credentials


def _withheld(name: str) -> bool:
    """A credential that never enters a sandbox: a name Hermes manages (its own check, the one its env
    passthrough registry applies: provider and tool API keys, GITHUB_TOKEN/GH_TOKEN, adapter secrets,
    AUXILIARY_*_API_KEY ...), or an LLM proxy or memory key Hermes does not classify (WITHHELD_ENV_PREFIXES).
    Hermes lets an EXPLICIT forward (docker_forward_env) bypass its check; this plugin does not. Fails closed: a
    name that cannot be classified is withheld, logged every time, and never marked as warned."""
    if name.startswith(WITHHELD_ENV_PREFIXES):
        reason = "is an LLM proxy or memory key"
    else:
        try:
            from tools.env_passthrough import _is_hermes_provider_credential
            if not _is_hermes_provider_credential(name):
                return False
        except Exception as e:
            log.warning("forwarded environment: cannot classify %s with Hermes's credential check (%s); "
                        "withheld", name, type(e).__name__)
            return True
        reason = "is a credential Hermes manages"
    if name not in _WITHHELD_WARNED:
        _WITHHELD_WARNED.add(name)
        log.warning("forwarded environment: %s %s; it is not forwarded into sandboxes", name, reason)
    return True


def _served(name: str) -> bool:
    with _SERVED_LOCK:
        return name in _SERVED_PODS


def _served_uid(name: str) -> str | None:
    with _SERVED_LOCK:
        return _SERVED_PODS.get(name)


def _remember(name: str, uid: str | None) -> None:
    with _SERVED_LOCK:
        _SERVED_PODS[name] = uid


# JSONDecodeError: a 2xx whose body is not JSON (a proxy's HTML page). Its message carries only the position.
_API_ERRORS = (KubeError, httpx.HTTPError, OSError, json.JSONDecodeError)


def _retryable(e: BaseException) -> bool:
    """Transport failures, 5xx and 429 may pass; 4xx client errors will not."""
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, KubeError) and (e.status >= 500 or e.status == 429)


def _api_error(what: str, e: BaseException) -> EnvironmentConnectionError:
    """Status and reason only: never request headers or the token."""
    if isinstance(e, KubeError):
        detail = str(e)  # "<status> <reason>: <API message>"
    elif isinstance(e, OSError):
        detail = f"{type(e).__name__}: {e.strerror or e} ({e.filename})" if e.filename else f"{type(e).__name__}: {e}"
    else:
        detail = f"{type(e).__name__}: {e}"
    return EnvironmentConnectionError(f"{what}: Kubernetes API error {detail}"[:500])


def _now_iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ready(pod: dict) -> bool:
    st = pod.get("status") or {}
    return st.get("phase") == "Running" and any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])


def _live(pod: dict) -> bool:
    """Not finished and not terminating: counts against capacity, can be adopted."""
    return ((pod.get("status") or {}).get("phase") not in ("Failed", "Succeeded")
            and not (pod.get("metadata") or {}).get("deletionTimestamp"))


def _uid(pod: dict) -> str | None:
    return (pod.get("metadata") or {}).get("uid")


def _default_exec_connect(client):
    def run(pod, container, argv, payload, on_kill):
        return StreamingExecHandle(lambda: connect_exec(client, pod, container, argv), payload, on_kill)
    return run


class KubernetesEnvironment(BaseEnvironment):
    _stdin_mode = "pipe"
    _snapshot_timeout = 60

    def __init__(self, *, settings: Settings, identity: Identity, cwd: str, timeout: int, default_cwd: str | None = None,
                 client=None, exec_connect=None, clock=time.time, sleep=time.sleep):
        # default_cwd: the cwd Hermes plans when terminal.cwd is unset (provider.hermes_default_cwd asks Hermes's
        # own resolver; /root in v0.22.1), which the work container's user (UID 1000) cannot enter: start in the
        # workspace instead (see also _wrap_command). None: nothing is mapped.
        self._hermes_default_cwd = default_cwd or None
        super().__init__(cwd=cwd if cwd and cwd != self._hermes_default_cwd else WORKSPACE, timeout=timeout)
        # Read by Hermes's is_persistent_env(): the Pod lives for the session, not the turn.
        self._session_scoped = True
        self._initial_cwd = self.cwd
        self._s, self._id = settings, identity
        self._client = client or KubeClient(settings)
        self._exec = exec_connect or _default_exec_connect(self._client)
        self._clock, self._sleep = clock, sleep
        self._pod_ready = False
        self._last_touch = 0.0
        self._lock = threading.RLock()
        self._had_pod = False
        self._pod_uid: str | None = None
        self._bootstrap_attempted = False
        # Names whose values are forwarded per exec (K11). Monotonic: a name stays excluded from the session
        # snapshot after its value is gone, so an old value cannot persist there.
        self._exec_env_names: set[str] = set(settings.forward_env) | credential_names(settings.git_credentials)

    # --- Pod lifecycle -------------------------------------------------------
    def _notify(self, msg: str) -> None:
        cb = get_activity_callback()
        if cb:
            try:
                cb(msg)
            except Exception:
                pass

    def _matches(self, pod: dict) -> bool:
        md = pod.get("metadata") or {}
        return ((md.get("annotations") or {}).get(ANN_SESSION_KEY) == self._id.session_key
                and (md.get("labels") or {}).get(LBL_TEMPLATE_HASH) == template_hash(self._s)
                and _live(pod))

    def _wait_gone(self, name: str, get=None) -> bool:
        get = get or self._client.get_pod
        deadline = self._clock() + _GONE_TIMEOUT
        while self._clock() < deadline:
            try:
                get(name)
            except NotFound:
                return True
            self._sleep(1)
        return False

    def _owns_claim(self, pvc: dict) -> bool:
        md = pvc.get("metadata") or {}
        labels = md.get("labels") or {}
        return (all(labels.get(k) == v for k, v in identity_labels(self._id).items())
                and (md.get("annotations") or {}).get(ANN_SESSION_KEY) == self._id.session_key)

    def _ensure_workspace(self) -> None:
        """Get or create the session's workspace PVC, which outlives every Pod of the session.
        A PVC being deleted (the janitor reaped it) is waited out and replaced by an empty one."""
        name = self._id.workspace_claim
        for _attempt in range(3):
            try:
                pvc = self._client.get_pvc(name)
            except NotFound:
                try:
                    self._client.create_pvc(render_workspace_claim(self._s, self._id))
                    return
                except Conflict:
                    continue  # created meanwhile (another gateway): GET and adopt it
                except QuotaExceeded as e:
                    raise EnvironmentConnectionError(
                        f"Sandbox capacity exhausted in namespace {self._s.namespace}: {e}",
                        retry_hint="The namespace's PVC count / storage quota is full; it frees when an idle "
                                   "workspace expires (workspace_retention). Retry later, or ask the operator "
                                   "to raise the quota.") from None
            if not (pvc.get("metadata") or {}).get("deletionTimestamp") and self._owns_claim(pvc):
                pvc = self._unstamp(pvc)
            if pvc is None:
                continue  # deleted between GET and PATCH
            if (pvc.get("metadata") or {}).get("deletionTimestamp"):
                self._notify("waiting for the expired workspace volume to be deleted")
                if not self._wait_gone(name, self._client.get_pvc):
                    raise EnvironmentConnectionError(
                        f"Workspace volume {name} is still being deleted",
                        retry_hint="Retry in a minute; a fresh workspace volume is created once it is gone.")
                continue
            if not self._owns_claim(pvc):
                raise EnvironmentConnectionError(
                    f"Refusing to mount workspace volume {name}: its labels or session key do not match "
                    f"session {self._id.session_key!r} of profile {self._id.profile!r}",
                    retry_hint="An operator must delete or rename that PVC; retrying will not help.")
            self._check_access_mode(pvc)
            return
        raise EnvironmentConnectionError(f"Could not create workspace volume {name}: it keeps changing",
                                         retry_hint="Retry the command.")

    def _check_access_mode(self, pvc: dict) -> None:
        """Access modes are immutable: an existing workspace is adopted as it is (never deleted or
        recreated over a setting change), with one warning per volume per process."""
        modes = (pvc.get("spec") or {}).get("accessModes") or []
        name = self._id.workspace_claim
        if modes != [self._s.workspace_access_mode] and name not in _ACCESS_WARNED:
            _ACCESS_WARNED.add(name)
            log.warning("workspace volume %s has access mode %s, not workspace_access_mode=%s; using it as it "
                        "is (access modes are immutable; new workspaces get the setting)",
                        name, ",".join(modes) or "unset", self._s.workspace_access_mode)

    def _unstamp_adopted(self) -> None:
        """Adopting a Ready Pod (e.g. a crashed gateway's): clear the idle stamp too, in case the
        janitor never sees this Pod mounted. Best effort, except a missing RBAC verb."""
        try:
            pvc = self._client.get_pvc(self._id.workspace_claim)
            if not (pvc.get("metadata") or {}).get("deletionTimestamp") and self._owns_claim(pvc):
                self._unstamp(pvc)
        except _API_ERRORS:
            pass

    def _unstamp(self, pvc: dict) -> dict | None:
        """Clear the janitor's idle stamp before every Pod start, so a short session between two
        janitor runs still restarts the retention clock. Patching also changes the PVC's
        resourceVersion, which makes a janitor DELETE based on an older listing fail its precondition."""
        if ANN_UNUSED_SINCE not in ((pvc.get("metadata") or {}).get("annotations") or {}):
            return pvc
        name = self._id.workspace_claim
        try:
            return self._client.patch_pvc(name, {"metadata": {"annotations": {ANN_UNUSED_SINCE: None}}})
        except NotFound:
            return None
        except KubeError as e:
            if e.status == 403:
                raise EnvironmentConnectionError(
                    f"Could not mark workspace volume {name} in use: Kubernetes API error {e}"[:500],
                    retry_hint="The driver Role needs the `patch` verb on persistentvolumeclaims "
                               "(see the example rbac.yaml).") from None
            raise

    def _capacity_error(self, e: KubeError) -> EnvironmentConnectionError:
        return EnvironmentConnectionError(
            f"Sandbox capacity exhausted in namespace {self._s.namespace}: {e}",
            retry_hint="Retry after an idle session's sandbox is reaped, or ask the operator to raise the quota.")

    def _check_capacity(self) -> None:
        sel = ",".join(f"{k}={v}" for k, v in MANAGED_BY.items()) + f",{LBL_PROFILE}={slug(self._id.profile, 63)}"
        live = [p["metadata"]["name"] for p in self._client.list_pods(sel)
                if p["metadata"]["name"] != self._id.pod_name and _live(p)]
        if len(live) >= self._s.max_per_profile:
            raise EnvironmentConnectionError(
                f"Sandbox limit reached for profile {self._id.profile!r}: {len(live)} live sandboxes "
                f"({', '.join(sorted(live))}); max_per_profile={self._s.max_per_profile}.",
                retry_hint="Retry after an idle sandbox is reaped, or close another session.")

    def ensure_pod(self) -> None:
        msg = interlock_error(self._s)
        if msg:
            raise EnvironmentConnectionError(msg, retry_hint="Fix kubernetes-sandbox.yaml; retrying will not help.")
        with self._lock:
            if self._pod_ready:
                return
            try:
                self._ensure_pod_locked()
            except EnvironmentConnectionError:
                raise
            except _API_ERRORS as e:
                raise _api_error(f"Could not start sandbox Pod {self._id.pod_name}", e) from None

    def _check_alive(self) -> None:
        """Runs on the caller's thread before every command, never inside Hermes's bounded
        worker: a Pod that vanished (janitor, OOM, drain), stopped being Ready, or was replaced
        under the same name (uid change) is re-ensured before the command is started. A failing
        API call is retried once if it may pass (transport error, 5xx, 429): a transient blip must
        not make Hermes evict the environment."""
        for attempt in range(2):
            try:
                pod = self._client.get_pod(self._id.pod_name)
                break
            except NotFound:
                self._pod_ready = False
                return
            except _API_ERRORS as e:
                if attempt or not _retryable(e):
                    raise _api_error(f"Could not check sandbox Pod {self._id.pod_name}", e) from None
                self._sleep(1)
        if not (self._matches(pod) and _ready(pod)) or _uid(pod) != self._pod_uid:
            self._pod_ready = False

    def _new_pod(self) -> None:
        """State that lives in the Pod: the snapshot (its /tmp) and the bootstrap attempt."""
        self._snapshot_ready = False
        self._bootstrap_attempted = False

    def _ensure_pod_locked(self) -> None:
        name = self._id.pod_name
        replaced_own = False
        for _attempt in range(2):
            try:
                pod = self._client.get_pod(name)
                if self._matches(pod):
                    if not _ready(pod):
                        self._ensure_workspace()  # a Pod Pending on a reaped claim schedules once it exists
                        pod = self._wait_ready(name)
                    else:
                        self._unstamp_adopted()
                    uid = _uid(pod)
                    known = self._pod_uid if self._pod_uid is not None else _served_uid(name)
                    replaced = known is not None and uid != known  # same name, another Pod
                    if replaced:
                        self._new_pod()
                        self._mark_recreated()
                    self._pod_uid = uid
                    self._had_pod = True
                    _remember(name, uid)
                    if replaced:
                        self._restore_cwd()
                    # Only now: a concurrent command must not pass _before_execute mid-restore.
                    self._pod_ready = True
                    return
                # Stale template, finished, terminating, or another session's: replace it. Replacing
                # this session's own Pod is a recreate even if this process never served it.
                if ((pod.get("metadata") or {}).get("annotations") or {}).get(ANN_SESSION_KEY) == self._id.session_key:
                    replaced_own = True
                self._client.delete_pod(name, grace_seconds=_DELETE_GRACE)
                if not self._wait_gone(name):
                    raise EnvironmentConnectionError(
                        f"Previous sandbox Pod {name} is still terminating",
                        retry_hint="Retry shortly; a new sandbox Pod is created once the old one is gone.")
            except NotFound:
                pass
            self._check_capacity()
            self._ensure_workspace()
            manifest = render_pod(self._s, self._id, now_iso=_now_iso(self._clock()),
                                  created_by=f"{socket.gethostname()}:{os.getpid()}")
            try:
                self._client.create_pod(manifest)
            except Conflict:
                continue  # someone else created it meanwhile: loop back and adopt
            except QuotaExceeded as e:
                raise self._capacity_error(e) from None
            except KubeError as e:
                raise EnvironmentConnectionError(f"Could not create sandbox Pod {name}: {e}")
            self._new_pod()
            recreated = self._had_pod or _served(name) or replaced_own
            if recreated:
                self._mark_recreated()
            self._notify("starting sandbox Pod")
            pod = self._wait_ready(name)
            self._pod_uid = _uid(pod)
            self._had_pod = True
            _remember(name, self._pod_uid)
            self._last_touch = self._clock()
            if recreated:
                self._restore_cwd()
            self._pod_ready = True  # after _restore_cwd, as above
            return
        raise EnvironmentConnectionError(
            f"Could not create sandbox Pod {name}: it keeps conflicting with an existing Pod",
            retry_hint="Retry the command.")

    def _restore_cwd(self) -> None:
        """A fresh Pod lacks the old cwd unless it was on persisted storage; recreate it, as
        `docker run -w <cwd>` does for the docker backend. Best effort: on any failure fall back
        to the cwd the environment was constructed with."""
        try:
            handle = self._start(f"mkdir -p -- {shlex.quote(self.cwd)}", False, 15, None)
            rc = handle.wait(timeout=20)
            if rc is None:
                handle.kill()
            ok = rc == 0 and getattr(handle, "error", None) is None
        except Exception:
            ok = False
        if not ok:
            self.cwd = self._initial_cwd

    def _wait_ready(self, name: str) -> dict:
        deadline = self._clock() + self._s.ready_timeout_seconds
        last = {}
        while self._clock() < deadline:
            last = self._client.get_pod(name)
            if _ready(last):
                return last
            if (last.get("status") or {}).get("phase") in ("Failed", "Succeeded"):
                break
            self._notify("waiting for sandbox Pod")
            self._sleep(2)
        conds = [f"{c.get('type')}={c.get('status')} {c.get('message', '')}".strip()
                 for c in (last.get("status") or {}).get("conditions") or []]
        raise EnvironmentConnectionError(
            f"Sandbox Pod {name} not ready after {self._s.ready_timeout_seconds}s: "
            f"phase={(last.get('status') or {}).get('phase')} {'; '.join(conds)}")

    def _touch_last_used(self) -> None:
        now = self._clock()
        if now - self._last_touch < _LAST_USED_EVERY:
            return
        try:
            self._client.patch_pod_annotations(self._id.pod_name, {ANN_LAST_USED: _now_iso(now)})
            self._last_touch = now
        except _API_ERRORS:
            pass

    # --- BaseEnvironment ------------------------------------------------------
    def _before_execute(self) -> None:
        if self._pod_ready:
            self._check_alive()
        if not self._pod_ready:
            self.ensure_pod()
        if not self._snapshot_ready and not self._bootstrap_attempted:
            self._bootstrap_attempted = True  # once per Pod: a failed bootstrap costs up to 60 s
            self.init_session()
        self._touch_last_used()

    def _wrap_command(self, command: str, cwd: str) -> str:
        # Hermes plans every command's cwd itself and passes it explicitly: without terminal.cwd that is its
        # default for this backend (/root in v0.22.1), which the work container's user (UID 1000) cannot
        # enter, so the command would fail `cd: /root: Permission denied` (exit 126). Run it in the
        # environment's initial cwd (the workspace) instead; any other cwd, the session's own included, passes.
        if self._hermes_default_cwd is not None and cwd == self._hermes_default_cwd:
            cwd = self._initial_cwd
        return super()._wrap_command(command, cwd)

    def _exec_env_script(self) -> str:
        """The forwarded environment of ONE exec, as shell lines the runner evals (never in an argv, the exec
        URL or the Pod spec). Values come from Hermes's own resolver: `terminal.env_passthrough` plus the
        `forward_env` setting, the routed profile's value under multiplexing; names the scope lacks come back
        to be unset, so another profile's value can never linger. Every RESOLVED name, explicit forward_env
        and implicit terminal.env_passthrough alike, then passes one withheld-credential check (_withheld):
        a withheld name is resolved but never emitted. Plus the git credential configuration. Only
        shell-name-valid names are emitted and every value is shell-quoted; a NUL in a value is refused by
        stdin_payload."""
        values, unset = resolve_passthrough_env(self._s.forward_env)
        values = {k: v for k, v in values.items()
                  if isinstance(k, str) and _SHELL_NAME.fullmatch(k) and not _withheld(k)}
        values.update(git_config_env(self._s.git_credentials))
        unset = {n for n in unset if isinstance(n, str) and _SHELL_NAME.fullmatch(n) and n not in values}
        self._exec_env_names.update(values)
        self._exec_env_names.update(unset)
        lines = [f"export {k}={shlex.quote(v)}" for k, v in sorted(values.items())]
        if unset:
            lines.append("unset " + " ".join(sorted(unset)))
        return "".join(line + "\n" for line in lines)

    def _snapshot_excluded_passthrough_names(self) -> tuple[str, ...]:
        """Forwarded and git names never enter the session snapshot, multiplexed or not: the snapshot is a
        file in the Pod's /tmp that every later command sources."""
        names = set(super()._snapshot_excluded_passthrough_names()) | self._exec_env_names
        try:
            from tools.env_passthrough import get_all_passthrough
            names.update(n for n in get_all_passthrough() if isinstance(n, str))
        except Exception:
            pass
        return tuple(sorted(n for n in names if _SHELL_NAME.fullmatch(n)))

    def _start(self, cmd_string, login, timeout, stdin_data):
        exec_id = uuid.uuid4().hex[:12]
        pod = self._id.pod_name

        def on_kill():
            try:
                self._exec(pod, "work", kill_argv(exec_id), b"", lambda: None).wait(timeout=15)
            except Exception:
                pass

        handle = self._exec(pod, "work", exec_argv(exec_id, login=login, timeout=timeout),
                            stdin_payload(cmd_string, stdin_data, self._exec_env_script()), on_kill)
        return handle

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        # Runs inside Hermes's bounded worker (execute -> run_bounded_sync), whose timeout can only
        # kill a started process: it NEVER creates or recreates a Pod. _before_execute does that.
        try:
            return self._start(cmd_string, login, timeout, stdin_data)
        except NotFound:
            self._pod_ready = False
            raise EnvironmentConnectionError(
                f"Sandbox Pod {self._id.pod_name} is gone",
                retry_hint="Retry the command; the sandbox Pod is recreated automatically.") from None

    def _wait_for_process(self, proc, timeout=120, **kwargs):
        # Every keyword goes through unchanged: the base grows parameters between Hermes releases
        # (v0.22 added `output`), and this override only adds the stream-loss check below.
        result = super()._wait_for_process(proc, timeout, **kwargs)
        err = getattr(proc, "error", None)
        killed = getattr(proc, "_killed", None)
        if isinstance(err, (ExecDisconnected, ExecFailed)) and not (killed is not None and killed.is_set()):
            if isinstance(err, ExecDisconnected):
                self._pod_ready = False  # re-check the Pod (and recreate it if gone) on the next command
            raise EnvironmentConnectionError(
                f"Lost the exec stream to sandbox {self._id.pod_name}: {err}",
                retry_hint="Retry the command; if the sandbox Pod was lost it is recreated automatically.")
        return result

    def cleanup(self):
        # Deletes the Pod only, NEVER the workspace PVC: the session's next Pod mounts it again,
        # and only the janitor deletes it (after workspace_retention unmounted).
        if _PROCESS_EXITING:
            return  # detach: the next gateway adopts the Pod, or the janitor reaps it
        try:
            self._client.delete_pod(self._id.pod_name, grace_seconds=_DELETE_GRACE)
        except Exception:
            pass
        self._pod_ready = False
        self._had_pod = False
