import threading

import pytest
from fakes import FakeExec, FakeKube
from kubernetes_sandbox import environment as envmod
from kubernetes_sandbox.environment import KubernetesEnvironment
from kubernetes_sandbox.kube import Conflict
from kubernetes_sandbox.naming import ANN_SESSION_KEY, LBL_TEMPLATE_HASH, Identity
from kubernetes_sandbox.podspec import render_pod, template_hash
from kubernetes_sandbox.settings import Settings
from tools.environments.base import EnvironmentConnectionError

S = Settings(runtime_class_name="kata", docker_disk_storage_class="b", ready_timeout_seconds=5)
ID = Identity("mars", "sess-1")


def _env(kube=None, ex=None, settings=S, ident=ID):
    kube, ex = kube or FakeKube(), ex or FakeExec()
    e = KubernetesEnvironment(settings=settings, identity=ident, cwd="/workspace", timeout=30,
                              client=kube, exec_connect=ex, sleep=lambda s: None)
    return e, kube, ex


def test_creates_pod_and_waits_ready():
    e, kube, _ = _env()
    e.ensure_pod()
    assert ("create", ID.pod_name) in kube.calls
    assert kube.pods[ID.pod_name]["status"]["phase"] == "Running"


def test_adopts_matching_running_pod():
    kube = FakeKube(ready_after=0)
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old"))
    kube.calls.clear()
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert not any(c[0] in ("create", "delete") for c in kube.calls)


def test_replaces_mismatched_pod():
    kube = FakeKube(ready_after=0)
    foreign = render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old")
    foreign["metadata"]["annotations"][ANN_SESSION_KEY] = "someone-else"
    kube.create_pod(foreign)
    kube.calls.clear()
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert ("delete", ID.pod_name) in kube.calls and ("create", ID.pod_name) in kube.calls
    assert kube.pods[ID.pod_name]["metadata"]["annotations"][ANN_SESSION_KEY] == "sess-1"


def test_interlock_blocks_creation():
    bad = Settings(docker=True, docker_disk_storage_class="b")
    e, kube, _ = _env(settings=bad)
    with pytest.raises(EnvironmentConnectionError, match="root on the node"):
        e.ensure_pod()
    assert not any(c[0] == "create" for c in kube.calls)


def test_per_profile_cap():
    kube = FakeKube(ready_after=0)
    s = Settings(runtime_class_name="kata", docker_disk_storage_class="b", max_per_profile=1)
    kube.create_pod(render_pod(s, Identity("mars", "other"), now_iso="x", created_by="x"))
    e, _, _ = _env(kube=kube, settings=s)
    with pytest.raises(EnvironmentConnectionError, match="mars"):
        e.ensure_pod()


def test_quota_exhausted_raises_connection_error():
    e, _, _ = _env(kube=FakeKube(quota=0))
    with pytest.raises(EnvironmentConnectionError, match="capacity exhausted") as ei:
        e.ensure_pod()
    assert e._client.calls.count(("create", ID.pod_name)) == 1
    assert "idle" in ei.value.retry_hint


def test_run_bash_execs_runner_in_work_container():
    e, _, ex = _env()
    h = e._run_bash("echo hi", login=True, timeout=10, stdin_data="IN")
    assert h.wait(timeout=5) == 0
    run = ex.runs[-1]
    assert run["container"] == "work" and run["argv"][:2] == ["bash", "-c"] and run["argv"][-2:] == ["1", "10"]
    assert run["payload"].endswith(b"echo hiIN")


def test_run_bash_never_recreates_on_404():
    e, kube, ex = _env()
    e.ensure_pod()
    kube.pods.clear()          # janitor deleted it
    ex.fail_with_notfound_once = True
    with pytest.raises(EnvironmentConnectionError) as ei:
        e._run_bash("true", timeout=10)
    assert "Retry" in ei.value.retry_hint
    assert e._pod_ready is False
    assert kube.calls.count(("create", ID.pod_name)) == 1


def test_pod_deleted_while_ready_is_recreated_before_run_bash():
    e, kube, ex = _env()
    assert e.execute("echo hi")["returncode"] == 0
    assert e._pod_ready
    kube.pods.clear()          # janitor reap / drain while _pod_ready is True
    seen = []
    real = e._run_bash

    def spy(*a, **kw):
        seen.append((ID.pod_name in kube.pods, kube.calls.count(("create", ID.pod_name))))
        return real(*a, **kw)

    e._run_bash = spy
    result = e.execute("echo hi", timeout=10)
    assert result["returncode"] == 0 and result["environment_recreated"] is True
    assert seen and all(present and creates == 2 for present, creates in seen)


