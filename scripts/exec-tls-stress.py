#!/usr/bin/env python3
"""Stress the exec websocket client against a local TLS 1.3 server: many threads, each opening a fresh
connection per "command" the way the plugin does (one exec per command, ~1800/hour per running background
process under Hermes's 2 s poller).

    scripts/exec-tls-stress.py --client sansio   # the plugin's connect_exec: expect exit 0
    scripts/exec-tls-stress.py --client sync     # websockets' sync client: SIGSEGV (exit -11/139) in ~10-80 s

`sync` reproduces the Hermes gateway crash of 2026-10 (CPython gh-143756: the sync client reads on a
background thread while the caller writes the upgrade request, and the TLS 1.3 session tickets that arrive
right after the TLS handshake are processed concurrently with that write). Run it in the image that runs
Hermes to test the exact Python/OpenSSL pair. Needs the openssl CLI; makes no network calls beyond 127.0.0.1.
"""
from __future__ import annotations

import argparse
import faulthandler
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SERVER = r'''
import asyncio, ssl, sys
from websockets.asyncio.server import serve
async def handler(ws):
    try:
        await ws.recv()
        await ws.send(b"\x01" + b"x" * 512)
        await ws.send(b'\x03{"status":"Success"}')
        await ws.wait_closed()
    except Exception:
        pass
async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(sys.argv[1], sys.argv[2])
    async with serve(handler, "127.0.0.1", 0, ssl=ctx, subprotocols=["v5.channel.k8s.io"], max_size=None) as s:
        print(s.sockets[0].getsockname()[1], flush=True)
        await asyncio.Future()
asyncio.run(main())
'''


class StubClient:
    """The two KubeClient methods connect_exec uses."""

    def __init__(self, port: int, cafile: str):
        self._port, self._cafile = port, cafile

    def ssl_context(self):
        import ssl
        return ssl.create_default_context(cafile=self._cafile)

    def exec_url(self, pod, container, argv):
        return f"wss://localhost:{self._port}/api/v1/namespaces/ns/pods/{pod}/exec?container={container}&command=sh"

    def auth_headers(self):
        return {"Authorization": "Bearer stress"}


def one_sansio(client) -> None:
    from kubernetes_sandbox.exec_stream import connect_exec
    conn = connect_exec(client, "p", "work", ["sh"])
    try:
        conn.send(b"\x00" + b"y" * 2048)
        while conn.recv(timeout=5)[:1] != b"\x03":
            pass
    finally:
        conn.close()


def one_sync(client) -> None:
    from websockets.sync.client import connect
    conn = connect(client.exec_url("p", "work", ["sh"]), subprotocols=["v5.channel.k8s.io"],
                   additional_headers=client.auth_headers(), ssl=client.ssl_context(), ping_interval=30,
                   max_size=None, open_timeout=30)
    try:
        conn.send(b"\x00" + b"y" * 2048)
        while conn.recv(timeout=5)[:1] != b"\x03":
            pass
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", choices=["sansio", "sync"], default="sansio")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--seconds", type=float, default=120)
    args = ap.parse_args()
    faulthandler.enable()
    with tempfile.TemporaryDirectory() as d:
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
        server = subprocess.Popen([sys.executable, "-c", SERVER, cert, key], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        try:
            client = StubClient(int(server.stdout.readline()), cert)
            one = one_sansio if args.client == "sansio" else one_sync
            done, errors, lock, stop = [0], [0], threading.Lock(), time.monotonic() + args.seconds
            first_error: list[str] = []

            def worker():
                while time.monotonic() < stop:
                    try:
                        one(client)
                        with lock:
                            done[0] += 1
                    except Exception as e:
                        with lock:
                            errors[0] += 1
                            first_error[:] = first_error or [f"{type(e).__name__}: {e}"]

            t0 = time.monotonic()
            threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.threads)]
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                time.sleep(10)
                print(f"t={time.monotonic() - t0:.0f}s completed={done[0]} errors={errors[0]}", flush=True)
            print(f"client={args.client} completed={done[0]} errors={errors[0]} threads={args.threads} "
                  f"seconds={args.seconds:g}" + (f" first_error={first_error[0]}" if first_error else ""), flush=True)
            return 1 if errors[0] else 0
        finally:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
