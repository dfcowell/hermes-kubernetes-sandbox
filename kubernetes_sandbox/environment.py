"""Hermes BaseEnvironment backed by one Kubernetes Pod per session."""
from __future__ import annotations

import atexit
import datetime as dt
import os
import socket
import time
import uuid

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError, get_activity_callback

from .exec_stream import ExecDisconnected, ExecFailed, StreamingExecHandle, connect_exec
from .kube import KubeClient, KubeError, NotFound, QuotaExceeded
from .naming import ANN_LAST_USED, ANN_SESSION_KEY, LBL_PROFILE, LBL_TEMPLATE_HASH, MANAGED_BY, Identity, slug
from .podspec import render_pod, template_hash
from .runner import exec_argv, kill_argv, stdin_payload
from .settings import Settings, interlock_error

_PROCESS_EXITING = False


def _mark_exiting():
    global _PROCESS_EXITING
    _PROCESS_EXITING = True


atexit.register(_mark_exiting)  # registered after Hermes's own hooks, so it runs before them (LIFO)

_LAST_USED_EVERY = 60.0


def _now_iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ready(pod: dict) -> bool:
    st = pod.get("status") or {}
    return st.get("phase") == "Running" and any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])


def _default_exec_connect(client):
    def run(pod, container, argv, payload, on_kill):
        client.get_pod(pod)  # a deleted Pod surfaces here as NotFound, so _run_bash can recreate it
        return StreamingExecHandle(lambda: connect_exec(client, pod, container, argv), payload, on_kill)
    return run


class KubernetesEnvironment(BaseEnvironment):
    _stdin_mode = "pipe"
    _snapshot_timeout = 60

    def __init__(self, *, settings: Settings, identity: Identity, cwd: str, timeout: int,
                 client=None, exec_connect=None, clock=time.time, sleep=time.sleep):
        super().__init__(cwd=cwd or "/workspace", timeout=timeout)
        self._s, self._id = settings, identity
        self._client = client or KubeClient(settings)
        self._exec = exec_connect or _default_exec_connect(self._client)
        self._clock, self._sleep = clock, sleep
        self._pod_ready = False
        self._last_touch = 0.0
        self._last_handle = None

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
                and (pod.get("status") or {}).get("phase") not in ("Failed", "Succeeded")
                and not md.get("deletionTimestamp"))

    def _wait_gone(self, name: str) -> None:
        deadline = self._clock() + 60
        while self._clock() < deadline:
            try:
                self._client.get_pod(name)
            except NotFound:
                return
            self._sleep(1)

    def _check_capacity(self) -> None:
        sel = ",".join(f"{k}={v}" for k, v in MANAGED_BY.items()) + f",{LBL_PROFILE}={slug(self._id.profile, 63)}"
        live = [p["metadata"]["name"] for p in self._client.list_pods(sel)
                if p["metadata"]["name"] != self._id.pod_name]
        if len(live) >= self._s.max_per_profile:
            raise EnvironmentConnectionError(
                f"Sandbox limit reached for profile {self._id.profile!r}: {len(live)} live sandboxes "
                f"({', '.join(sorted(live))}); max_per_profile={self._s.max_per_profile}.",
                retry_hint="Retry after an idle sandbox is reaped, or close another session.")

    def ensure_pod(self) -> None:
        msg = interlock_error(self._s)
        if msg:
            raise EnvironmentConnectionError(msg, retry_hint="Fix kubernetes-sandbox.yaml; retrying will not help.")
        name = self._id.pod_name
        try:
            pod = self._client.get_pod(name)
            if self._matches(pod):
                if not _ready(pod):
                    self._wait_ready(name)
                self._pod_ready = True
                return
            self._client.delete_pod(name, grace_seconds=0)
            self._wait_gone(name)
        except NotFound:
            pass
        self._check_capacity()
        manifest = render_pod(self._s, self._id, now_iso=_now_iso(self._clock()),
                              created_by=f"{socket.gethostname()}:{os.getpid()}")
        try:
            self._client.create_pod(manifest)
        except QuotaExceeded as e:
            raise EnvironmentConnectionError(
                f"Sandbox capacity exhausted in namespace {self._s.namespace}: {e}",
                retry_hint="Retry after an idle session's sandbox is reaped, or ask the operator to raise the quota.")
        except KubeError as e:
            raise EnvironmentConnectionError(f"Could not create sandbox Pod {name}: {e}")
        self._notify("starting sandbox Pod")
        self._wait_ready(name)
        self._pod_ready = True
        self._last_touch = self._clock()

    def _wait_ready(self, name: str) -> None:
        deadline = self._clock() + self._s.ready_timeout_seconds
        last = {}
        while self._clock() < deadline:
            last = self._client.get_pod(name)
            if _ready(last):
                return
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
        except KubeError:
            pass

    # --- BaseEnvironment ------------------------------------------------------
    def _before_execute(self) -> None:
        if not self._pod_ready:
            self.ensure_pod()
        self._touch_last_used()

    def _start(self, cmd_string, login, timeout, stdin_data):
        exec_id = uuid.uuid4().hex[:12]
        pod = self._id.pod_name

        def on_kill():
            try:
                self._exec(pod, "work", kill_argv(exec_id), b"", lambda: None).wait(timeout=15)
            except Exception:
                pass

        handle = self._exec(pod, "work", exec_argv(exec_id, login=login, timeout=timeout),
                            stdin_payload(cmd_string, stdin_data), on_kill)
        self._last_handle = handle
        return handle

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        if not self._pod_ready:
            self.ensure_pod()
        try:
            return self._start(cmd_string, login, timeout, stdin_data)
        except NotFound:
            self._pod_ready = False
            self.ensure_pod()
            self._mark_recreated()
            return self._start(cmd_string, login, timeout, stdin_data)

    def execute(self, command, cwd="", **kwargs):
        self._last_handle = None
        result = super().execute(command, cwd, **kwargs)
        h = self._last_handle
        err = getattr(h, "error", None) if h is not None else None
        if isinstance(err, (ExecDisconnected, ExecFailed)) and not getattr(h, "_killed").is_set():
            if isinstance(err, ExecDisconnected):
                self._pod_ready = False  # re-check the Pod (and recreate it if gone) on the next command
            raise EnvironmentConnectionError(
                f"Lost the exec stream to sandbox {self._id.pod_name}: {err}",
                retry_hint="Retry the command; if the sandbox Pod was lost it is recreated automatically.")
        return result

    def cleanup(self):
        if _PROCESS_EXITING:
            return  # detach: the next gateway adopts the Pod, or the janitor reaps it
        try:
            self._client.delete_pod(self._id.pod_name, grace_seconds=10)
        except Exception:
            pass
        self._pod_ready = False