def test_pod_replaced_under_the_same_name_counts_as_recreated():
    e, kube, ex = _env()
    e.execute("echo hi")
    e.cwd = "/workspace/sub"
    kube.pods.clear()
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="another-gateway"))
    n = len(ex.runs)
    result = e.execute("echo hi")
    assert result["environment_recreated"] is True
    assert kube.calls.count(("create", ID.pod_name)) == 2      # adopted, not created by us
    later = ex.runs[n:]
    assert any(b"mkdir -p -- /workspace/sub" in r["payload"] for r in later)   # _restore_cwd
    assert any(r["argv"][-2] == "1" for r in later)                           # snapshot rebuilt


def test_same_pod_is_not_recreated():
    e, kube, ex = _env()
    e.execute("echo hi")
    assert e.execute("echo hi").get("environment_recreated") is None
    assert kube.calls.count(("create", ID.pod_name)) == 1


def test_unready_pod_is_rechecked_before_execute():
    e, kube, ex = _env()
    e.execute("echo hi")
    kube.pods[ID.pod_name]["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}
    kube._gets[ID.pod_name] = -1   # unready for one more GET, then Ready again
    result = e.execute("echo hi")
    assert result["returncode"] == 0 and result.get("environment_recreated") is None
    assert kube.calls.count(("create", ID.pod_name)) == 1


def _raising(exc):
    def f(*a, **kw):
        raise exc
    return f


@pytest.mark.parametrize("exc", [
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(500, "InternalError", "etcd down"),
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(403, "Forbidden", "no pods/get"),
    __import__("httpx").ConnectError("connection refused"),
    FileNotFoundError(2, "No such file or directory", "/var/run/secrets/token"),
])
def test_api_errors_in_ensure_pod_are_connection_errors(exc):
    e, kube, _ = _env()
    kube.get_pod = _raising(exc)
    with pytest.raises(EnvironmentConnectionError) as ei:
        e.ensure_pod()
    assert "Bearer" not in str(ei.value)
    if hasattr(exc, "status"):
        assert str(exc.status) in str(ei.value) and exc.reason in str(ei.value)


def test_api_errors_in_list_pods_are_connection_errors():
    from kubernetes_sandbox.kube import KubeError
    e, kube, _ = _env()
    kube.list_pods = _raising(KubeError(500, "InternalError", "x"))
    with pytest.raises(EnvironmentConnectionError, match="500"):
        e.ensure_pod()


def test_api_errors_in_the_liveness_check_are_connection_errors():
    import httpx
    e, kube, _ = _env()
    e.execute("echo hi")
    kube.get_pod = _raising(httpx.ReadTimeout("timed out"))
    with pytest.raises(EnvironmentConnectionError, match="ReadTimeout"):
        e.execute("echo hi")


def test_failed_snapshot_bootstrap_is_not_retried_before_every_command():
    import json

    from kubernetes_sandbox.exec_stream import ERROR, StreamingExecHandle, encode_frame

    inner = FakeExec()
    boots = []

    class FailConn:
        def __init__(self):
            self.frames = [encode_frame(ERROR, json.dumps({"status": "Failure", "details": {
                "causes": [{"reason": "ExitCode", "message": "1"}]}}).encode())]

        def send(self, d):
            pass

        def recv(self, timeout=None):
            if self.frames:
                return self.frames.pop(0)
            raise ConnectionError("closed")

        def close(self):
            pass

    def ex(pod, container, argv, payload, on_kill):
        if b"declare -F" in payload:   # the snapshot bootstrap script
            boots.append(1)
            return StreamingExecHandle(lambda: FailConn(), payload, on_kill)
        return inner(pod, container, argv, payload, on_kill)

    e, kube, _ = _env(ex=ex)
    e.execute("echo 1")
    e.execute("echo 2")
    e.execute("echo 3")
    assert len(boots) == 1 and e._snapshot_ready is False
    kube.pods.clear()                     # a new Pod gets a fresh attempt
    e.execute("echo 4")
    assert len(boots) == 2


def test_cleanup_deletes_but_detaches_on_process_exit(monkeypatch):
    e, kube, _ = _env()
    e.ensure_pod()
    monkeypatch.setattr(envmod, "_PROCESS_EXITING", True)
    e.cleanup()
    assert ID.pod_name in kube.pods
    monkeypatch.setattr(envmod, "_PROCESS_EXITING", False)
    e2, _, _ = _env(kube=kube)
    e2.ensure_pod()
    e2.cleanup()
    assert ID.pod_name not in kube.pods


def test_fresh_env_creating_a_served_pod_name_marks_recreated():
    e, kube, _ = _env()
    assert e.execute("echo hi").get("environment_recreated") is None
    e.cleanup()                           # gateway stop / degraded eviction: the Pod is deleted
    assert ID.pod_name not in kube.pods
    e2, _, _ = _env(kube=kube)
    assert e2.execute("echo hi")["environment_recreated"] is True


def test_fresh_env_adopting_a_served_pod_is_not_recreated():
    e, kube, _ = _env()
    e.execute("echo hi")
    e2, _, _ = _env(kube=kube)
    assert e2.execute("echo hi").get("environment_recreated") is None
    assert kube.calls.count(("create", ID.pod_name)) == 1


def test_env_is_session_scoped_so_hermes_skips_per_turn_teardown(monkeypatch):
    from tools import terminal_tool_lifecycle as lc
    e, _, _ = _env()
    monkeypatch.setattr(lc, "get_active_env", lambda task_id: e)
    assert e._session_scoped is True and lc.is_persistent_env("sess-1")


def test_last_used_patch_is_throttled():
    e, kube, _ = _env()
    e.ensure_pod()
    ticks = iter([1000.0, 1001.0, 1070.0])
    e._clock, e._last_touch = (lambda: next(ticks)), 0.0
    for _ in range(3):
        e._touch_last_used()
    assert kube.calls.count(("patch", ID.pod_name)) == 2


def test_dropped_stream_raises_environment_connection_error():
    from kubernetes_sandbox.exec_stream import StreamingExecHandle

    class DropConn:
        def send(self, d):
            pass

        def recv(self, timeout=None):
            raise ConnectionError("reset by peer")

        def close(self):
            pass

    def dropping_exec(pod, container, argv, payload, on_kill):
        return StreamingExecHandle(lambda: DropConn(), payload, on_kill)

    e, _, _ = _env(ex=dropping_exec)
    e.ensure_pod()
    e._snapshot_ready = True
    with pytest.raises(EnvironmentConnectionError, match="exec stream"):
        e.execute("echo hi")


class DropConn:
    def send(self, d):
        pass

    def recv(self, timeout=None):
        raise ConnectionError("reset by peer")

    def close(self):
        pass


def test_template_hash_mismatch_is_replaced():
    kube = FakeKube(ready_after=0)
    old = render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old")
    old["metadata"]["labels"][LBL_TEMPLATE_HASH] = "stale"
    kube.create_pod(old)
    kube.calls.clear()
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert ("delete", ID.pod_name) in kube.calls and ("create", ID.pod_name) in kube.calls
    assert kube.pods[ID.pod_name]["metadata"]["labels"][LBL_TEMPLATE_HASH] == template_hash(S)


def test_failed_pod_is_replaced():
    kube = FakeKube(ready_after=0)
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old"))
    kube.pods[ID.pod_name]["status"] = {"phase": "Failed"}
    kube.calls.clear()
    e, _, _ = _env(kube=kube)
    kube._gets[ID.pod_name] = -10  # stay un-Ready for the first get so the Failed phase is seen
    e.ensure_pod()
    assert ("delete", ID.pod_name) in kube.calls and ("create", ID.pod_name) in kube.calls


def test_concurrent_executes_only_the_dropping_one_raises():
    from kubernetes_sandbox.exec_stream import StreamingExecHandle

    inner = FakeExec()

    def selective(pod, container, argv, payload, on_kill):
        if b"DROPME" in payload:
            return StreamingExecHandle(lambda: DropConn(), payload, on_kill)
        return inner(pod, container, argv, payload, on_kill)

    e, _, _ = _env(ex=selective)
    e.ensure_pod()
    e._snapshot_ready = True
    outcomes = {}

    def go(name, cmd):
        try:
            outcomes[name] = e.execute(cmd)
        except EnvironmentConnectionError as exc:
            outcomes[name] = exc

    ts = [threading.Thread(target=go, args=("bad", "echo DROPME")),
          threading.Thread(target=go, args=("good", "echo fine"))]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert isinstance(outcomes["bad"], EnvironmentConnectionError)
    assert isinstance(outcomes["good"], dict) and outcomes["good"]["returncode"] == 0


def test_lost_pod_after_dropped_stream_is_recreated_with_notice():
    from kubernetes_sandbox.exec_stream import StreamingExecHandle

    inner, state = FakeExec(), {"drop": False}

    def ex(pod, container, argv, payload, on_kill):
        if state["drop"]:
            state["drop"] = False
            return StreamingExecHandle(lambda: DropConn(), payload, on_kill)
        return inner(pod, container, argv, payload, on_kill)

    e, kube, _ = _env(ex=ex)
    e.ensure_pod()
    e._snapshot_ready = True
    state["drop"] = True
    with pytest.raises(EnvironmentConnectionError):
        e.execute("echo hi")
    assert e._pod_ready is False
    kube.pods.clear()
    result = e.execute("echo hi")
    assert result["returncode"] == 0 and result["environment_recreated"] is True
    assert kube.calls.count(("create", ID.pod_name)) == 2


def test_ensure_pod_is_locked_single_create():
    kube = FakeKube(ready_after=2)
    e, _, _ = _env(kube=kube)
    ts = [threading.Thread(target=e.ensure_pod) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert kube.calls.count(("create", ID.pod_name)) == 1


def test_conflict_on_create_adopts_the_pod():
    kube = FakeKube(ready_after=0)
    real_create = kube.create_pod

    def racing_create(manifest):
        real_create(manifest)  # the other gateway wins the race
        raise Conflict(409, "AlreadyExists", manifest["metadata"]["name"])

    kube.create_pod = racing_create
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert e._pod_ready and kube.calls.count(("create", ID.pod_name)) == 1


def test_persistent_conflict_raises():
    kube = FakeKube()

    def always(manifest):
        raise Conflict(409, "AlreadyExists", "x")

    kube.create_pod = always
    e, _, _ = _env(kube=kube)
    with pytest.raises(EnvironmentConnectionError, match="conflicting"):
        e.ensure_pod()


def test_default_exec_connect_does_not_get_the_pod_and_execute_recreates(monkeypatch):
    from kubernetes_sandbox.exec_stream import ERROR, encode_frame
    import json

    class Conn:
        def __init__(self):
            self.frames = [b"\x01ok\n", encode_frame(ERROR, json.dumps({"status": "Success"}).encode())]

        def send(self, d):
            pass

        def recv(self, timeout=None):
            if self.frames:
                return self.frames.pop(0)
            raise ConnectionError("closed")

        def close(self):
            pass

    monkeypatch.setattr(envmod, "connect_exec", lambda client, pod, container, argv: Conn())
    kube = FakeKube()
    e = KubernetesEnvironment(settings=S, identity=ID, cwd="/workspace", timeout=30, client=kube,
                              sleep=lambda s: None)
    assert e.execute("echo hi")["returncode"] == 0
    kube.calls.clear()
    envmod._default_exec_connect(kube)(ID.pod_name, "work", ["true"], b"", lambda: None).wait(5)
    assert kube.calls == []          # liveness lives in _before_execute, on the caller's thread
    kube.pods.clear()
    result = e.execute("echo hi", timeout=10)
    assert result["returncode"] == 0 and result["environment_recreated"] is True
    assert kube.calls.count(("create", ID.pod_name)) == 1


def test_session_snapshot_bootstrapped_lazily_and_again_after_recreate():
    e, kube, ex = _env()
    e.execute("echo hi")
    logins = [r for r in ex.runs if r["argv"][-2] == "1"]
    assert len(logins) == 1 and len(ex.runs) == 2   # bootstrap (login) then the command
    assert logins[0] is ex.runs[0]
    assert e._snapshot_ready is True
    kube.pods.clear()
    e._pod_ready = False
    n = len(ex.runs)
    e.execute("echo hi")
    assert b"mkdir -p -- /workspace" in ex.runs[n]["payload"]  # cwd restored first (non-login)
    assert ex.runs[n]["argv"][-2] == "0"
    assert ex.runs[n + 1]["argv"][-2] == "1"        # bootstrap again in the new Pod


def _mkdir_runs(ex, path):
    return [r for r in ex.runs if f"mkdir -p -- {path}".encode() in r["payload"]]


def test_recreate_keeps_cwd_by_creating_it_in_the_new_pod():
    e, kube, ex = _env()
    e.ensure_pod()
    assert _mkdir_runs(ex, "/workspace") == []      # first Pod: nothing to restore
    e.cwd = "/workspace/sub"
    kube.pods.clear()
    e._pod_ready = False
    e.ensure_pod()
    runs = _mkdir_runs(ex, "/workspace/sub")
    assert len(runs) == 1 and runs[0]["container"] == "work"
    assert e.cwd == "/workspace/sub"


def test_recreate_falls_back_to_initial_cwd_when_mkdir_fails():
    import json

    from kubernetes_sandbox.exec_stream import ERROR, StreamingExecHandle, encode_frame

    inner = FakeExec()

    class FailConn:
        def __init__(self):
            self.frames = [encode_frame(ERROR, json.dumps({"status": "Failure", "details": {
                "causes": [{"reason": "ExitCode", "message": "1"}]}}).encode())]

        def send(self, d):
            pass

        def recv(self, timeout=None):
            if self.frames:
                return self.frames.pop(0)
            raise ConnectionError("closed")

        def close(self):
            pass

    def ex(pod, container, argv, payload, on_kill):
        if b"mkdir -p -- " in payload:
            inner.runs.append({"payload": payload})
            return StreamingExecHandle(lambda: FailConn(), payload, on_kill)
        return inner(pod, container, argv, payload, on_kill)

    e, kube, _ = _env(ex=ex)
    e.ensure_pod()
    e.cwd = "/read-only/sub"
    kube.pods.clear()
    e._pod_ready = False
    e.ensure_pod()
    assert len(_mkdir_runs(inner, "/read-only/sub")) == 1
    assert e.cwd == "/workspace"


def test_capacity_ignores_finished_and_terminating_pods():
    kube = FakeKube(ready_after=0)
    s = Settings(runtime_class_name="kata", docker_disk_storage_class="b", max_per_profile=1)
    kube.create_pod(render_pod(s, Identity("mars", "done"), now_iso="x", created_by="x"))
    kube.create_pod(render_pod(s, Identity("mars", "dying"), now_iso="x", created_by="x"))
    kube.pods[Identity("mars", "done").pod_name]["status"] = {"phase": "Succeeded"}
    kube.pods[Identity("mars", "dying").pod_name]["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    e, _, _ = _env(kube=kube, settings=s)
    kube._gets[Identity("mars", "done").pod_name] = -100
    e.ensure_pod()
    assert e._pod_ready


# --- persistent workspace volume -----------------------------------------------------
def _claim(settings=S, ident=ID):
    from kubernetes_sandbox.podspec import render_workspace_claim
    return render_workspace_claim(settings, ident)


def test_workspace_pvc_is_created_before_the_pod():
    e, kube, _ = _env()
    e.ensure_pod()
    creates = [c for c in kube.calls if c[0] in ("create", "create_pvc")]
    assert creates == [("create_pvc", ID.workspace_claim), ("create", ID.pod_name)]
    assert kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][
        "hermes-sandbox/workspace-retention-seconds"] == str(S.workspace_retention_seconds)
    vols = {v["name"]: v for v in kube.pods[ID.pod_name]["spec"]["volumes"]}
    assert vols["workspace"]["persistentVolumeClaim"]["claimName"] == ID.workspace_claim


def test_existing_workspace_pvc_is_adopted():
    e, kube, _ = _env()
    kube.create_pvc(_claim())
    uid = kube.pvcs[ID.workspace_claim]["metadata"]["uid"]
    kube.calls.clear()
    e.ensure_pod()
    assert kube.calls.count(("create_pvc", ID.workspace_claim)) == 0
    assert kube.pvcs[ID.workspace_claim]["metadata"]["uid"] == uid
    assert ("create", ID.pod_name) in kube.calls


def test_conflict_creating_the_pvc_adopts_it():
    kube = FakeKube()
    real = kube.create_pvc

    def racing(manifest):
        real(manifest)           # another gateway wins the race
        raise Conflict(409, "AlreadyExists", manifest["metadata"]["name"])

    kube.create_pvc = racing
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert e._pod_ready and ID.workspace_claim in kube.pvcs


def test_terminating_pvc_is_waited_out_then_recreated():
    kube = FakeKube(pvc_gone_after=2)
    kube.create_pvc(_claim())
    old_uid = kube.pvcs[ID.workspace_claim]["metadata"]["uid"]
    kube.pvcs[ID.workspace_claim]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"
    kube.calls.clear()
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    new = kube.pvcs[ID.workspace_claim]["metadata"]
    assert new["uid"] != old_uid and "deletionTimestamp" not in new
    calls = [c for c in kube.calls if c[0] in ("get_pvc", "create_pvc", "create")]
    assert calls.index(("create_pvc", ID.workspace_claim)) < calls.index(("create", ID.pod_name))
    assert calls.count(("get_pvc", ID.workspace_claim)) >= 3


def test_pvc_that_never_finishes_terminating_is_a_connection_error():
    kube = FakeKube(pvc_gone_after=10**9)
    kube.create_pvc(_claim())
    kube.pvcs[ID.workspace_claim]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"
    ticks = iter(range(0, 10**6, 5))
    e = KubernetesEnvironment(settings=S, identity=ID, cwd="/workspace", timeout=30, client=kube,
                              exec_connect=FakeExec(), clock=lambda: float(next(ticks)), sleep=lambda s: None)
    with pytest.raises(EnvironmentConnectionError, match="being deleted"):
        e.ensure_pod()
    assert ("create", ID.pod_name) not in kube.calls


@pytest.mark.parametrize("tamper", [
    lambda md: md["labels"].__setitem__("hermes-sandbox/key-hash", "0000000000"),
    lambda md: md["labels"].pop("app.kubernetes.io/managed-by"),
    lambda md: md["labels"].__setitem__("hermes-sandbox/profile", "venus"),
    lambda md: md["annotations"].__setitem__("hermes-sandbox/session-key", "someone-else"),
])
def test_label_mismatched_pvc_is_never_mounted(tamper):
    kube = FakeKube()
    kube.create_pvc(_claim())
    tamper(kube.pvcs[ID.workspace_claim]["metadata"])
    e, _, _ = _env(kube=kube)
    with pytest.raises(EnvironmentConnectionError, match="workspace volume") as ei:
        e.ensure_pod()
    assert "retrying will not help" in ei.value.retry_hint
    assert ("create", ID.pod_name) not in kube.calls
    assert ID.workspace_claim in kube.pvcs


@pytest.mark.parametrize("method", ["get_pvc", "create_pvc"])
@pytest.mark.parametrize("exc", [
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(500, "InternalError", "etcd down"),
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(403, "Forbidden", "no pvc/create"),
    __import__("httpx").ConnectError("connection refused"),
])
def test_pvc_api_errors_are_connection_errors(method, exc):
    e, kube, _ = _env()
    setattr(kube, method, _raising(exc))
    with pytest.raises(EnvironmentConnectionError) as ei:
        e.ensure_pod()
    assert "Bearer" not in str(ei.value)
    if hasattr(exc, "status"):
        assert str(exc.status) in str(ei.value) and exc.reason in str(ei.value)
    assert ("create", ID.pod_name) not in kube.calls


def test_pvc_quota_exceeded_is_capacity_error():
    from kubernetes_sandbox.kube import QuotaExceeded
    e, kube, _ = _env()
    kube.create_pvc = _raising(QuotaExceeded(403, "Forbidden", "exceeded quota: persistentvolumeclaims"))
    with pytest.raises(EnvironmentConnectionError, match="capacity exhausted") as ei:
        e.ensure_pod()
    assert "idle" in ei.value.retry_hint


def test_pvc_is_not_created_when_the_profile_is_at_capacity():
    kube = FakeKube(ready_after=0)
    s = Settings(runtime_class_name="kata", docker_disk_storage_class="b", max_per_profile=1)
    kube.create_pod(render_pod(s, Identity("mars", "other"), now_iso="x", created_by="x"))
    e, _, _ = _env(kube=kube, settings=s)
    with pytest.raises(EnvironmentConnectionError, match="limit reached"):
        e.ensure_pod()
    assert kube.pvcs == {}


def test_unready_adopted_pod_re_ensures_its_workspace():
    # The janitor reaped the PVC as a new Pod was being created: the Pod sits Pending on a
    # missing claim. Re-creating the claim lets it schedule.
    kube = FakeKube(ready_after=2)
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="other"))
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert ID.workspace_claim in kube.pvcs and e._pod_ready


