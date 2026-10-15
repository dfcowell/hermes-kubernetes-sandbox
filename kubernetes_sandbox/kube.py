"""Minimal Kubernetes API client for sandbox Pods and workspace PVCs (httpx; no kubernetes package).

Deliberately not a client library: Hermes plugins are installed as a directory with no dependency
install step, and httpx, websockets and a YAML loader are already pinned Hermes core dependencies, so
this keeps the plugin dependency-free (a library would mean a new core dependency or an unpinned runtime PyPI
fetch). The surface needed is small: Pod/PVC get/list/create/patch/delete (PVC deletes carry
uid/resourceVersion preconditions), plus the exec URL (the exec stream itself is exec_stream.py). The token file is
re-read on every request so a rotating projected ServiceAccount token just works. Trade-off: only
bearer-token + CA authentication. See README, "Why no Kubernetes client library?".
"""
from __future__ import annotations

import json
import pathlib
import ssl
import urllib.parse

import httpx

from .settings import Settings


class KubeError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str):
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason = status, reason


class NotFound(KubeError):
    pass


class Conflict(KubeError):
    pass


class QuotaExceeded(KubeError):
    pass


class KubeClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self._s = settings
        self._base = settings.api_url.rstrip("/")
        self._http = httpx.Client(transport=transport, verify=self.ssl_context(), timeout=30.0)

    def ssl_context(self) -> ssl.SSLContext | bool:
        # A configured but missing ca_path falls back to system trust; the provider's
        # check_requirements()/probe() report it (ca_problem()).
        if self._s.ca_path and pathlib.Path(self._s.ca_path).exists():
            return ssl.create_default_context(cafile=self._s.ca_path)
        return True

    def auth_headers(self) -> dict:
        # Re-read on every call: projected service-account tokens rotate.
        token = pathlib.Path(self._s.token_path).read_text(encoding="utf-8").strip()
        return {"Authorization": f"Bearer {token}"}

    def _pods(self, name: str = "") -> str:
        return self._collection("pods", name)

    def _pvcs(self, name: str = "") -> str:
        return self._collection("persistentvolumeclaims", name)

    def _collection(self, kind: str, name: str) -> str:
        path = f"{self._base}/api/v1/namespaces/{self._s.namespace}/{kind}"
        return f"{path}/{name}" if name else path

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        headers = {**self.auth_headers(), **kw.pop("headers", {})}
        r = self._http.request(method, url, headers=headers, **kw)
        if r.status_code < 400:
            return r
        try:
            body = r.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        reason, message = str(body.get("reason", "")), str(body.get("message", r.text[:300]))
        if r.status_code == 404:
            raise NotFound(404, reason, message)
        if r.status_code == 409:
            raise Conflict(409, reason, message)
        if r.status_code == 403 and "exceeded quota" in message:
            raise QuotaExceeded(403, reason, message)
        raise KubeError(r.status_code, reason, message)

    def get_pod(self, name: str) -> dict:
        return self._request("GET", self._pods(name)).json()

    def create_pod(self, manifest: dict) -> dict:
        return self._request("POST", self._pods(), json=manifest).json()

    def delete_pod(self, name: str, grace_seconds: int = 10) -> None:
        try:
            self._request("DELETE", self._pods(name), json={"gracePeriodSeconds": grace_seconds})
        except NotFound:
            pass

    def list_pods(self, label_selector: str) -> list[dict]:
        return self._request("GET", self._pods(), params={"labelSelector": label_selector}).json().get("items", [])

    def patch_pod_annotations(self, name: str, annotations: dict) -> None:
        self._request(
            "PATCH", self._pods(name),
            content=json.dumps({"metadata": {"annotations": annotations}}),
            headers={"Content-Type": "application/merge-patch+json"},
        )

    # --- workspace PersistentVolumeClaims ------------------------------------
    def get_pvc(self, name: str) -> dict:
        return self._request("GET", self._pvcs(name)).json()

    def create_pvc(self, manifest: dict) -> dict:
        return self._request("POST", self._pvcs(), json=manifest).json()

    def list_pvcs(self, label_selector: str) -> list[dict]:
        return self._request("GET", self._pvcs(), params={"labelSelector": label_selector}).json().get("items", [])

    def patch_pvc(self, name: str, merge_patch: dict) -> dict:
        """Returns the patched object (its deletionTimestamp shows a delete that won the race)."""
        return self._request("PATCH", self._pvcs(name), content=json.dumps(merge_patch),
                             headers={"Content-Type": "application/merge-patch+json"}).json()

    def delete_pvc(self, name: str, *, uid: str, resource_version: str) -> None:
        """Deletes only this PVC exactly as last read (409 Conflict if it changed or was replaced).
        The plugin never calls it (its Role has no PVC delete); scripts/e2e.py does."""
        try:
            self._request("DELETE", self._pvcs(name),
                          json={"preconditions": {"uid": uid, "resourceVersion": resource_version}})
        except NotFound:
            pass

    def exec_url(self, name: str, container: str, argv: list[str]) -> str:
        q = [("container", container), ("stdin", "true"), ("stdout", "true"), ("stderr", "true")]
        q += [("command", a) for a in argv]
        base = self._base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/api/v1/namespaces/{self._s.namespace}/pods/{name}/exec?{urllib.parse.urlencode(q)}"
