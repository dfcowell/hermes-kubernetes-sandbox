import importlib.util
import pathlib
import urllib.error
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("janitor", pathlib.Path(__file__).parents[1] / "janitor/janitor.py")
janitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(janitor)

NOW = 1_000_000.0


def pod(last_used_age=0, created_age=0, phase="Running", max_life=86400, name="p", uid="uid-p", deletion_ts=None):
    iso = janitor.iso
    p = {"metadata": {"name": name, "uid": uid, "creationTimestamp": iso(NOW - created_age),
                      "labels": {"app.kubernetes.io/managed-by": "hermes-kubernetes-sandbox"},
                      "annotations": {"hermes-sandbox/last-used": iso(NOW - last_used_age),
                                      "hermes-sandbox/max-lifetime-seconds": str(max_life)}},
         "status": {"phase": phase}}
    if deletion_ts is not None:
        p["metadata"]["deletionTimestamp"] = deletion_ts
    return p


def test_keeps_fresh_pod():
    assert janitor.decide(pod(10, 10), NOW, 3600) is None


def test_idle_pod_reaped():
    assert janitor.decide(pod(4000, 4000), NOW, 3600) == "idle"


def test_max_lifetime_beats_activity():
    assert janitor.decide(pod(5, 90000), NOW, 3600) == "max-lifetime"


def test_finished_pods_reaped():
    assert janitor.decide(pod(phase="Failed"), NOW, 3600) == "finished"


def test_missing_annotations_fall_back_to_creation_time():
    p = pod(created_age=5000)
    p["metadata"]["annotations"] = {}
    assert janitor.decide(p, NOW, 3600) == "idle"


def test_unparseable_last_used_falls_back_to_creation():
    """Test that garbage last-used annotation falls back to creationTimestamp."""
    p = pod(created_age=4000)
    p["metadata"]["annotations"]["hermes-sandbox/last-used"] = "garbage"
    # Pod is idle by creation time
    assert janitor.decide(p, NOW, 3600) == "idle"


def test_unparseable_max_lifetime_uses_default():
    """Test that unparseable max-lifetime-seconds defaults to 86400."""
    p = pod(created_age=90000)
    p["metadata"]["annotations"]["hermes-sandbox/max-lifetime-seconds"] = "abc"
    # Pod is max-lifetime (90000 > 86400 default)
    assert janitor.decide(p, NOW, 3600) == "max-lifetime"


def test_unparseable_creation_timestamp_raises():
    """Test that unparseable creationTimestamp raises ValueError."""
    p = pod()
    p["metadata"]["creationTimestamp"] = "garbage"
    try:
        janitor.decide(p, NOW, 3600)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_main_garbage_last_used_and_idle_pod():
    """Test that one Pod with garbage last-used (falls back to creation time) and one idle Pod results in only the idle one deleted."""
    pods = [
        pod(name="garbage-last-used", created_age=10),  # garbage last-used falls back to creation time (10s ago, kept)
        pod(name="idle-pod", created_age=4000, last_used_age=4000),  # idle pod
    ]
    pods[0]["metadata"]["annotations"]["hermes-sandbox/last-used"] = "garbage"

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
        with patch("time.time", return_value=NOW):
            with patch.object(janitor, "_api", mock_api):
                with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                    exit_code = janitor.main()

    # Only idle-pod should be deleted; garbage-last-used should be kept (falls back to 10s-ago creation time)
    assert len(called_deletes) == 1
    assert "idle-pod" in called_deletes[0][0]
    assert exit_code == 0


def test_main_unparseable_max_lifetime_doesnt_stop_others():
    """Test that one Pod with unparseable max-lifetime doesn't prevent others from being deleted."""
    pods = [
        pod(name="bad-max-life", created_age=90000, last_used_age=90000),  # old but unparseable max-life
        pod(name="idle-pod", created_age=4000, last_used_age=4000),  # idle pod
    ]
    pods[0]["metadata"]["annotations"]["hermes-sandbox/max-lifetime-seconds"] = "abc"

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
        with patch("time.time", return_value=NOW):
            with patch.object(janitor, "_api", mock_api):
                with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                    exit_code = janitor.main()

    # Both should be deleted (bad-max-life uses default 86400, 90000 > 86400 so max-lifetime; idle-pod is idle)
    assert len(called_deletes) == 2
    assert exit_code == 0


def test_main_unparseable_creation_timestamp():
    """Test that a Pod with unparseable creationTimestamp causes error, exit 1, but others still deleted."""
    pods = [
        pod(name="bad-creation"),
        pod(name="idle-pod", created_age=4000, last_used_age=4000),
    ]
    pods[0]["metadata"]["creationTimestamp"] = "garbage"

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    output = []
    with patch("builtins.print", side_effect=lambda *args, **kw: output.append(" ".join(str(a) for a in args))):
        with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        exit_code = janitor.main()

    # idle-pod should still be deleted
    assert len(called_deletes) == 1
    assert "idle-pod" in called_deletes[0][0]
    # error message for bad-creation
    assert any("error bad-creation" in line for line in output)
    # exit code 1 due to error
    assert exit_code == 1