def test_cleanup_never_deletes_the_workspace_pvc():
    e, kube, _ = _env()
    e.execute("echo hi")
    e.cleanup()
    assert ID.pod_name not in kube.pods
    assert ID.workspace_claim in kube.pvcs
    assert not any(c[0] == "delete_pvc" for c in kube.calls)


def test_new_pod_after_cleanup_mounts_the_same_pvc():
    e, kube, _ = _env()
    e.execute("echo hi")
    uid = kube.pvcs[ID.workspace_claim]["metadata"]["uid"]
    e.cleanup()
    e2, _, _ = _env(kube=kube)
    assert e2.execute("echo hi")["environment_recreated"] is True
    assert kube.pvcs[ID.workspace_claim]["metadata"]["uid"] == uid
    assert kube.calls.count(("create_pvc", ID.workspace_claim)) == 1



# --- carry-over R2-R5 ----------------------------------------------------------------------------
def test_template_hash_replacement_after_restart_marks_recreated():
    kube = FakeKube(ready_after=0)
    old = render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old-gateway")
    old["metadata"]["labels"][LBL_TEMPLATE_HASH] = "stale"
    kube.create_pod(old)
    e, _, _ = _env(kube=kube)       # fresh process: nothing served yet
    assert e.execute("echo hi")["environment_recreated"] is True
    assert ("delete", ID.pod_name) in kube.calls


