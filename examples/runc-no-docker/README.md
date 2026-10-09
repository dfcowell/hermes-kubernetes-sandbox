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

Mount the `hermes-sandbox-driver-token` Secret into the gateway container as described in the
[Kata README](../kata/README.md#give-the-gateway-the-driver-credentials), copy
`kubernetes-sandbox.yaml` into `$HERMES_HOME`, and set `terminal.backend: kubernetes`.

There is no janitor here: the plugin deletes its Pod when a session ends and Pods carry a
`max_lifetime_seconds` annotation, but if you want the safety net for crashed gateways, reuse
`../kata/janitor.yaml` (it needs a janitor ServiceAccount and Role like the ones in `../kata/rbac.yaml`).
