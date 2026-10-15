"""Render a sandbox Pod manifest (pure; no cluster access)."""
from __future__ import annotations

import copy
import hashlib
import json

from .naming import (ANN_CREATED_BY, ANN_LAST_USED, ANN_MAX_LIFETIME, ANN_SESSION_KEY, ANN_WS_RETENTION,
                     LBL_COMPONENT, LBL_KEY_HASH, LBL_PROFILE, LBL_TEMPLATE_HASH, MANAGED_BY, Identity, slug)
from .settings import Settings

# Restricted-PSA compliant. Applied per container, not pod-wide: dind and the formatter run as root.
_WORK_SC = {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True, "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
DOCKER_GID = 2375
_CLI_DIR = "/opt/docker-cli"


def _spec(s: Settings, claim: str) -> dict:
    work = {
        "name": "work", "image": s.work_image, "command": ["sleep", "infinity"],
        "securityContext": copy.deepcopy(_WORK_SC),
        "env": [], "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
        "workingDir": "/workspace",
        "resources": {"requests": {"cpu": s.work_cpu, "memory": s.work_memory},
                      "limits": {"memory": s.work_memory}},
    }
    spec: dict = {
        "automountServiceAccountToken": False, "restartPolicy": "Never", "enableServiceLinks": False,
        # OnRootMismatch: chown the workspace once, not on every remount of a grown volume.
        "securityContext": {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"},
        "containers": [work],
        "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": claim}}],
    }
    if s.runtime_class_name:
        spec["runtimeClassName"] = s.runtime_class_name
    if not s.docker:
        return spec

    spec["securityContext"]["supplementalGroups"] = [DOCKER_GID]
    work["env"].append({"name": "DOCKER_HOST", "value": "unix:///run/docker/docker.sock"})
    # Probe from work, not dind: exec into the dockerd container fails under Kata.
    work["readinessProbe"] = {"exec": {"command": [f"{_CLI_DIR}/bin/docker", "info"]},
                              "periodSeconds": 2, "failureThreshold": 3}
    work["volumeMounts"] += [
        {"name": "docker-sock", "mountPath": "/run/docker"},
        {"name": "profile", "mountPath": "/etc/profile.d/docker-cli.sh", "subPath": "docker-cli.sh",
         "readOnly": True},
    ]
    spec["volumes"] += [
        {"name": "docker-sock", "emptyDir": {}},
        {"name": "profile", "configMap": {"name": s.profile_configmap}},
        {"name": "docker-disk", "ephemeral": {"volumeClaimTemplate": {"spec": {
            "storageClassName": s.docker_disk_storage_class, "volumeMode": "Block",
            "accessModes": ["ReadWriteOncePod"], "resources": {"requests": {"storage": s.docker_disk_size}}}}}},
    ]
    init = [{
        "name": "format", "image": s.formatter_image,
        "command": ["/bin/sh", "-c",
                    "blkid -p /dev/docker-disk >/dev/null 2>&1 || mkfs.ext4 -q -L docker /dev/docker-disk"],
        "volumeDevices": [{"name": "docker-disk", "devicePath": "/dev/docker-disk"}],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
    }]
    if s.docker_cli == "image-volume":
        spec["volumes"].append({"name": "dind-image", "image": {"reference": s.dind_image,
                                                                "pullPolicy": "IfNotPresent"}})
        work["volumeMounts"] += [
            {"name": "dind-image", "mountPath": f"{_CLI_DIR}/bin", "subPath": "usr/local/bin", "readOnly": True},
            {"name": "dind-image", "mountPath": "/usr/local/libexec/docker/cli-plugins",
             "subPath": "usr/local/libexec/docker/cli-plugins", "readOnly": True},
        ]
    else:
        spec["volumes"].append({"name": "docker-cli", "emptyDir": {"sizeLimit": "256Mi"}})
        work["volumeMounts"].append({"name": "docker-cli", "mountPath": _CLI_DIR})
        work["env"].append({"name": "DOCKER_CONFIG", "value": _CLI_DIR})
        init.append({
            "name": "docker-cli", "image": s.cli_image,
            "command": ["/bin/sh", "-c",
                        f"mkdir -p {_CLI_DIR}/bin {_CLI_DIR}/cli-plugins && cp /usr/local/bin/docker {_CLI_DIR}/bin/ "
                        f"&& cp /usr/local/libexec/docker/cli-plugins/docker-* {_CLI_DIR}/cli-plugins/"],
            "volumeMounts": [{"name": "docker-cli", "mountPath": _CLI_DIR}],
            "securityContext": copy.deepcopy(_WORK_SC),
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
        })
    spec["initContainers"] = init
    spec["containers"].insert(0, {
        "name": "dind", "image": s.dind_image, "securityContext": {"privileged": True},
        "command": ["/bin/sh", "-c",
                    "set -eu; mkdir -p /var/lib/docker; mount -t ext4 -o noatime /dev/docker-disk /var/lib/docker; "
                    f"exec dind dockerd --host=unix:///run/docker/docker.sock --group={DOCKER_GID} "
                    "--storage-driver=overlay2 --feature containerd-snapshotter=false"],
        "volumeDevices": [{"name": "docker-disk", "devicePath": "/dev/docker-disk"}],
        "volumeMounts": [{"name": "docker-sock", "mountPath": "/run/docker"},
                         {"name": "workspace", "mountPath": "/workspace"}],
        "resources": {"requests": {"cpu": s.dind_cpu_request, "memory": s.dind_memory},
                      "limits": {"cpu": s.dind_cpu_limit, "memory": s.dind_memory}},
    })
    return spec