def test_replacing_another_sessions_pod_is_not_a_recreate():
    kube = FakeKube(ready_after=0)
    foreign = render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old")
    foreign["metadata"]["annotations"][ANN_SESSION_KEY] = "someone-else"
    kube.create_pod(foreign)
    e, _, _ = _env(kube=kube)
    assert e.execute("echo hi").get("environment_recreated") is None


def test_pod_ready_is_set_only_after_restore_cwd():
    e, kube, _ = _env()
    e.ensure_pod()
    seen = []
    real = e._restore_cwd

    def spy():
        seen.append(e._pod_ready)
        real()

    e._restore_cwd = spy
    kube.pods.clear()
    e._pod_ready = False
    e.ensure_pod()                                   # recreate path
    kube.pods.clear()
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="another-gateway"))
    e._pod_ready = False
    e.ensure_pod()                                   # adopt-a-replacement path
    assert seen == [False, False] and e._pod_ready


def test_liveness_check_retries_a_transient_error_once():
    from kubernetes_sandbox.kube import KubeError
    e, kube, _ = _env()
    e.execute("echo hi")
    real, fails = kube.get_pod, [KubeError(503, "ServiceUnavailable", "blip")]

    def flaky(name):
        if fails:
            raise fails.pop()
        return real(name)

    kube.get_pod = flaky
    result = e.execute("echo hi")
    assert result["returncode"] == 0 and result.get("environment_recreated") is None


