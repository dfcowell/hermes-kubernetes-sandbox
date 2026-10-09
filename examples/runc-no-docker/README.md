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
`kubernetes-sandbox.yaml` into `$HERMES_HOME`, and set `terminal.backend: kubernetes`. This example
uses the namespace `agent-sandbox-runc` so it can coexist with the Kata example.

The plugin deletes its Pod when Hermes ends the session, but Pods of a crashed or stopped
gateway, and Pods past `max_lifetime_seconds`, are only reaped by the janitor (it is what
enforces the max-lifetime annotation). Run it here too: reuse `../kata/janitor.yaml` with
`NAMESPACE=agent-sandbox-runc`, plus a janitor ServiceAccount, Role and RoleBinding for that
namespace like the ones in `../kata/rbac.yaml`.
