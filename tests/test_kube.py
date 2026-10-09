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