def test_liveness_check_gives_up_after_the_retry():
    from kubernetes_sandbox.kube import KubeError
    e, kube, _ = _env()
    e.execute("echo hi")
    calls = []

    def down(name):
        calls.append(name)
        raise KubeError(503, "ServiceUnavailable", "down")

    kube.get_pod = down
    with pytest.raises(EnvironmentConnectionError, match="503"):
        e.execute("echo hi")
    assert len(calls) == 2


def test_non_json_2xx_body_is_a_connection_error():
    import json
    e, kube, _ = _env()
    kube.get_pod = _raising(json.JSONDecodeError("Expecting value", "<html>secret</html>", 0))
    with pytest.raises(EnvironmentConnectionError, match="JSONDecodeError") as ei:
        e.ensure_pod()
    assert "secret" not in str(ei.value)
    e2, kube2, _ = _env()
    e2.execute("echo hi")
    kube2.get_pod = _raising(json.JSONDecodeError("Expecting value", "<html/>", 0))
    with pytest.raises(EnvironmentConnectionError, match="JSONDecodeError"):
        e2.execute("echo hi")


# --- fix round 1 ----------------------------------------------------------------------------------
UNUSED = "hermes-sandbox/unused-since"


def test_replace_path_deletes_gracefully_never_force():
    kube = FakeKube(ready_after=0)
    old = render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old")
    old["metadata"]["labels"][LBL_TEMPLATE_HASH] = "stale"
    kube.create_pod(old)
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert kube.deletes and all(grace > 0 for _, grace in kube.deletes)


