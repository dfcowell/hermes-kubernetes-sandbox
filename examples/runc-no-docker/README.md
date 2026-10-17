# runc, no Docker

The simplest deployment: each session is an **unprivileged Pod** on the normal runtime (runc)
with a single `work` container. There is no Docker daemon, no privileged container, no
RuntimeClass, no Docker disk and no profile ConfigMap. Agents get an isolated shell and
workspace but cannot run `docker`. If they need Docker, use the [Kata example](../kata/README.md).

Because nothing is privileged, the namespace enforces Pod Security `restricted` and no
admission policy is required. Isolation from the host is the ordinary container boundary, so
this is weaker than the Kata VM boundary; the NetworkPolicy and ResourceQuota still apply.

## Apply

```sh
kubectl apply -f namespace.yaml
kubectl apply -f networkpolicy.yaml
kubectl apply -f resourcequota.yaml
kubectl apply -f rbac.yaml     # change `hermes` to your Hermes namespace
```

Run the Hermes Pod as `hermes-sandbox-driver` with a projected token, as described in the
[Kata README](../kata/README.md#give-the-gateway-the-driver-credentials), copy
`kubernetes-sandbox.yaml` into `$HERMES_HOME`, and set `terminal.backend: kubernetes` and
`terminal.cwd: /workspace`. This example uses the namespace `agent-sandbox-runc` so it can coexist with the Kata example.

Each session's `/workspace` is a PVC (on the cluster's default StorageClass unless
`workspace_storage_class` is set) that outlives the session's Pods: the plugin deletes a Pod when
Hermes ends the environment (session close, idle reaper, graceful gateway stop, eviction), and the
next command's Pod mounts the same volume.

**The janitor is required**: it is the only thing that deletes workspace volumes (once unmounted
for longer than `workspace_retention`, default 7 days), and it reaps idle Pods, Pods of a crashed
gateway and Pods past `max_lifetime_seconds`. `rbac.yaml` already includes its ServiceAccount, Role
and RoleBinding for this namespace; run a copy of `../kata/janitor.yaml` with a different
CronJob/ConfigMap name (if the Kata one also runs) and `NAMESPACE=agent-sandbox-runc`.
