from kubernetes_sandbox.naming import Identity
from kubernetes_sandbox.podspec import render_pod, template_hash
from kubernetes_sandbox.settings import Settings

KATA = Settings(runtime_class_name="kata", docker_disk_storage_class="block-ephemeral")


def _pod(s=KATA):
    return render_pod(s, Identity("mars", "sess-1"), now_iso="2026-10-09T00:00:00Z", created_by="gw-0")


def _containers(pod):
    return {c["name"]: c for c in pod["spec"]["containers"]}


def test_identity_metadata_and_runtime():
    pod = _pod()
    md = pod["metadata"]
    assert md["name"] == Identity("mars", "sess-1").pod_name
    assert md["labels"]["hermes-sandbox/profile"] == "mars"
    assert md["labels"]["hermes-sandbox/template-hash"] == template_hash(KATA)
    assert md["annotations"]["hermes-sandbox/session-key"] == "sess-1"
    assert pod["spec"]["runtimeClassName"] == "kata"
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["restartPolicy"] == "Never"


def test_only_dind_is_privileged_and_work_is_uid_1000():
    pod = _pod()
    c = _containers(pod)
    assert c["dind"]["securityContext"]["privileged"] is True
    w = c["work"]["securityContext"]
    assert w["runAsUser"] == 1000 and w["allowPrivilegeEscalation"] is False
    assert w["capabilities"]["drop"] == ["ALL"]
    for init in pod["spec"].get("initContainers", []):
        assert not init.get("securityContext", {}).get("privileged")


def test_docker_cli_is_directory_mount_never_emptydir_subpath():
    for mode in ("image-volume", "init-copy"):
        pod = _pod(Settings(runtime_class_name="kata", docker_disk_storage_class="b", docker_cli=mode))
        vols = {v["name"]: v for v in pod["spec"]["volumes"]}
        mounts = _containers(pod)["work"]["volumeMounts"]
        cli = [m for m in mounts if m["mountPath"].startswith("/opt/docker-cli")]
        assert cli, mode
        for m in mounts:
            if "subPath" in m:
                assert "emptyDir" not in vols[m["name"]], (mode, m)
        assert any(m["mountPath"] == "/etc/profile.d/docker-cli.sh" for m in mounts)


def test_docker_disk_is_generic_ephemeral_block():
    vols = {v["name"]: v for v in _pod()["spec"]["volumes"]}
    tmpl = vols["docker-disk"]["ephemeral"]["volumeClaimTemplate"]["spec"]
    assert tmpl["volumeMode"] == "Block" and tmpl["storageClassName"] == "block-ephemeral"


def test_every_container_declares_requests_and_memory_limit():
    pod = _pod()
    for c in pod["spec"]["containers"] + pod["spec"].get("initContainers", []):
        r = c["resources"]
        assert r["requests"]["cpu"] and r["requests"]["memory"] and r["limits"]["memory"], c["name"]


def test_no_docker_means_single_unprivileged_container():
    pod = _pod(Settings(docker=False))
    assert list(_containers(pod)) == ["work"]
    assert "runtimeClassName" not in pod["spec"]
    assert "initContainers" not in pod["spec"]


def test_template_hash_changes_with_spec_not_identity():
    other = Settings(runtime_class_name="kata", docker_disk_storage_class="block-ephemeral", work_memory="1Gi")
    assert template_hash(KATA) != template_hash(other)
    assert template_hash(KATA) == template_hash(KATA)


def test_readiness_probes_docker_from_work_not_dind():
    for mode in ("image-volume", "init-copy"):
        c = _containers(_pod(Settings(runtime_class_name="kata", docker_disk_storage_class="b", docker_cli=mode)))
        assert "readinessProbe" not in c["dind"], mode
        assert c["work"]["readinessProbe"]["exec"]["command"] == ["/opt/docker-cli/bin/docker", "info"], mode
    assert "readinessProbe" not in _containers(_pod(Settings(docker=False)))["work"]


def test_template_hash_covers_extra_pod_annotations():
    other = Settings(runtime_class_name="kata", docker_disk_storage_class="block-ephemeral",
                     extra_pod_annotations={"a": "b"})
    assert template_hash(KATA) != template_hash(other)


def _all_containers(pod):
    return pod["spec"]["containers"] + pod["spec"].get("initContainers", [])


def test_docker_false_pod_satisfies_restricted_pss():
    pod = _pod(Settings(docker=False))
    spec = pod["spec"]
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        assert not spec.get(key)
    assert all("hostPath" not in v for v in spec["volumes"])
    for c in _all_containers(pod):
        sc = c["securityContext"]
        assert sc["runAsNonRoot"] is True
        assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert sc["allowPrivilegeEscalation"] is False
        assert not sc.get("privileged")
        assert all("hostPort" not in p for p in c.get("ports", []))


def test_docker_true_hardens_work_and_cli_but_not_root_containers():
    for mode in ("image-volume", "init-copy"):
        pod = _pod(Settings(runtime_class_name="kata", docker_disk_storage_class="b", docker_cli=mode))
        by = {c["name"]: c for c in _all_containers(pod)}
        hardened = ["work"] + (["docker-cli"] if mode == "init-copy" else [])
        for name in hardened:
            sc = by[name]["securityContext"]
            assert sc["runAsNonRoot"] is True and sc["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for name in ("dind", "format"):
            sc = by[name].get("securityContext", {})
            assert "runAsNonRoot" not in sc and "seccompProfile" not in sc, name
        assert "runAsNonRoot" not in pod["spec"]["securityContext"]
        assert "seccompProfile" not in pod["spec"]["securityContext"]
