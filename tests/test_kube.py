import json

import httpx
import pytest

from kubernetes_sandbox.kube import KubeClient, NotFound, QuotaExceeded
from kubernetes_sandbox.settings import Settings


@pytest.fixture
def settings(tmp_path):
    tok = tmp_path / "token"
    tok.write_text("t0k3n\n")
    return Settings(docker=False, api_url="https://api.example:6443", token_path=str(tok), ca_path=None)


def _client(settings, handler):
    return KubeClient(settings, transport=httpx.MockTransport(handler))


def test_get_pod_sends_bearer_and_namespace(settings):
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        return httpx.Response(200, json={"metadata": {"name": "p"}})

    assert _client(settings, handler).get_pod("p")["metadata"]["name"] == "p"
    assert seen == {"auth": "Bearer t0k3n", "path": "/api/v1/namespaces/agent-sandbox/pods/p"}


def test_token_is_reread_each_request(settings, tmp_path):
    auths = []

    def handler(req):
        auths.append(req.headers["authorization"])
        return httpx.Response(200, json={})

    c = _client(settings, handler)
    c.get_pod("a")
    (tmp_path / "token").write_text("rotated")
    c.get_pod("a")
    assert auths == ["Bearer t0k3n", "Bearer rotated"]


def test_get_pod_404_is_notfound(settings):
    with pytest.raises(NotFound):
        _client(settings, lambda r: httpx.Response(404, json={"reason": "NotFound"})).get_pod("x")


def test_create_quota_exceeded(settings):
    body = {"kind": "Status", "reason": "Forbidden",
            "message": 'pods "x" is forbidden: exceeded quota: sandbox-capacity, requested: pods=1'}
    with pytest.raises(QuotaExceeded, match="exceeded quota"):
        _client(settings, lambda r: httpx.Response(403, json=body)).create_pod({"metadata": {"name": "x"}})


def test_delete_ignores_404(settings):
    _client(settings, lambda r: httpx.Response(404, json={})).delete_pod("gone")


def test_patch_annotations_uses_merge_patch(settings):
    seen = {}

    def handler(req):
        seen["ctype"] = req.headers["content-type"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    _client(settings, handler).patch_pod_annotations("p", {"a": "1"})
    assert seen == {"ctype": "application/merge-patch+json", "body": {"metadata": {"annotations": {"a": "1"}}}}


def test_list_pods_passes_selector(settings):
    def handler(req):
        assert req.url.params["labelSelector"] == "a=b"
        return httpx.Response(200, json={"items": [{"metadata": {"name": "p1"}}]})

    assert [p["metadata"]["name"] for p in _client(settings, handler).list_pods("a=b")] == ["p1"]


def test_exec_url_encodes_argv(settings):
    url = _client(settings, lambda r: httpx.Response(200)).exec_url("p", "work", ["bash", "-c", "a b&c"])
    assert url.startswith("wss://api.example:6443/api/v1/namespaces/agent-sandbox/pods/p/exec?")
    assert "container=work" in url and "command=bash" in url and "command=a+b%26c" in url


def test_non_string_error_message_is_stringified(settings):
    from kubernetes_sandbox.kube import KubeError
    with pytest.raises(KubeError) as ei:
        _client(settings, lambda r: httpx.Response(500, json={"reason": "X", "message": {"nested": 1}})).get_pod("p")
    assert "nested" in str(ei.value)


PVCS = "/api/v1/namespaces/agent-sandbox/persistentvolumeclaims"


def test_pvc_get_create_list_hit_the_pvc_collection(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.url.params.get("labelSelector"),
                     json.loads(req.content) if req.content else None, req.headers["authorization"]))
        if req.method == "GET" and req.url.path == PVCS:
            return httpx.Response(200, json={"items": [{"metadata": {"name": "c1"}}]})
        return httpx.Response(200, json={"metadata": {"name": "c"}})

    c = _client(settings, handler)
    assert c.get_pvc("c")["metadata"]["name"] == "c"
    assert c.create_pvc({"metadata": {"name": "c"}})["metadata"]["name"] == "c"
    assert [p["metadata"]["name"] for p in c.list_pvcs("a=b")] == ["c1"]
    assert seen == [("GET", PVCS + "/c", None, None, "Bearer t0k3n"),
                    ("POST", PVCS, None, {"metadata": {"name": "c"}}, "Bearer t0k3n"),
                    ("GET", PVCS, "a=b", None, "Bearer t0k3n")]


def test_pvc_errors_map_like_pods(settings):
    from kubernetes_sandbox.kube import Conflict, KubeError
    with pytest.raises(NotFound):
        _client(settings, lambda r: httpx.Response(404, json={"reason": "NotFound"})).get_pvc("x")
    with pytest.raises(Conflict):
        _client(settings, lambda r: httpx.Response(409, json={"reason": "AlreadyExists"})).create_pvc(
            {"metadata": {"name": "x"}})
    with pytest.raises(KubeError) as ei:
        _client(settings, lambda r: httpx.Response(403, json={"reason": "Forbidden", "message": "no"})).list_pvcs("")
    assert "t0k3n" not in str(ei.value) and "Bearer" not in str(ei.value)


def test_patch_pvc_uses_merge_patch(settings):
    seen = {}

    def handler(req):
        seen.update(method=req.method, path=req.url.path, ctype=req.headers["content-type"],
                    body=json.loads(req.content))
        return httpx.Response(200, json={})

    _client(settings, handler).patch_pvc("c", {"metadata": {"annotations": {"a": None}}})
    assert seen == {"method": "PATCH", "path": PVCS + "/c", "ctype": "application/merge-patch+json",
                    "body": {"metadata": {"annotations": {"a": None}}}}


def test_delete_pvc_sends_uid_precondition_and_maps_conflict(settings):
    from kubernetes_sandbox.kube import Conflict
    seen = {}

    def handler(req):
        seen.update(method=req.method, path=req.url.path, body=json.loads(req.content))
        return httpx.Response(200, json={})

    _client(settings, handler).delete_pvc("c", uid="u-1", resource_version="7")
    assert seen == {"method": "DELETE", "path": PVCS + "/c",
                    "body": {"preconditions": {"uid": "u-1", "resourceVersion": "7"}}}
    with pytest.raises(Conflict):
        _client(settings, lambda r: httpx.Response(409, json={"reason": "Conflict"})).delete_pvc("c", uid="u-2", resource_version="8")
    _client(settings, lambda r: httpx.Response(404, json={})).delete_pvc("gone", uid="u-3", resource_version="9")


def test_non_json_2xx_body_raises_json_decode_error(settings):
    with pytest.raises(json.JSONDecodeError):
        _client(settings, lambda r: httpx.Response(200, text="<html>proxy</html>")).get_pod("p")


def test_patch_pvc_returns_the_updated_object(settings):
    body = {"metadata": {"name": "c", "deletionTimestamp": "2026-10-09T00:00:00Z"}}
    got = _client(settings, lambda r: httpx.Response(200, json=body)).patch_pvc("c", {"metadata": {}})
    assert got == body


def test_delete_pvc_sends_uid_and_resource_version_preconditions(settings):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    _client(settings, handler).delete_pvc("c", uid="u-1", resource_version="42")
    assert seen["body"] == {"preconditions": {"uid": "u-1", "resourceVersion": "42"}}
