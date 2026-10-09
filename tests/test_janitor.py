import importlib.util
import pathlib
import urllib.error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("janitor", pathlib.Path(__file__).parents[1] / "janitor/janitor.py")
janitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(janitor)

NOW = 1_000_000.0


def pod(last_used_age=0, created_age=0, phase="Running", max_life=86400, name="p", uid="uid-p", deletion_ts=None):
    iso = janitor.iso
    p = {"metadata": {"name": name, "uid": uid, "creationTimestamp": iso(NOW - created_age),
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
            return {"items": pods}
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