def test_leftover_terminating_pod_is_deleted_gracefully_and_waited_out():
    kube = FakeKube(ready_after=0)
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old"))
    kube.pods[ID.pod_name]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"   # cleanup() in flight
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert kube.deletes == [(ID.pod_name, 10)]
    assert e._pod_ready


def test_previous_pod_still_terminating_is_retryable_and_never_creates():
    kube = FakeKube(ready_after=0)
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="old"))
    kube.pods[ID.pod_name]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"
    kube.pods_linger = True
    kube.calls.clear()
    ticks = iter(range(0, 10**6, 5))
    e = KubernetesEnvironment(settings=S, identity=ID, cwd="/workspace", timeout=30, client=kube,
                              exec_connect=FakeExec(), clock=lambda: float(next(ticks)), sleep=lambda s: None)
    with pytest.raises(EnvironmentConnectionError, match="still terminating") as ei:
        e.ensure_pod()
    assert "Retry" in ei.value.retry_hint
    assert ("create", ID.pod_name) not in kube.calls
    assert all(grace > 0 for _, grace in kube.deletes)


def test_gone_wait_outlasts_grace_plus_vm_teardown():
    assert envmod._GONE_TIMEOUT >= envmod._DELETE_GRACE + 60


def test_cleanup_deletes_with_the_same_grace():
    e, kube, _ = _env()
    e.ensure_pod()
    e.cleanup()
    assert kube.deletes == [(ID.pod_name, envmod._DELETE_GRACE)] and envmod._DELETE_GRACE > 0


