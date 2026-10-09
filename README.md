# hermes-kubernetes-sandbox

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal backend plugin that runs
each session's commands in its own Kubernetes Pod — optionally with its own Docker daemon, so
agents can run container-based test suites — instead of on the gateway host.

**Status: alpha.** Tested end to end on Kata Containers 3.28 (Cloud Hypervisor) / containerd
2.2.3 / Kubernetes 1.36.5, including a 3.5 h soak. The runc example is validated against the API
server but has not been live-tested.

## How it works

- A `TerminalEnvironmentProvider` plugin; select it with `terminal.backend: kubernetes`.
- One Pod per Hermes session: a `work` container running Hermes's sandbox image and, with
  `docker: true`, a `dind` sidecar from the official `docker:*-dind` image on a raw block volume
  for `/var/lib/docker`. The Pod is created on the session's first command, adopted by a
  restarted gateway, and recreated if it disappears — the result then carries
  `environment_recreated` so the model is told the workspace was reset.
- Commands run over the Kubernetes exec websocket through a fixed bash runner; Hermes's session
  snapshot, cwd tracking, timeouts and interrupts work as with the docker backend.
- The Pod is deleted when Hermes ends the environment (session close, idle reaper). A gateway
  that exits leaves its Pods for the next gateway to adopt; the janitor reaps what is left.
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
  policy, quota, RBAC, janitor and the node configuration the soak proved necessary.
- [`examples/runc-no-docker/`](examples/runc-no-docker/README.md) — the simplest deployment: an
  unprivileged Pod on the default runtime, no Docker, Pod Security `restricted`.

Run the janitor (`examples/kata/janitor.yaml`) with either: it reaps Pods left behind by a
crashed or stopped gateway, idle Pods, finished Pods and Pods past `max_lifetime_seconds` (the
janitor is what enforces the `hermes-sandbox/max-lifetime-seconds` annotation the plugin writes).

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
| `workspace_size` | `10Gi` | `sizeLimit` of the `/workspace` emptyDir. |
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
command (template-hash label), with the `environment_recreated` notice.

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

## Known limits

- **The exec's stdin is never closed.** On Kata 3.28 / containerd 2.2.3 / Kubernetes 1.36.5 an
  exec whose stdin has been closed loses all output written after the close (it reproduces with
  `kubectl exec -i`). So the runner receives the script and the caller's stdin length-prefixed on
  the exec's stdin, spools the stdin to a private file in the Pod's `/tmp` before starting the
  command, and runs the command from that file: the command still sees EOF, and a command that
  never reads its stdin cannot stall the upload.
- The workspace is an emptyDir: it lives and dies with the Pod. When Hermes ends the environment
  (session close, idle reaper, or eviction after an infrastructure error) the Pod is deleted, and
  the next command starts in a fresh Pod with the `environment_recreated` notice.
- Scripts are limited to 128 KiB (same as the docker backend).
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