def test_main_delete_404():
    """Test that DELETE 404 results in 'gone' message and exit 0."""
    pods = [pod(name="pod-to-delete", created_age=4000, last_used_age=4000)]

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            raise urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        return {}

    output = []
    with patch("builtins.print", side_effect=lambda *args, **kw: output.append(" ".join(str(a) for a in args))):
        with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        exit_code = janitor.main()

    # gone message
    assert any("gone pod-to-delete" in line for line in output)
    assert exit_code == 0


def test_main_delete_409():
    """Test that DELETE 409 results in 'skip: changed' message."""
    pods = [pod(name="pod-to-delete", created_age=4000, last_used_age=4000)]

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            raise urllib.error.HTTPError(None, 409, "Conflict", {}, None)
        return {}

    output = []
    with patch("builtins.print", side_effect=lambda *args, **kw: output.append(" ".join(str(a) for a in args))):
        with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        exit_code = janitor.main()

    # skip: changed message
    assert any("skip pod-to-delete: changed" in line for line in output)
    assert exit_code == 0


def test_main_terminating_pod():
    """Test that pods with deletionTimestamp are skipped."""
    pods = [
        pod(name="terminating-pod", created_age=4000, last_used_age=4000, deletion_ts="2026-01-01T00:00:00Z"),
        pod(name="idle-pod", created_age=4000, last_used_age=4000),
    ]

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    output = []
    with patch("builtins.print", side_effect=lambda *args, **kw: output.append(" ".join(str(a) for a in args))):
        with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        exit_code = janitor.main()

    # Only idle-pod should be deleted, not terminating-pod
    assert len(called_deletes) == 1
    assert "idle-pod" in called_deletes[0][0]
    # skip: terminating message for terminating-pod
    assert any("skip terminating-pod: terminating" in line for line in output)
    assert exit_code == 0


def test_main_dry_run():
    """Test that DRY_RUN=1 doesn't call DELETE."""
    pods = [pod(name="idle-pod", created_age=4000, last_used_age=4000)]

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    output = []
    with patch("builtins.print", side_effect=lambda *args, **kw: output.append(" ".join(str(a) for a in args))):
        with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "1"}):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        exit_code = janitor.main()

    # No DELETE calls in dry-run
    assert len(called_deletes) == 0
    # Would delete message
    assert any("would delete idle-pod" in line for line in output)
    assert exit_code == 0


def test_main_delete_body_has_uid_precondition():
    """Test that DELETE body includes uid precondition."""
    pods = [pod(name="pod-to-delete", created_age=4000, last_used_age=4000, uid="uid-123")]

    called_deletes = []

    def mock_api(method, path, ctx, token, body=None):
        if method == "GET":
            return {"items": pods if "/pods" in path else []}
        elif method == "DELETE":
            called_deletes.append((path, body))
            return {}
        return {}

    with patch("os.environ", {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0"}):
        with patch("time.time", return_value=NOW):
            with patch.object(janitor, "_api", mock_api):
                with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                    exit_code = janitor.main()

    assert len(called_deletes) == 1
    path, body = called_deletes[0]
    assert "pod-to-delete" in path
    assert body == {"gracePeriodSeconds": 10, "preconditions": {"uid": "uid-123"}}
    assert exit_code == 0


def test_janitor_imports_urllib_error_itself():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(janitor.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "urllib.error" in imported


# --- workspace PVC pass ---------------------------------------------------------------
WEEK = 604800
UNUSED, RETENTION = "hermes-sandbox/unused-since", "hermes-sandbox/workspace-retention-seconds"


def pvc(name="ws", uid="uid-ws", unused_age=None, retention=None, deletion_ts=None, unused_raw=None):
    ann = {}
    if unused_age is not None:
        ann[UNUSED] = janitor.iso(NOW - unused_age)
    if unused_raw is not None:
        ann[UNUSED] = unused_raw
    if retention is not None:
        ann[RETENTION] = retention
    c = {"metadata": {"name": name, "uid": uid, "resourceVersion": "rv-" + name, "annotations": ann,
                      "labels": {"app.kubernetes.io/managed-by": "hermes-kubernetes-sandbox",
                                 "app.kubernetes.io/component": "workspace"}}}
    if deletion_ts:
        c["metadata"]["deletionTimestamp"] = deletion_ts
    return c


@pytest.mark.parametrize("mounted,unused_age,retention,expected", [
    (True, None, None, None),                 # in use, never stamped
    (True, 10, None, "unstamp"),              # back in use: clear the idle clock
    (True, 2 * WEEK, None, "unstamp"),        # in use wins over expiry
    (False, None, None, "stamp"),             # first run unmounted: start the clock
    (False, 100, None, None),                 # stamped, not expired
    (False, WEEK - 1, None, None),
    (False, WEEK + 1, None, "delete"),        # default retention expired
    (False, 4000, "3600", "delete"),          # per-PVC retention honoured
    (False, 4000, "86400", None),
    (False, WEEK + 1, "1209600", None),       # per-PVC longer than default
    (False, WEEK + 1, "garbage", "delete"),   # bad retention annotation -> default
    (False, WEEK + 1, "-5", "delete"),
])
def test_decide_pvc_table(mounted, unused_age, retention, expected):
    assert janitor.decide_pvc(pvc(unused_age=unused_age, retention=retention), mounted, NOW, WEEK) == expected


@pytest.mark.parametrize("raw", ["garbage", "", "2026-13-45T00:00:00Z", "1700000000"])
def test_decide_pvc_malformed_unused_since_restamps_never_deletes(raw):
    assert janitor.decide_pvc(pvc(unused_raw=raw, retention="1"), False, NOW, WEEK) == "stamp"
    assert janitor.decide_pvc(pvc(unused_raw=raw), True, NOW, WEEK) == "unstamp"


def test_decide_pvc_missing_annotations_map():
    c = pvc()
    c["metadata"]["annotations"] = None
    assert janitor.decide_pvc(c, False, NOW, WEEK) == "stamp"


def mounting_pod(claim, name="hs-x", phase="Running"):
    return {"metadata": {"name": name, "uid": "u-" + name, "creationTimestamp": janitor.iso(NOW)},
            "spec": {"volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": claim}},
                                 {"name": "scratch", "emptyDir": {}}]},
            "status": {"phase": phase}}


