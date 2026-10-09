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
`terminal.backend: kubernetes` in Hermes.

## Give the gateway the driver credentials

The plugin talks to the API server as the `hermes-sandbox-driver` ServiceAccount (`rbac.yaml`:
Pods, `pods/exec`, `pods/log` in `agent-sandbox` only). Run the Hermes Pod as that ServiceAccount, turn
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

## Janitor

`janitor.yaml` reaps sandbox Pods left behind by a crashed or stopped gateway, idle Pods
(`IDLE_SECONDS`), finished Pods and Pods past `max_lifetime_seconds` (the janitor is what
enforces that annotation). Set `NAMESPACE` to your sandbox namespace and the CronJob/ConfigMap
namespace to your Hermes namespace.

### First manual janitor run

```sh
kubectl -n hermes create job --from=cronjob/hermes-sandbox-janitor janitor-first-run
kubectl -n hermes logs job/janitor-first-run
```

To see what it would delete without deleting, add `DRY_RUN=1` to the CronJob's env first.

## Measured behaviour (dev-cluster spike)

- Cold start (Pod created to ready): about 27 s on Kata, about 14 s on runc.
- Real test suites run at about 1.5x their runc time.
- Exec latency p50 / p95: about 180 / 227 ms.
- Guest memory about 2 GiB (512 MiB Kata base + container limits); peak in-guest need 703 MiB.
- 3.5 h soak: clean, pod cgroup peak 1474 of 2560 MiB, with `O_DIRECT` enabled. Soak 1, without
  it, OOM-killed the VM because the host double-cached the Docker disk.
