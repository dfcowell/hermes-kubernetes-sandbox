import copy
import uuid

from kubernetes_sandbox.kube import Conflict, NotFound, QuotaExceeded


class FakeKube:
    """In-memory Pod and PVC store. `ready_after` = get_pod calls before a created Pod reports Ready.
    A PVC with a deletionTimestamp disappears after `pvc_gone_after` further get_pvc calls."""

    def __init__(self, ready_after=1, quota=None, pvc_gone_after=2):
        self.pods, self.calls, self.ready_after, self.quota = {}, [], ready_after, quota
        self._gets = {}
        self.pvcs, self.pvc_gone_after, self._pvc_gets = {}, pvc_gone_after, {}
        self.deletes = []              # (pod name, grace_seconds)
        self.pods_linger = False       # True: a deleted Pod stays Terminating forever

    def _ready(self, pod):
        pod.setdefault("status", {})
        pod["status"].update({"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]})

    def get_pod(self, name):
        self.calls.append(("get", name))
        if name not in self.pods:
            raise NotFound(404, "NotFound", name)
        self._gets[name] = self._gets.get(name, 0) + 1
        if self._gets[name] >= self.ready_after:
            self._ready(self.pods[name])
        return copy.deepcopy(self.pods[name])

    def create_pod(self, manifest):
        self.calls.append(("create", manifest["metadata"]["name"]))
        if self.quota is not None and len(self.pods) >= self.quota:
            raise QuotaExceeded(403, "Forbidden", "exceeded quota: sandbox-capacity")
        pod = copy.deepcopy(manifest)
        pod["metadata"]["uid"] = str(uuid.uuid4())
        pod["status"] = {"phase": "Pending"}
        self.pods[manifest["metadata"]["name"]] = pod
        self._gets[manifest["metadata"]["name"]] = 0
        return copy.deepcopy(pod)

    def delete_pod(self, name, grace_seconds=10):
        self.calls.append(("delete", name))
        self.deletes.append((name, grace_seconds))
        if self.pods_linger and name in self.pods:
            self.pods[name]["metadata"]["deletionTimestamp"] = "2026-10-09T00:00:00Z"
            return
        self.pods.pop(name, None)

    def list_pods(self, label_selector):
        k, _, v = label_selector.partition(",")[2].partition("=") if "," in label_selector else ("", "", "")
        return [copy.deepcopy(p) for p in self.pods.values()
                if not k or p["metadata"]["labels"].get(k) == v]

    def patch_pod_annotations(self, name, annotations):
        self.calls.append(("patch", name))
        self.pods[name]["metadata"]["annotations"].update(annotations)


    def get_pvc(self, name):
        self.calls.append(("get_pvc", name))
        pvc = self.pvcs.get(name)
        if pvc is None:
            raise NotFound(404, "NotFound", name)
        if pvc["metadata"].get("deletionTimestamp"):
            self._pvc_gets[name] = self._pvc_gets.get(name, 0) + 1
            if self._pvc_gets[name] > self.pvc_gone_after:
                del self.pvcs[name]
                raise NotFound(404, "NotFound", name)
        return copy.deepcopy(pvc)

    def create_pvc(self, manifest):
        name = manifest["metadata"]["name"]
        self.calls.append(("create_pvc", name))
        if name in self.pvcs:
            raise Conflict(409, "AlreadyExists", name)
        pvc = copy.deepcopy(manifest)
        pvc["metadata"]["uid"] = str(uuid.uuid4())
        self.pvcs[name] = pvc
        return copy.deepcopy(pvc)

    def list_pvcs(self, label_selector):
        return [copy.deepcopy(p) for p in self.pvcs.values()]

    def patch_pvc(self, name, merge_patch):
        self.calls.append(("patch_pvc", name, copy.deepcopy(merge_patch)))
        if name not in self.pvcs:
            raise NotFound(404, "NotFound", name)
        ann = self.pvcs[name]["metadata"].setdefault("annotations", {})
        for k, v in ((merge_patch.get("metadata") or {}).get("annotations") or {}).items():
            if v is None:
                ann.pop(k, None)
            else:
                ann[k] = v
        return copy.deepcopy(self.pvcs[name])

    def delete_pvc(self, name, *, uid, resource_version=None):
        self.calls.append(("delete_pvc", name))
        if name in self.pvcs and self.pvcs[name]["metadata"]["uid"] == uid:
            del self.pvcs[name]


class FakeExec:
    """exec_connect replacement: runs nothing; returns a handle with scripted results."""

    def __init__(self):
        self.runs = []
        self.fail_with_notfound_once = False

    def __call__(self, pod, container, argv, payload, on_kill):
        from kubernetes_sandbox.exec_stream import StreamingExecHandle, encode_frame, ERROR
        import json
        self.runs.append({"pod": pod, "container": container, "argv": argv, "payload": payload})
        if self.fail_with_notfound_once:
            self.fail_with_notfound_once = False
            raise NotFound(404, "NotFound", pod)

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

        return StreamingExecHandle(lambda: Conn(), payload, on_kill)


class LocalExec:
    """exec_connect replacement that RUNS the runner on this machine (bash), speaking the exec channel protocol:
    the stdin frames are collected, the runner is started with them on its stdin (closed afterwards: the runner
    delimits its stdin in-band, so only Kata cares, see exec_stream.py), and its stdout/stderr and exit status
    come back as channel 1/2/3 frames. The "Pod" is this host, so paths in commands must exist here."""

    def __init__(self):
        self.runs = []

    def __call__(self, pod, container, argv, payload, on_kill):
        from kubernetes_sandbox.exec_stream import ERROR, STDERR, STDIN, STDOUT, StreamingExecHandle, encode_frame
        import json
        import subprocess
        self.runs.append({"pod": pod, "container": container, "argv": argv, "payload": payload})

        class Conn:
            def __init__(self):
                self.stdin, self.frames = b"", None

            def send(self, d):
                assert d[0] == STDIN, d[:1]
                self.stdin += d[1:]

            def recv(self, timeout=None):
                if self.frames is None:
                    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    out, err = proc.communicate(self.stdin, timeout=60)
                    rc = proc.returncode
                    status = ({"status": "Success"} if rc == 0 else
                              {"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": str(rc)}]}})
                    self.frames = [encode_frame(STDOUT, out), encode_frame(STDERR, err),
                                   encode_frame(ERROR, json.dumps(status).encode())]
                if self.frames:
                    return self.frames.pop(0)
                raise ConnectionError("closed")

            def close(self):
                pass

        return StreamingExecHandle(lambda: Conn(), payload, on_kill)