def test_mounted_claims_ignores_terminal_pods():
    pods = [mounting_pod("a"), mounting_pod("b", "p2", phase="Succeeded"), mounting_pod("c", "p3", phase="Failed"),
            mounting_pod("d", "p4", phase="Pending"), {"metadata": {"name": "bare"}}]
    assert janitor.mounted_claims(pods) == {"a", "d"}


def run_main(pods, pvcs, env=None, fail=None):
    """fail: {(method, name): exception} raised for that call."""
    calls, output = [], []

    def mock_api(method, path, ctx, token, body=None, content_type="application/json"):
        calls.append((method, path, body, content_type))
        for (m, name), exc in (fail or {}).items():
            if m == method and path.rstrip("/").endswith("/" + name):
                raise exc
        if method == "GET":
            return {"items": pods if "/pods" in path else pvcs}
        return {}

    environ = {"NAMESPACE": "test-ns", "IDLE_SECONDS": "3600", "DRY_RUN": "0", **(env or {})}
    with patch("builtins.print", side_effect=lambda *a, **kw: output.append(" ".join(str(x) for x in a))):
        with patch("os.environ", environ):
            with patch("time.time", return_value=NOW):
                with patch.object(janitor, "_api", mock_api):
                    with patch.object(janitor, "_load_api_context", return_value=("token", "ctx")):
                        rc = janitor.main()
    return rc, calls, output


def test_main_lists_all_pods_once_and_pvcs_by_kind_with_selector():
    rc, calls, _ = run_main([], [])
    gets = [c[1] for c in calls if c[0] == "GET"]
    assert len(gets) == 2
    assert gets[0] == "/api/v1/namespaces/test-ns/pods"           # every Pod: any of them may mount a claim
    assert gets[1].startswith("/api/v1/namespaces/test-ns/persistentvolumeclaims?labelSelector=")
    assert "component%3Dworkspace" in gets[1] and "managed-by%3Dhermes-kubernetes-sandbox" in gets[1]
    assert rc == 0


def test_main_pod_pass_only_reaps_managed_pods():
    stranger = pod(name="not-ours", created_age=4000, last_used_age=4000)
    stranger["metadata"]["labels"] = {}
    rc, calls, _ = run_main([stranger, pod(name="idle-pod", created_age=4000, last_used_age=4000)], [])
    deletes = [c[1] for c in calls if c[0] == "DELETE"]
    assert deletes == ["/api/v1/namespaces/test-ns/pods/idle-pod"]