def test_stamped_workspace_is_unstamped_before_the_pod_is_created():
    kube = FakeKube()
    kube.create_pvc(_claim())
    kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    patches = [c for c in kube.calls if c[0] == "patch_pvc"]
    assert patches == [("patch_pvc", ID.workspace_claim, {"metadata": {"annotations": {UNUSED: None}}})]
    names = [c[0] for c in kube.calls]
    assert names.index("patch_pvc") < names.index("create")
    assert UNUSED not in kube.pvcs[ID.workspace_claim]["metadata"]["annotations"]


def test_unstamped_workspace_is_not_patched():
    kube = FakeKube()
    kube.create_pvc(_claim())
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert not any(c[0] == "patch_pvc" for c in kube.calls)


def test_foreign_stamped_pvc_is_not_patched():
    kube = FakeKube()
    kube.create_pvc(_claim())
    md = kube.pvcs[ID.workspace_claim]["metadata"]
    md["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    md["annotations"][ANN_SESSION_KEY] = "someone-else"
    e, _, _ = _env(kube=kube)
    with pytest.raises(EnvironmentConnectionError, match="workspace volume"):
        e.ensure_pod()
    assert not any(c[0] == "patch_pvc" for c in kube.calls)


def test_unstamp_racing_the_janitor_delete_waits_and_creates_fresh():
    kube = FakeKube(pvc_gone_after=1)
    kube.create_pvc(_claim())
    old_uid = kube.pvcs[ID.workspace_claim]["metadata"]["uid"]
    kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    real = kube.patch_pvc

    def janitor_won(name, patch):     # the janitor's DELETE landed between our GET and PATCH
        kube.pvcs[name]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"
        return real(name, patch)

    kube.patch_pvc = janitor_won
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    new = kube.pvcs[ID.workspace_claim]["metadata"]
    assert new["uid"] != old_uid and "deletionTimestamp" not in new
    assert e._pod_ready


def test_unstamp_forbidden_names_the_missing_verb():
    from kubernetes_sandbox.kube import KubeError
    kube = FakeKube()
    kube.create_pvc(_claim())
    kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    kube.patch_pvc = _raising(KubeError(403, "Forbidden", "cannot patch persistentvolumeclaims"))
    e, _, _ = _env(kube=kube)
    with pytest.raises(EnvironmentConnectionError, match="403") as ei:
        e.ensure_pod()
    assert "patch" in ei.value.retry_hint and "persistentvolumeclaims" in ei.value.retry_hint
    assert ("create", ID.pod_name) not in kube.calls


@pytest.mark.parametrize("status,reason", [(400, "BadRequest"), (401, "Unauthorized"), (403, "Forbidden"),
                                           (409, "Conflict")])
def test_liveness_does_not_retry_client_errors(status, reason):
    from kubernetes_sandbox.kube import KubeError
    e, kube, _ = _env()
    e.execute("echo hi")
    calls = []

    def fail(name):
        calls.append(name)
        raise KubeError(status, reason, "no")

    kube.get_pod = fail
    with pytest.raises(EnvironmentConnectionError, match=str(status)):
        e.execute("echo hi")
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(429, "TooManyRequests", "slow down"),
    __import__("kubernetes_sandbox.kube", fromlist=["KubeError"]).KubeError(500, "InternalError", "x"),
    __import__("httpx").ConnectError("refused"),
])
def test_liveness_retries_transport_5xx_and_429(exc):
    e, kube, _ = _env()
    e.execute("echo hi")
    real, fails = kube.get_pod, [exc]

    def flaky(name):
        if fails:
            raise fails.pop()
        return real(name)

    kube.get_pod = flaky
    assert e.execute("echo hi")["returncode"] == 0


