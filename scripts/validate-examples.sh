#!/usr/bin/env bash
# Validate every example manifest against a live API server.
#   (no flag)         read-only: server-side dry run of every manifest. Creates nothing: the
#                     namespaces must already exist. Safe against a real cluster.
#   --apply           THROWAWAY CLUSTERS ONLY (CI kind): applies the example manifests for real,
#                     then server-side dry-runs what the plugin itself renders from the sample
#                     settings -- the session's workspace PVC and its Pod, as one List (both
#                     docker_cli modes into the Kata namespace, docker: false into the runc
#                     namespace). Any admission rejection (PSA, policy, quota) fails the run.
#   --render-janitor  regenerates examples/kata/janitor.yaml's ConfigMap from janitor/janitor.py.
# PYTHON: an interpreter with PyYAML (CI: a uv venv); the plugin itself is imported from the repo.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
render_janitor() {
  "$PYTHON" - <<'PY'
import pathlib, yaml
yaml.SafeDumper.add_representer(
    str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))
src = pathlib.Path("janitor/janitor.py").read_text()
path = pathlib.Path("examples/kata/janitor.yaml")
# Rewrite only the ConfigMap document; every other document and comment stays byte-for-byte.
chunks = path.read_text().split("\n---\n")
for i, chunk in enumerate(chunks):
    lines = chunk.split("\n")
    n = 0
    while n < len(lines) and lines[n].startswith("#"):
        n += 1
    doc = yaml.safe_load("\n".join(lines[n:]))
    if doc and doc.get("kind") == "ConfigMap":
        doc["data"]["janitor.py"] = src
        chunks[i] = "\n".join(lines[:n] + [yaml.safe_dump(doc, sort_keys=False).rstrip("\n")])
path.write_text("\n---\n".join(chunks))
PY
}
if [ "${1:-}" = "--render-janitor" ]; then render_janitor; exit 0; fi
before="$(mktemp)"
trap 'rm -f "$before"' EXIT
cp examples/kata/janitor.yaml "$before"
render_janitor
if ! diff -q "$before" examples/kata/janitor.yaml >/dev/null; then
  cp "$before" examples/kata/janitor.yaml   # a check must not edit the tree
  echo "janitor ConfigMap drifted from janitor/janitor.py: run $0 --render-janitor"
  exit 1
fi
rc=0
mode="${1:-}"
if [ -n "$mode" ] && [ "$mode" != "--apply" ]; then echo "usage: $0 [--apply|--render-janitor]"; exit 2; fi
echo "kube context: $(kubectl config current-context)"

if [ "$mode" = "--apply" ]; then
  # namespace first; the VAP before any Pod is created; hermes namespace for the janitor/RBAC objects.
  kubectl get ns hermes >/dev/null 2>&1 || kubectl create ns hermes
  kubectl apply -f examples/kata/runtimeclass.yaml -f examples/kata/namespace.yaml \
    -f examples/kata/admission-policy.yaml
  for f in examples/kata/*.yaml; do
    case "$f" in */kubernetes-sandbox.yaml|*/runtimeclass.yaml|*/namespace.yaml|*/admission-policy.yaml) continue ;; esac
    kubectl apply -f "$f" >/dev/null && echo "applied $f" || { echo "FAIL $f"; rc=1; }
  done
  kubectl apply -f examples/runc-no-docker/namespace.yaml
  for f in examples/runc-no-docker/*.yaml; do
    case "$f" in */kubernetes-sandbox.yaml|*/namespace.yaml) continue ;; esac
    kubectl apply -f "$f" >/dev/null && echo "applied $f" || { echo "FAIL $f"; rc=1; }
  done
  # Positive controls instead of a sleep: the VAP binding and the quota take effect
  # asynchronously, and a dry run before then would pass for the wrong reason.
  wait_for() {  # wait_for <what> <command...>: retry for up to 60 s
    local what="$1"; shift
    for _ in $(seq 60); do "$@" && { echo "ok   $what"; return 0; }; sleep 1; done
    echo "FAIL $what (not in effect after 60 s)"; return 1
  }
  vap_rejects() {  # rejected BY THE POLICY (quota or PSA rejections do not count)
    # Capture first: the rejection makes kubectl exit non-zero, which under pipefail would fail a
    # `kubectl … | grep -q` pipeline even when grep matches.
    local out
    out=$(kubectl -n agent-sandbox run vap-probe --image=busybox --restart=Never --dry-run=server \
        --overrides='{"spec":{"automountServiceAccountToken":false}}' 2>&1) || true
    grep -q "must set runtimeClassName kata" <<<"$out"
  }
  quota_ready() { [ -n "$(kubectl -n "$1" get resourcequota -o jsonpath='{.items[*].status.hard}')" ]; }
  wait_for "admission policy rejects a Pod without runtimeClassName" vap_rejects || exit 1
  wait_for "ResourceQuota agent-sandbox enforced" quota_ready agent-sandbox || exit 1
  wait_for "ResourceQuota agent-sandbox-runc enforced" quota_ready agent-sandbox-runc || exit 1
  # Render real Pods with the plugin from the sample settings and dry-run them.
  render() {  # render <example dir> <docker_cli>
    PYTHONPATH=. "$PYTHON" - "$1" "$2" <<'PY'
import dataclasses, json, pathlib, sys, tempfile
from kubernetes_sandbox.naming import Identity
from kubernetes_sandbox.podspec import render_pod, render_workspace_claim
from kubernetes_sandbox.settings import load_settings
example, cli = sys.argv[1:3]
with tempfile.TemporaryDirectory() as home:
    pathlib.Path(home, "kubernetes-sandbox.yaml").write_text(
        pathlib.Path(example, "kubernetes-sandbox.yaml").read_text().replace("<your RWO block StorageClass>", "standard"))
    s = load_settings(pathlib.Path(home))
if s.docker:
    s = dataclasses.replace(s, docker_cli=cli)
ident = Identity("ci", "x")
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
    render_workspace_claim(s, ident),
    render_pod(s, ident, now_iso="2026-01-01T00:00:00Z", created_by="ci")]}))
PY
  }
  for spec in "examples/kata image-volume" "examples/kata init-copy" "examples/runc-no-docker -"; do
    set -- $spec
    render "$1" "$2" | kubectl create --dry-run=server -f - >/dev/null \
      && echo "ok   plugin-rendered PVC + Pod ($1, docker_cli=$2)" \
      || { echo "FAIL plugin-rendered PVC + Pod ($1, docker_cli=$2)"; rc=1; }
  done
  exit "$rc"
fi

# Read-only default. Namespaced objects need their namespace to exist for a server-side dry run.
for ns in hermes agent-sandbox agent-sandbox-runc; do
  kubectl get ns "$ns" >/dev/null 2>&1 || {
    echo "missing namespace $ns (create it, or use --apply on a throwaway cluster)"; rc=1; }
done
[ "$rc" = 0 ] || exit "$rc"
for dir in examples/kata examples/runc-no-docker; do
  for f in "$dir"/*.yaml; do
    case "$f" in */kubernetes-sandbox.yaml) continue ;; esac
    kubectl apply --dry-run=server -f "$f" >/dev/null || { echo "FAIL $f"; rc=1; continue; }
    echo "ok   $f"
  done
done
exit "$rc"