def test_main_pvc_pass_stamps_unstamps_and_deletes():
    pods = [mounting_pod("ws-mounted"), mounting_pod("ws-finished", "done", phase="Succeeded")]
    pvcs = [pvc("ws-mounted", unused_age=50), pvc("ws-new"), pvc("ws-finished"),
            pvc("ws-fresh", unused_age=100), pvc("ws-expired", uid="uid-exp", unused_age=WEEK + 60),
            pvc("ws-dying", unused_age=WEEK + 60, deletion_ts="2026-01-01T00:00:00Z")]
    rc, calls, output = run_main(pods, pvcs)
    base = "/api/v1/namespaces/test-ns/persistentvolumeclaims/"
    writes = [(m, p, b, ct) for m, p, b, ct in calls if m in ("PATCH", "DELETE") and "persistentvolumeclaims" in p]
    merge = "application/merge-patch+json"
    assert writes == [
        ("PATCH", base + "ws-mounted", {"metadata": {"annotations": {UNUSED: None}}}, merge),
        ("PATCH", base + "ws-new", {"metadata": {"annotations": {UNUSED: janitor.iso(NOW)}}}, merge),
        ("PATCH", base + "ws-finished", {"metadata": {"annotations": {UNUSED: janitor.iso(NOW)}}}, merge),
        ("DELETE", base + "ws-expired", {"preconditions": {"uid": "uid-exp", "resourceVersion": "rv-ws-expired"}},
         "application/json"),
    ]
    assert any("skip ws-dying: terminating" in line for line in output)
    assert any("keep ws-fresh" in line for line in output)
    assert rc == 0


def test_main_pvc_retention_env_default():
    pvcs = [pvc("ws-a", unused_age=7200)]
    _, calls, _ = run_main([], pvcs, env={"WORKSPACE_RETENTION_SECONDS": "3600"})
    assert [c[0] for c in calls if "persistentvolumeclaims/" in c[1]] == ["DELETE"]
    _, calls, _ = run_main([], pvcs)                      # default: 7 days
    assert [c[0] for c in calls if "persistentvolumeclaims/" in c[1]] == []


def test_main_pvc_failures_are_isolated_per_pvc():
    pvcs = [pvc("ws-bad", unused_age=WEEK + 60), pvc("ws-new"), pvc("ws-gone", unused_age=WEEK + 60),
            pvc("ws-raced", unused_age=WEEK + 60), {"metadata": {}}]
    fail = {("DELETE", "ws-bad"): urllib.error.HTTPError(None, 500, "boom", {}, None),
            ("DELETE", "ws-gone"): urllib.error.HTTPError(None, 404, "Not Found", {}, None),
            ("DELETE", "ws-raced"): urllib.error.HTTPError(None, 409, "Conflict", {}, None)}
    rc, calls, output = run_main([], pvcs, fail=fail)
    assert any(c[0] == "PATCH" and c[1].endswith("/ws-new") for c in calls)
    assert any("error ws-bad" in line for line in output)
    assert any("gone ws-gone" in line for line in output)
    assert any("skip ws-raced: changed" in line for line in output)
    assert any("error unknown" in line for line in output)
    assert rc == 1


def test_main_pvc_dry_run_writes_nothing():
    pvcs = [pvc("ws-new"), pvc("ws-expired", unused_age=WEEK + 60), pvc("ws-m", unused_age=5)]
    rc, calls, output = run_main([mounting_pod("ws-m")], pvcs, env={"DRY_RUN": "1"})
    assert [c for c in calls if c[0] != "GET"] == []
    assert any("would stamp ws-new" in line for line in output)
    assert any("would delete ws-expired" in line for line in output)
    assert any("would unstamp ws-m" in line for line in output)
    assert rc == 0


def test_api_sends_the_given_content_type():
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, context=None, timeout=None):
        seen["ctype"] = req.get_header("Content-type")
        seen["method"] = req.get_method()
        return Resp()

    with patch.object(janitor.urllib.request, "urlopen", fake_urlopen):
        janitor._api("PATCH", "/x", None, "tok", {"a": None}, content_type="application/merge-patch+json")
    assert seen == {"ctype": "application/merge-patch+json", "method": "PATCH"}


def test_decide_pvc_future_unused_since_is_malformed_and_restamped():
    assert janitor.decide_pvc(pvc(unused_age=-3600, retention="1"), False, NOW, WEEK) == "stamp"
    assert janitor.decide_pvc(pvc(unused_age=0, retention="1"), False, NOW, WEEK) is None


def test_main_pvc_changed_since_listing_is_skipped_benignly():
    """The plugin cleared the stamp (new resourceVersion) after the janitor listed: 409, not an error."""
    pvcs = [pvc("ws-revived", uid="u", unused_age=WEEK + 60)]
    fail = {("DELETE", "ws-revived"): urllib.error.HTTPError(None, 409, "Conflict", {}, None)}
    rc, calls, output = run_main([], pvcs, fail=fail)
    body = [c[2] for c in calls if c[0] == "DELETE"][0]
    assert body == {"preconditions": {"uid": "u", "resourceVersion": "rv-ws-revived"}}
    assert any("skip ws-revived: changed" in line for line in output)
    assert rc == 0
