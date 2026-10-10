"""Kubernetes exec over websocket (v5.channel.k8s.io) behind Hermes's ProcessHandle protocol.

The exec's stdin is never closed (no CLOSE frame on channel 255): on Kata 3.28 / containerd 2.2.3
/ Kubernetes 1.36.5 an exec whose stdin has been closed loses every byte of output written after
the close (measured with `echo one; sleep 0.3; echo two` x5: 0/5 got "two" with stdin closed, 5/5
with it left open; the server sends "one", then the channel-3 status, then hangs up). The runner
(runner.py) delimits the caller's stdin in-band instead; the connection is closed only after the
status frame arrives.

The whole payload is sent before any output is read. That cannot deadlock: the runner spools all
of the caller's stdin to a file before it starts the command, so nothing is written to stdout
until the upload is complete (websockets' sync client holds one mutex across send and recv, so a
concurrent sender thread would not help)."""
from __future__ import annotations

import json
import os
import threading
from typing import Callable, Protocol

STDIN, STDOUT, STDERR, ERROR, CLOSE = 0, 1, 2, 3, 255
SUBPROTOCOL = "v5.channel.k8s.io"
_CHUNK = 64 * 1024


class Connection(Protocol):
    def send(self, data: bytes) -> None: ...
    def recv(self, timeout: float | None = None) -> bytes: ...
    def close(self) -> None: ...


class ExecFailed(RuntimeError):
    pass


class ExecDisconnected(RuntimeError):
    pass


def encode_frame(channel: int, data: bytes) -> bytes:
    return bytes([channel]) + data


def parse_status(payload: bytes) -> int:
    try:
        st = json.loads(payload.decode("utf-8", "replace") or "{}")
        if not isinstance(st, dict):
            raise ValueError("status is not an object")
        if st.get("status") == "Success":
            return 0
        for cause in (st.get("details") or {}).get("causes") or []:
            if cause.get("reason") == "ExitCode":
                return int(cause.get("message", "1"))
    except (ValueError, TypeError, AttributeError) as e:  # JSONDecodeError is a ValueError
        raise ExecFailed(f"malformed exec status: {e}") from None
    raise ExecFailed(st.get("message") or "exec failed")


class StreamingExecHandle:
    def __init__(self, connect: Callable[[], Connection], payload: bytes, on_kill: Callable[[], None]):
        self._on_kill = on_kill
        self._done = threading.Event()
        self._killed = threading.Event()
        self._returncode: int | None = None
        self.error: BaseException | None = None
        self._conn: Connection | None = None
        r, w = os.pipe()
        self._stdout = os.fdopen(r, "r", encoding="utf-8", errors="replace")
        self._w = w
        threading.Thread(target=self._run, args=(connect, payload), daemon=True).start()

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self):
        return self._returncode

    def _write(self, data: bytes) -> None:
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self._w, view):]
        except OSError:
            pass

    def _run(self, connect, payload: bytes) -> None:
        try:
            self._conn = conn = connect()
            if self._killed.is_set():
                self._returncode = 137
                return
            for i in range(0, len(payload), _CHUNK):
                if self._killed.is_set():  # stop uploading; the runner's tombstone check does the rest
                    self._returncode = 137
                    return
                conn.send(encode_frame(STDIN, payload[i:i + _CHUNK]))
            # Deliberately no close-stdin frame: see the module docstring.
            while True:
                if self._killed.is_set():
                    self._returncode = 137
                    return
                try:
                    frame = conn.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(frame, str):
                    frame = frame.encode()
                if not frame:
                    continue
                ch, data = frame[0], frame[1:]
                if ch in (STDOUT, STDERR):
                    self._write(data)
                elif ch == ERROR:
                    self._returncode = parse_status(data)
                    return
        except ExecFailed as e:
            self.error, self._returncode = e, 126
        except Exception as e:  # connection reset, handshake failure, closed without status
            self.error = ExecDisconnected(str(e) or type(e).__name__)
            self._returncode = 137 if self._killed.is_set() else 255
        finally:
            try:
                os.close(self._w)
            except OSError:
                pass
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._done.set()

    def poll(self):
        return self._returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout)
        return self._returncode

    def kill(self):
        self._killed.set()
        # on_kill may be a slow second exec; never block the caller. _run owns closing the connection.
        threading.Thread(target=self._safe_on_kill, daemon=True).start()

    def _safe_on_kill(self) -> None:
        try:
            self._on_kill()
        except Exception:
            pass


def connect_exec(client, pod: str, container: str, argv: list[str]) -> Connection:
    from websockets.sync.client import connect
    ctx = client.ssl_context()
    return connect(client.exec_url(pod, container, argv), subprotocols=[SUBPROTOCOL],
                   additional_headers=client.auth_headers(), ssl=ctx if not isinstance(ctx, bool) else None,
                   ping_interval=30, max_size=None, open_timeout=30)
