# Kata example (Docker-in-Docker, hardware-isolated)

Each session is a Pod running in its own Kata Containers micro-VM, with a Docker-in-Docker
sidecar. The privileged `dind` container is privileged **inside the VM**, so the host kernel is
not exposed. This is the recommended deployment when agents need `docker`.

## Prerequisites

- Kubernetes >= 1.30 (ValidatingAdmissionPolicy).
- The default `docker_cli: image-volume` needs the `ImageVolume` feature enabled in your cluster and a
  CRI that supports it (containerd >= 2.1 or CRI-O); verified on Kubernetes 1.36 with containerd 2.2.3.
  Otherwise (for example on Kubernetes < 1.33, where image volumes are unavailable) set
  `docker_cli: init-copy`.
- Kata Containers with **Cloud Hypervisor** on the sandbox nodes, configured as in
  [kata-node-config.md](kata-node-config.md) (not optional).
- A StorageClass that provisions **Block** volumes (`volumeMode: Block`, access mode `ReadWriteOncePod`); the Docker disk is a
  generic ephemeral volume that lives and dies with the Pod.
- A StorageClass for the per-session **workspace** volumes (filesystem, access mode
  `ReadWriteOncePod`, which needs a CSI driver with `SINGLE_NODE_SINGLE_WRITER`, or set
  `workspace_access_mode: ReadWriteOnce`; the cluster default is used unless
  `workspace_storage_class` is set; see the top-level README, "Storage requirements"). The workspace outlives the session's Pods; under Kata it
  is shared into the VM over virtio-fs (see the top-level README, "Workspace persistence").
- A CNI that enforces NetworkPolicy. With Cilium, `socketLB.hostNamespaceOnly: true` is mandatory:
  without it Service connectivity from Kata Pods breaks (see networkpolicy.yaml).

## Apply order

The manifests assume the Hermes namespace is `hermes`; change it in `rbac.yaml` and `janitor.yaml`
if yours differs.

```sh
kubectl apply -f runtimeclass.yaml
kubectl apply -f namespace.yaml
kubectl apply -f admission-policy.yaml      # BEFORE anything can create Pods in agent-sandbox
kubectl apply -f networkpolicy.yaml
kubectl apply -f resourcequota.yaml
kubectl apply -f profile-configmap.yaml
kubectl apply -f rbac.yaml
kubectl apply -f janitor.yaml
```

Then put `kubernetes-sandbox.yaml` (edit the StorageClass) in `$HERMES_HOME` and set
`terminal.backend: kubernetes` in Hermes. Optional settings in that file:

- Forge access: `forward_env` + `git_credentials` + `extra_pod_labels` and the commented policy in
  `networkpolicy.yaml` (README, Credentials in the sandbox).

## Give the gateway the driver credentials

The plugin talks to the API server as the `hermes-sandbox-driver` ServiceAccount (`rbac.yaml`:
Pods, `pods/exec`, `pods/log`, and create/get/list/patch on PersistentVolumeClaims, in `agent-sandbox` only). Run the Hermes Pod as that ServiceAccount, turn
off the automatic token mount, and project a short-lived token plus the cluster CA on the **gateway
container only**, so no other container in the Pod sees it. The plugin re-reads the token file on
every request, so kubelet's rotation just works.

```yaml
# in the Hermes Pod spec
serviceAccountName: hermes-sandbox-driver
automountServiceAccountToken: false
volumes:
  - name: sandbox-driver
    projected:
      defaultMode: 0440
      sources:
        - serviceAccountToken: {path: token, expirationSeconds: 3600}
        - configMap:
            name: kube-root-ca.crt
            items: [{key: ca.crt, path: ca.crt}]
containers:
  - name: gateway              # only this container
    volumeMounts:
      - {name: sandbox-driver, mountPath: /var/run/secrets/hermes-sandbox, readOnly: true}
```

This matches `token_path` / `ca_path` in `kubernetes-sandbox.yaml`. Projected files are owned by root and
group-readable per `defaultMode`; a non-root gateway needs `securityContext.fsGroup` (or `runAsGroup`) set
so its group can read them.

If the Hermes Pod must keep its own ServiceAccount, edit the RoleBinding in `rbac.yaml` to bind
that ServiceAccount instead and project its token the same way.

For a gateway running **outside the cluster**, mint a bounded token and write it to `token_path`:
`kubectl -n hermes create token hermes-sandbox-driver --duration=24h` (renew before it expires).

**Fallback, not recommended:** a long-lived token Secret
(`type: kubernetes.io/service-account-token`, annotation `kubernetes.io/service-account.name:
hermes-sandbox-driver`). It never expires, is not bound to the Pod, and anyone who can read Secrets in
that namespace can use it. Prefer the projected token above.

## Janitor (required)

Sandbox Pods come and go: the plugin deletes a session's Pod when Hermes ends the environment
(session close, idle reaper, a graceful gateway stop, eviction after an infrastructure error),
and the next command starts a new Pod that mounts the same workspace volume. The janitor is
**required**, because it is the only thing that ever deletes a workspace volume. Every 5 minutes
`janitor.yaml`:

- reaps idle Pods (`IDLE_SECONDS`), finished Pods, Pods past `max_lifetime_seconds` (the janitor
  is what enforces that annotation) and Pods a crashed (killed) gateway left behind;
- stamps each workspace PVC that no Pod mounts with `hermes-sandbox/unused-since` (the plugin
  clears that stamp whenever it starts a Pod on the volume, and the janitor clears it when it sees
  the volume mounted), and deletes it once it has been unmounted for longer than its retention,
  only if the PVC is unchanged since the janitor listed it (uid + resourceVersion preconditions);
  retention is the PVC's `hermes-sandbox/workspace-retention-seconds` annotation (the plugin's
  `workspace_retention`, default 7 days), else `WORKSPACE_RETENTION_SECONDS`.

For draining nodes and force-deleting sandbox Pods safely, see "Operating: node drains and
force-deletes" in the top-level README.

Set `NAMESPACE` to your sandbox namespace and the CronJob/ConfigMap namespace to your Hermes
namespace.

### First manual janitor run

```sh
kubectl -n hermes create job --from=cronjob/hermes-sandbox-janitor janitor-first-run
kubectl -n hermes logs job/janitor-first-run
```

To see what it would delete without deleting, add `DRY_RUN=1` to the CronJob's env first.

## Measured behaviour

Measured on Kata 3.28.0 / Cloud Hypervisor, containerd 2.2.3, Kubernetes 1.36.5.

- Cold start (Pod created to ready): about 27 s on Kata, about 14 s on runc.
- Real test suites run at about 1.5x their runc time.
- Exec latency p50 / p95: about 180 / 227 ms.
- Guest memory about 2 GiB (512 MiB Kata base + container limits); peak in-guest need 703 MiB.
- 3.5 h load test: clean, pod cgroup peak 1474 of 2560 MiB, with `O_DIRECT` enabled. Without it
  a long-running load test OOM-killed the whole VM: the host cached the raw-block Docker disk a
  second time, charged to the Pod's cgroup.
