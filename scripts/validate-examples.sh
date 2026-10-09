#!/usr/bin/env bash
# Validate every example manifest against a live API server (server-side dry run).
# CI runs it against kind; locally pass KUBECONFIG. --render-janitor regenerates
# examples/kata/janitor.yaml's ConfigMap from janitor/janitor.py.
set -euo pipefail
cd "$(dirname "$0")/.."
render_janitor() {
  python3 - <<'PY'
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
diff -q "$before" examples/kata/janitor.yaml || { echo "janitor ConfigMap drifted: run --render-janitor"; exit 1; }
# Namespaced objects need their namespace to exist for a server-side dry run.
for ns in hermes agent-sandbox; do kubectl get ns "$ns" >/dev/null 2>&1 || kubectl create ns "$ns"; done
for dir in examples/kata examples/runc-no-docker; do
  for f in "$dir"/*.yaml; do
    case "$f" in */kubernetes-sandbox.yaml) continue ;; esac
    kubectl apply --dry-run=server -f "$f" >/dev/null && echo "ok   $f"
  done
done
