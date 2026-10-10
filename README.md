# hermes-kubernetes-sandbox

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal backend plugin that runs
each session's commands in its own Kubernetes Pod — optionally with its own Docker daemon, so
agents can run container-based test suites — instead of on the gateway host.

**Status: alpha.** Tested end to end on Kata Containers 3.28 (Cloud Hypervisor) / containerd
2.2.3 / Kubernetes 1.36.5, including a 3.5 h load test. The runc example is validated against the API
server but has not been live-tested.

## How it works

- A `TerminalEnvironmentProvider` plugin; select it with `terminal.backend: kubernetes`.
- One Pod per Hermes session: a `work` container running Hermes's sandbox image and, with
  `docker: true`, a `dind` sidecar from the official `docker:*-dind` image on a raw block volume
  for `/var/lib/docker`. The Pod is created on the session's first command and recreated if it
  disappears — the result then carries `environment_recreated` so the model is told that running
  processes and Docker state are gone.
- One **workspace volume** (PVC) per session, mounted at `/workspace`. It outlives the session's
  Pods: every new Pod for the session mounts the same volume, so files under `/workspace` pick up
  where the last Pod left off (see [Workspace persistence](#workspace-persistence)).
- Commands run over the Kubernetes exec websocket through a fixed bash runner; Hermes's session
  snapshot, cwd tracking, timeouts and interrupts work as with the docker backend.
- Pods come and go: the plugin deletes the session's Pod whenever Hermes ends the environment —
  session close, Hermes's idle reaper, a graceful gateway stop or restart, eviction after an
  infrastructure error — and the janitor reaps idle and expired ones. Only a gateway that dies
  without shutting down (crash, SIGKILL) leaves its Pods running, for the next gateway to adopt or
  the janitor to reap. The workspace volume survives all of these.
- The plugin refuses to run a privileged Docker daemon without a VM-isolated RuntimeClass unless
  explicitly told to (`allow_privileged_host_runtime`).

## Install

The plugin uses only libraries Hermes already ships (httpx, websockets, PyYAML).

```sh
git clone https://github.com/dfcowell/hermes-kubernetes-sandbox "$HERMES_HOME/plugins/kubernetes-sandbox"
hermes plugins enable kubernetes-sandbox
# write $HERMES_HOME/kubernetes-sandbox.yaml (or set HERMES_K8S_* variables), then:
hermes config set terminal.backend kubernetes
```

Then set up the cluster side from one of the examples:

- [`examples/kata/`](examples/kata/README.md) — **recommended**: each sandbox is a Kata
  Containers micro-VM with Docker-in-Docker. RuntimeClass sizing, admission policy, network
  policy, quota, RBAC, janitor and the node configuration that load testing showed to be necessary.
- [`examples/runc-no-docker/`](examples/runc-no-docker/README.md) — the simplest deployment: an
  unprivileged Pod on the default runtime, no Docker, Pod Security `restricted`.

**The janitor (`examples/kata/janitor.yaml`) is required** with either example: it is the only
thing that deletes workspace volumes (see below). It also reaps idle Pods, finished Pods, Pods
left behind by a crashed gateway and Pods past `max_lifetime_seconds` (the janitor is what
enforces the `hermes-sandbox/max-lifetime-seconds` annotation the plugin writes).

## Workspace persistence

Each session's `/workspace` is a PersistentVolumeClaim named `<pod name>-ws`, created by the plugin
before the session's first Pod and labelled like it (`app.kubernetes.io/component: workspace`).
Deleting the Pod — `cleanup()`, a gateway roll, eviction, the janitor's idle reap, a node drain —
never deletes the volume, and the session's next Pod mounts it again.

| Survives a new Pod | Does not survive |
|---|---|
| Files under `/workspace`, including the cwd | Running and background processes |
| | Docker images, containers, volumes and build cache (the Docker disk is per Pod) |
| | Anything outside `/workspace` (`/tmp`, `$HOME`, installed packages) |

The result of the first command in a new Pod carries `environment_recreated`, as before: the
workspace is intact but processes and Docker state are not.

**Retention.** The plugin stamps `workspace_retention` (default `7d`) on each PVC as
`hermes-sandbox/workspace-retention-seconds`. The janitor, every run, stamps a PVC that no Pod
mounts with `hermes-sandbox/unused-since` (its own clock, accurate to one run) and deletes it once
`unused-since` is older than the retention. The stamp is cleared by the plugin every time it starts
a Pod on the volume (before the Pod is created, so even a session shorter than the janitor's
interval restarts the clock) and by the janitor whenever it sees the volume mounted. The janitor
deletes with `uid` + `resourceVersion` preconditions from its listing, so a PVC the plugin touched
after that listing is skipped (409) and re-evaluated next run; if the delete wins instead, the
plugin sees the PVC terminating, waits it out and creates a fresh, empty workspace. Malformed or
future `unused-since` stamps restart the clock; they never delete a volume. A session that returns
after its workspace was deleted gets a fresh, empty one. The plugin never deletes a PVC: its RBAC
has `create`/`get`/`list`/`patch` on them, no `delete`.

**Storage requirements.** A filesystem StorageClass (`workspace_storage_class`, unset = the
cluster default) that supports the `workspace_access_mode`:

- `ReadWriteOncePod` (default) is the guarantee that exactly one Pod uses the workspace at a time:
  the scheduler keeps a second Pod Pending until the first is gone, and the kubelet enforces it.
  It needs Kubernetes >= 1.29 and a CSI driver that supports `SINGLE_NODE_SINGLE_WRITER`, with
  csi-provisioner >= v3.0 and csi-attacher >= v4.0 (verified on democratic-csi iSCSI with
  provisioner v3.6.0 / attacher v4.4.0).
- `ReadWriteOnce` is the fallback for non-CSI or older drivers. It is per **node**: two Pods on the
  same node can both mount and write the volume, and only the plugin's graceful
  replace-then-wait (below) prevents that.

Symptom of a driver without RWOP support: the workspace PVC stays `Pending` with a provisioning or
attach error naming the access mode, and commands fail with "not ready" errors. Set
`workspace_access_mode: ReadWriteOnce` (new workspaces only: access modes are immutable, so an
existing PVC is adopted as it is, with a warning in the gateway log; delete the stuck PVC to get
one with the new mode).

`workspace_size` is the PVC's request; changing it affects new workspaces only
(existing PVCs keep their size). Budget the namespace quota for idle workspaces as well as live
sessions (the example quotas show the arithmetic). Under Kata, the volume is mounted on the host
and shared into the VM over virtio-fs (see Known limits). Workspaces are not backed up by this
plugin; back up the StorageClass's volumes if you need that.

### Out-of-cluster gateways

Set `api_url` to the API server, `ca_path` to its CA bundle (or to an empty value to use the
system trust store), and write a bounded token to `token_path`:

```sh
kubectl -n hermes create token hermes-sandbox-driver --duration=24h > /path/to/token
```

The token file is re-read on every request, so renewing it needs no restart.

### Work image requirements

The `work` image must provide GNU bash (the runner uses `read -N` and arrays), GNU coreutils
(`head -c`, `wc`, `timeout`) and util-linux `setsid` (`--wait`). Hermes's default sandbox image does; busybox-only images do not.

## Operating: node drains and force-deletes

Sandbox Pods are bare Pods with `emptyDir` volumes, so `kubectl drain` refuses them by default
("cannot delete Pods not managed by ReplicationController, ReplicaSet, Job, DaemonSet or
StatefulSet", plus the `emptyDir` warning). Drain with:

```bash
kubectl drain <node> --ignore-daemonsets --force --delete-emptydir-data
```

`--force` only lifts drain's unmanaged-Pod check; the Pods are still evicted gracefully (their grace
period, 30 s by default). The `emptyDir` contents (Docker socket, Docker CLI copy) are disposable;
the workspace is on the PVC.

**What a session sees.** The command in flight is lost, and the model gets a retryable "Lost the
exec stream" error. The next command recreates the Pod on another node (the drained node is
cordoned), waits for the workspace volume to detach from the old node and attach to the new one
(normally seconds to a minute, within `ready_timeout_seconds`, default 180), then continues with
the files and cwd intact and the `environment_recreated` notice. Running processes, background jobs
and Docker images and containers are lost, as with any Pod deletion. If the volume move outlasts the
ready timeout, the command returns a retryable "not ready" error and the next command waits again.

**Force-deleting.** `kubectl delete pod --force --grace-period=0` removes the Pod object
immediately while the old sandbox (the Kata VM) is still being torn down. On a cordoned node that
is safe: the replacement lands on another node, and the attach-detach controller will not attach the
`ReadWriteOnce`/`ReadWriteOncePod` volume there until the old node has unmounted it (a `Multi-Attach`
warning and `ContainerCreating` in the meantime). Files written by the killed command are
crash-consistent.

**Never force-delete a sandbox Pod on a node that is not cordoned.** The replacement can schedule
onto the same node while the old sandbox still has the workspace mounted: `ReadWriteOncePod`'s
scheduler check sees only Pod objects, and the volume is already attached to that node, so two
sandboxes could briefly write the same workspace (whether the CSI driver refuses the second mount is
driver-specific). Prefer a graceful `kubectl delete pod` (the plugin itself always deletes with a
10 s grace and waits for the old Pod to be gone), and cordon first if you must force.

A node that is lost rather than drained holds the volume until the attach-detach controller
force-detaches it (about 6 minutes); see Known limits.

## Settings

Read from `$HERMES_HOME/kubernetes-sandbox.yaml` (optional); every key can be set or overridden by
the environment variable `HERMES_K8S_<KEY in upper case>`, e.g. `HERMES_K8S_MAX_PER_PROFILE=3`.
Unknown keys are an error. Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`; an empty value
for a nullable key means "unset".

| Key | Default | Meaning |
|---|---|---|
| `namespace` | `agent-sandbox` | Namespace the sandbox Pods live in. |
| `runtime_class_name` | unset | RuntimeClass for sandbox Pods (e.g. `kata`). |
| `docker` | `true` | Run a privileged Docker-in-Docker sidecar (see the interlock). |
| `allow_privileged_host_runtime` | `false` | Allow `docker: true` WITHOUT `runtime_class_name` — the daemon is then root on the node. |
| `work_image` | Hermes's default sandbox image | Image of the `work` container (see requirements above). |
| `dind_image` | `docker:29.8.1-dind` | Docker daemon image. |
| `docker_cli` | `image-volume` | How the Docker CLI reaches `work`: `image-volume` (needs the ImageVolume feature) or `init-copy`. |
| `cli_image` | `docker:29.8.1-cli` | Source of the Docker CLI. |
| `formatter_image` | `debian:bookworm-slim` | Init container that formats the Docker block volume. |
| `docker_disk_storage_class` | unset (required with `docker: true`) | StorageClass providing `volumeMode: Block` volumes. |
| `docker_disk_size` | `30Gi` | Size of the per-Pod Docker disk. |
| `workspace_size` | `10Gi` | Storage request of the session's `/workspace` PVC (new workspaces only). |
| `workspace_storage_class` | unset (cluster default) | StorageClass of the workspace PVC: a filesystem class supporting `workspace_access_mode`. Env: `HERMES_K8S_WORKSPACE_STORAGE_CLASS`. |
| `workspace_access_mode` | `ReadWriteOncePod` | Access mode of new workspace PVCs: `ReadWriteOncePod` (one Pod at a time; needs CSI `SINGLE_NODE_SINGLE_WRITER`) or `ReadWriteOnce` (one node; fallback for older drivers). See Storage requirements. Env: `HERMES_K8S_WORKSPACE_ACCESS_MODE`. |
| `workspace_retention` | `7d` | How long a workspace may sit unmounted before the janitor deletes it: seconds, or `<n>s`/`m`/`h`/`d` (`36h`, `7d`); must be positive. Written to each new PVC. Env: `HERMES_K8S_WORKSPACE_RETENTION`. |
| `work_cpu` / `work_memory` | `100m` / `512Mi` | `work` CPU request; memory request and limit. |
| `dind_cpu_request` / `dind_cpu_limit` / `dind_memory` | `500m` / `2` / `1Gi` | `dind` resources. |
| `profile_configmap` | `hermes-sandbox-profile` | ConfigMap mounted into `/etc/profile.d` (puts the Docker CLI on PATH). |
| `max_per_profile` | `2` | Live sandboxes per Hermes profile; beyond it a command fails with a retryable error. |
| `ready_timeout_seconds` | `180` | How long to wait for a new Pod to become Ready. |
| `max_lifetime_seconds` | `86400` | Written to the Pod as an annotation; the janitor enforces it. |
| `api_url` | `https://kubernetes.default.svc` | API server URL. |
| `token_path` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | Bearer token file (re-read per request). |
| `ca_path` | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | CA bundle; empty = system trust. A configured path that does not exist fails `check_requirements`. |
| `extra_pod_annotations` | `{}` | Extra Pod annotations, string to string. From the environment: JSON or YAML text. |

Changing a setting that affects the Pod template replaces existing sandbox Pods on their next
command (template-hash label), with the `environment_recreated` notice; the workspace volume is
kept.

## Development and validation

    uv venv -p 3.13 .venv
    git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git .hermes-src
    git -C .hermes-src checkout --quiet 524041b9d0437cd4424a524af4b9e26145e69ab0
    uv pip install --python .venv -e .hermes-src pytest==9.1.1 ruff==0.15.10
    .venv/bin/ruff check . && .venv/bin/pytest

- `scripts/validate-examples.sh` — server-side dry run of every example manifest against the
  current kube context. Read-only: it prints the context first and fails if a namespace is
  missing. `--apply` is for throwaway clusters only (CI kind): it applies the examples and
  dry-runs the Pods the plugin renders. `--render-janitor` regenerates the janitor ConfigMap
  from `janitor/janitor.py`.
- `scripts/e2e.py <settings.yaml>` — live end-to-end check against a real cluster (never run in
  CI). From the repo root: `.venv/bin/python scripts/e2e.py my-settings.yaml`.

## Why no Kubernetes client library?

`kubernetes_sandbox/kube.py` is a small REST client and `exec_stream.py` a websocket exec client,
rather than a dependency on the official `kubernetes` package or another client library:

- **No new dependencies.** A Hermes plugin is installed as a directory, with no dependency
  installation step. The plugin uses only `httpx`, `websockets` and PyYAML, which Hermes already
  ships as pinned core dependencies. A client library would have to become a Hermes core
  dependency, or be fetched from PyPI at runtime, unpinned, on a security-sensitive path.
- **The API surface is small.** Get, list, create, patch and delete for Pods and PVCs (PVC
  deletes carry uid/resourceVersion preconditions), plus exec. The token file is re-read on every request, so a
  rotating projected ServiceAccount token works without a refresh mechanism.
- **Exec needs exact control of the stream.** The script and its stdin travel in-band, the status
  frame carries the exit code, and stdin is deliberately never closed (see Known limits). That is
  simpler to guarantee on a raw `v5.channel.k8s.io` websocket than through a library's stream
  wrapper.

The trade-off: authentication is a bearer token plus a CA bundle only. Kubeconfig exec plugins,
OIDC refresh and client certificates are not supported; out-of-cluster gateways use a minted
token (see "Out-of-cluster gateways"). The client is one module behind the interface the tests
fake, so replacing it with a library later stays contained.

## Known limits

- **The exec's stdin is never closed.** On Kata 3.28 / containerd 2.2.3 / Kubernetes 1.36.5 an
  exec whose stdin has been closed loses all output written after the close (it reproduces with
  `kubectl exec -i`). So the runner receives the script and the caller's stdin length-prefixed on
  the exec's stdin, spools the stdin to a private file in the Pod's `/tmp` before starting the
  command, and runs the command from that file: the command still sees EOF, and a command that
  never reads its stdin cannot stall the upload.
- Only `/workspace` persists across Pods; processes, Docker state and the rest of the filesystem
  do not (see Workspace persistence).
- Under Kata the workspace PVC is a host filesystem shared into the VM over virtio-fs, so its writes
  go through the host page cache. The live gate measures its memory cost under heavy writes; the
  memory figures in the Kata example were measured without it.
- A workspace PVC with a matching name but foreign labels or session key is never mounted: the
  command fails with a non-retryable error until an operator removes that PVC.
- An expired workspace races a returning session only inside the janitor's list-to-delete
  window: the plugin clears the idle stamp before it creates the Pod, which changes the PVC's
  resourceVersion and fails the janitor's DELETE precondition. If the DELETE lands first, the plugin
  sees the PVC terminating and starts the session on a fresh, empty workspace (with the
  `environment_recreated` notice).
- Node drains need `--force --delete-emptydir-data`, and a force-delete is only safe on a cordoned
  node (see Operating: node drains and force-deletes).
- A Pod on a lost node holds its workspace volume until the attach-detach controller
  force-detaches the volume (about 6 minutes), on top of the Pod being stuck Terminating; until then
  the session's new Pod waits (`Multi-Attach`) and commands fail with retryable errors.
- Replacing a Pod is always a graceful delete (10 s grace) and waits up to 90 s for the old Pod to
  be gone before creating the new one (a reused name cannot be created earlier anyway); if the old
  one is still terminating the command fails with a retryable error. With `ReadWriteOncePod` the
  volume itself refuses a second Pod; with `ReadWriteOnce` this wait is the only guard against two
  Pods on one node sharing the workspace.
- The plugin re-issues that 10 s graceful delete even on a Pod a node drain or eviction is already
  deleting, which shortens their 30 s grace to 10 s. Harmless for the volume: the Pod object is only
  removed after its volumes are unmounted.
- Scripts are limited to 128 KiB (same as the docker backend).
- The runner's `exec_id` (used to find and kill a command) is not validated in the Pod: it is an
  internal uuid, never user input.
- The Pod's PID 1 is `sleep`, which does not reap zombies.
- Processes that leave the command's process group (`setsid`, double-forking daemons) escape a
  kill; the Pod is the boundary.
- A command killed by a signal can make bash print a job notice.
- `timeout=0` gives the remote command a 5 s backstop.
- Stdin containing lone surrogates raises (Hermes-wide convention).
- `cleanup()` runs without the env lock; two concurrent first commands can both run the session
  bootstrap (harmless).
- The RBAC grants `watch` and `pods/log`, reserved for later use.
- `docker_cli: init-copy` copies `cli-plugins/docker-*`; an image with an empty plugins
  directory fails the glob.
- Creating a Pod can wait up to `ready_timeout_seconds` on the caller's thread and is not
  interruptible by `/stop` (same as docker's container creation).
- The kata-deploy section of the node configuration is written from documentation, not tested.
- CI actions are pinned by tag, not by SHA.
- runc is validated against the API server only, not live-tested.

## License

MIT