def test_pvc_quota_hint_is_about_storage():
    from kubernetes_sandbox.kube import QuotaExceeded
    e, kube, _ = _env()
    kube.create_pvc = _raising(QuotaExceeded(403, "Forbidden", "exceeded quota: requests.storage"))
    with pytest.raises(EnvironmentConnectionError, match="capacity exhausted") as ei:
        e.ensure_pod()
    assert "workspace_retention" in ei.value.retry_hint and "quota" in ei.value.retry_hint


# --- fix round 2 ----------------------------------------------------------------------------------
def test_existing_pvc_with_other_access_mode_is_adopted_as_is_and_warned_once(caplog):
    import logging
    kube = FakeKube()
    rwo = Settings(runtime_class_name="kata", docker_disk_storage_class="b", ready_timeout_seconds=5,
                   workspace_access_mode="ReadWriteOnce")
    kube.create_pvc(_claim(settings=rwo))
    uid = kube.pvcs[ID.workspace_claim]["metadata"]["uid"]
    e, _, _ = _env(kube=kube)                      # default setting: ReadWriteOncePod
    with caplog.at_level(logging.WARNING):
        e.ensure_pod()
        kube.pods.clear()
        e._pod_ready = False
        e.ensure_pod()
    assert kube.pvcs[ID.workspace_claim]["metadata"]["uid"] == uid
    assert kube.pvcs[ID.workspace_claim]["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert not any(c[0] == "delete_pvc" for c in kube.calls)
    warnings = [r for r in caplog.records if "access mode" in r.getMessage()]
    assert len(warnings) == 1 and ID.workspace_claim in warnings[0].getMessage()


def test_adopting_a_ready_pod_clears_the_idle_stamp():
    kube = FakeKube(ready_after=0)
    kube.create_pvc(_claim())
    kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="crashed-gateway"))
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert UNUSED not in kube.pvcs[ID.workspace_claim]["metadata"]["annotations"]
    assert kube.calls.count(("create_pvc", ID.workspace_claim)) == 1   # the existing one, kept
    assert kube.calls.count(("create", ID.pod_name)) == 1              # the crashed gateway's, adopted


def test_adopting_a_ready_pod_tolerates_a_transient_unstamp_failure():
    from kubernetes_sandbox.kube import KubeError
    kube = FakeKube(ready_after=0)
    kube.create_pvc(_claim())
    kube.pvcs[ID.workspace_claim]["metadata"]["annotations"][UNUSED] = "2026-10-01T00:00:00Z"
    kube.create_pod(render_pod(S, ID, now_iso="2026-01-01T00:00:00Z", created_by="crashed-gateway"))
    kube.patch_pvc = _raising(KubeError(503, "ServiceUnavailable", "blip"))
    e, _, _ = _env(kube=kube)
    e.ensure_pod()
    assert e._pod_ready


def test_pvc_quota_hint_names_count_and_storage():
    from kubernetes_sandbox.kube import QuotaExceeded
    e, kube, _ = _env()
    kube.create_pvc = _raising(QuotaExceeded(403, "Forbidden", "exceeded quota: persistentvolumeclaims"))
    with pytest.raises(EnvironmentConnectionError) as ei:
        e.ensure_pod()
    assert "PVC count" in ei.value.retry_hint and "storage" in ei.value.retry_hint
