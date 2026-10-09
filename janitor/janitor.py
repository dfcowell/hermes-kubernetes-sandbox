#!/usr/bin/env python3
"""Reap Hermes sandbox Pods: idle too long, past max lifetime, or finished. Stdlib only."""
from __future__ import annotations

import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
SELECTOR = "app.kubernetes.io/managed-by=hermes-kubernetes-sandbox"


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str | None) -> float | None:
    if not s:
        return None
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def decide(pod: dict, now: float, idle_seconds: int) -> str | None:
    md, ann = pod.get("metadata") or {}, (pod.get("metadata") or {}).get("annotations") or {}
    if (pod.get("status") or {}).get("phase") in ("Failed", "Succeeded"):
        return "finished"
    created = parse(md.get("creationTimestamp")) or now
    max_life = int(ann.get("hermes-sandbox/max-lifetime-seconds", "86400"))
    if now - created > max_life:
        return "max-lifetime"
    last = parse(ann.get("hermes-sandbox/last-used")) or created
    if now - last > idle_seconds:
        return "idle"
    return None


def _api(method: str, path: str, ctx, token: str, body: dict | None = None) -> dict:
    url = "https://kubernetes.default.svc" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    ns = os.environ.get("NAMESPACE", "agent-sandbox")
    idle = int(os.environ.get("IDLE_SECONDS", "3600"))
    dry = os.environ.get("DRY_RUN") == "1"
    token = open(f"{SA}/token").read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    q = urllib.parse.urlencode({"labelSelector": SELECTOR})
    pods = _api("GET", f"/api/v1/namespaces/{ns}/pods?{q}", ctx, token).get("items", [])
    now = time.time()
    for p in pods:
        name, reason = p["metadata"]["name"], decide(p, now, idle)
        if reason is None:
            print(f"keep {name}")
            continue
        print(f"{'would delete' if dry else 'delete'} {name}: {reason}")
        if not dry:
            _api("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}", ctx, token, {"gracePeriodSeconds": 10})
    print(f"checked {len(pods)} sandbox pods")
    return 0


if __name__ == "__main__":
    sys.exit(main())