# The claim name is per session; hash the template with a fixed placeholder so every session shares it.
_HASH_CLAIM = "<workspace-claim>"


def template_hash(s: Settings) -> str:
    payload = {"spec": _spec(s, _HASH_CLAIM), "annotations": s.extra_pod_annotations}
    if s.extra_pod_labels:  # only when set: an upgrade must not change every existing Pod's hash
        payload["labels"] = s.extra_pod_labels
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]


def identity_labels(ident: Identity) -> dict:
    return {**MANAGED_BY, LBL_PROFILE: slug(ident.profile, 63), LBL_KEY_HASH: ident.key_hash}


def render_pod(s: Settings, ident: Identity, *, now_iso: str, created_by: str) -> dict:
    labels = {**s.extra_pod_labels, **identity_labels(ident), LBL_COMPONENT: "sandbox", LBL_TEMPLATE_HASH: template_hash(s)}
    annotations = {**s.extra_pod_annotations, ANN_SESSION_KEY: ident.session_key, ANN_LAST_USED: now_iso,
                   ANN_MAX_LIFETIME: str(s.max_lifetime_seconds), ANN_CREATED_BY: created_by}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": ident.pod_name, "namespace": s.namespace,
                         "labels": labels, "annotations": annotations},
            "spec": _spec(s, ident.workspace_claim)}


def render_workspace_claim(s: Settings, ident: Identity) -> dict:
    """The session's /workspace volume. No template hash: it must survive template changes.
    ReadWriteOncePod (default) lets exactly one Pod use it; ReadWriteOnce is per node."""
    spec: dict = {"accessModes": [s.workspace_access_mode], "resources": {"requests": {"storage": s.workspace_size}}}
    if s.workspace_storage_class:
        spec["storageClassName"] = s.workspace_storage_class
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": ident.workspace_claim, "namespace": s.namespace,
                         "labels": {**identity_labels(ident), LBL_COMPONENT: "workspace"},
                         "annotations": {ANN_SESSION_KEY: ident.session_key,
                                         ANN_WS_RETENTION: str(s.workspace_retention_seconds)}},
            "spec": spec}
