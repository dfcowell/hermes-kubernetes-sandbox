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
    created = parse(md.get("creationTimestamp"))
    if created is None:
        raise ValueError(f"unparseable creationTimestamp: {md.get('creationTimestamp')}")
    max_life_str = ann.get("hermes-sandbox/max-lifetime-seconds", "86400")
    try:
        max_life = int(max_life_str)
    except ValueError:
        max_life = 86400
    if now - created > max_life:
        return "max-lifetime"
    last_str = ann.get("hermes-sandbox/last-used")
    try:
        last = parse(last_str) if last_str else created
    except ValueError:
        last = created
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


def _load_api_context() -> tuple[str, object]:
    """Load service account token and SSL context. Factored for testability."""
    token = open(f"{SA}/token").read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    return token, ctx


def main() -> int:
    ns = os.environ.get("NAMESPACE", "agent-sandbox")
    idle = int(os.environ.get("IDLE_SECONDS", "3600"))
    dry = os.environ.get("DRY_RUN") == "1"
    token, ctx = _load_api_context()
    q = urllib.parse.urlencode({"labelSelector": SELECTOR})
    pods = _api("GET", f"/api/v1/namespaces/{ns}/pods?{q}", ctx, token).get("items", [])
    now = time.time()
    had_error = False
    for p in pods:
        try:
            name = p["metadata"]["name"]
            # Skip pods that are terminating
            if p.get("metadata", {}).get("deletionTimestamp"):
                print(f"skip {name}: terminating")
                continue
            reason = decide(p, now, idle)
            if reason is None:
                print(f"keep {name}")
                continue
            print(f"{'would delete' if dry else 'delete'} {name}: {reason}")
            if not dry:
                uid = p["metadata"]["uid"]
                try:
                    _api("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}", ctx, token,
                         {"gracePeriodSeconds": 10, "preconditions": {"uid": uid}})
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        print(f"gone {name}")
                    elif e.code == 409:
                        print(f"skip {name}: changed")
                    else:
                        raise
        except Exception as e:
            print(f"error {p.get('metadata', {}).get('name', 'unknown')}: {e}")
            had_error = True
    print(f"checked {len(pods)} sandbox pods")
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
