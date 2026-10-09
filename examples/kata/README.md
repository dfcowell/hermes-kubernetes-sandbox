# Kata example (Docker-in-Docker, hardware-isolated)

Each session is a Pod running in its own Kata Containers micro-VM, with a Docker-in-Docker
sidecar. The privileged `dind` container is privileged **inside the VM**, so the host kernel is
not exposed. This is the recommended deployment when agents need `docker`.

## Prerequisites

- Kubernetes >= 1.30 (ValidatingAdmissionPolicy). Kubernetes >= 1.33 for image volumes (the
  default `docker_cli: image-volume`); on older clusters set `docker_cli: init-copy`.
- Kata Containers with **Cloud Hypervisor** on the sandbox nodes, configured as in
  [kata-node-config.md](kata-node-config.md) (not optional).
- A StorageClass that provisions **Block** volumes (`volumeMode: Block`, access mode `ReadWriteOncePod`); the Docker disk is a
  generic ephemeral volume that lives and dies with the Pod.
- A CNI that enforces NetworkPolicy. With Cilium, `socketLB.hostNamespaceOnly: true` is mandatory
  (see networkpolicy.yaml).

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

The plugin talks to the API server with the `hermes-sandbox-driver` ServiceAccount, not with the
gateway's own identity. Mount the token Secret from `rbac.yaml` through a projected volume on the
**gateway container only**, so the terminal children Hermes spawns elsewhere never see it:

```yaml
# in the Hermes Pod spec
volumes:
  - name: sandbox-driver
    secret:
      secretName: hermes-sandbox-driver-token
      defaultMode: 0440
      items:
        - {key: token, path: token}
        - {key: ca.crt, path: ca.crt}
containers:
  - name: gateway              # only this container
    volumeMounts:
      - {name: sandbox-driver, mountPath: /var/run/secrets/hermes-sandbox, readOnly: true}
```

This matches `token_path` / `ca_path` in `kubernetes-sandbox.yaml`. The Role grants rights in
`agent-sandbox` only: Pods, `pods/exec`, `pods/log`.

## First manual janitor run

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
