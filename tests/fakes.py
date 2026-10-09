import copy

from kubernetes_sandbox.kube import NotFound, QuotaExceeded


class FakeKube:
    """In-memory Pod store. `ready_after` = get_pod calls before a created Pod reports Ready."""

    def __init__(self, ready_after=1, quota=None):
        self.pods, self.calls, self.ready_after, self.quota = {}, [], ready_after, quota
        self._gets = {}

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
        pod["status"] = {"phase": "Pending"}
        self.pods[manifest["metadata"]["name"]] = pod
        self._gets[manifest["metadata"]["name"]] = 0
        return copy.deepcopy(pod)

    def delete_pod(self, name, grace_seconds=10):
        self.calls.append(("delete", name))
        self.pods.pop(name, None)

    def list_pods(self, label_selector):
        k, _, v = label_selector.partition(",")[2].partition("=") if "," in label_selector else ("", "", "")
        return [copy.deepcopy(p) for p in self.pods.values()
                if not k or p["metadata"]["labels"].get(k) == v]

    def patch_pod_annotations(self, name, annotations):
        self.calls.append(("patch", name))
        self.pods[name]["metadata"]["annotations"].update(annotations)


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
