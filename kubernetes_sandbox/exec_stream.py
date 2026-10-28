"""Kubernetes exec over websocket (v5.channel.k8s.io) behind Hermes's ProcessHandle protocol.

The exec's stdin is never closed (no CLOSE frame on channel 255): on Kata 3.28 / containerd 2.2.3
/ Kubernetes 1.36.5 an exec whose stdin has been closed loses every byte of output written after
the close (measured with `echo one; sleep 0.3; echo two` x5: 0/5 got "two" with stdin closed, 5/5
with it left open; the server sends "one", then the channel-3 status, then hangs up). The runner
(runner.py) delimits the caller's stdin in-band instead; the connection is closed only after the
status frame arrives.

The whole payload is sent before any output is read. That cannot deadlock: the runner spools all
of the caller's stdin to a file before it starts the command, so nothing is written to stdout
until the upload is complete.

Every socket read and write happens on the one thread that runs the exec (SansIOConnection, driving
websockets' Sans-I/O protocol). websockets' sync client cannot be used here: it reads on a background
thread while the caller writes, and OpenSSL does not allow one SSL object to be used from two threads at
once. CPython does not serialise the two (gh-143756, open; its lock was reverted in 3.13.7 because it
deadlocked exactly this pattern), so the race corrupts OpenSSL's state: under TLS 1.3 the session tickets
the server sends right after the TLS handshake are read while the upgrade request is written, and the
Hermes gateway died with SIGSEGV in this path three times in a week (2026-10). A local reproduction
(scripts/exec-tls-stress.py) crashes the sync client in under a minute on Python 3.12, 3.13 and 3.14."""
from __future__ import annotations

import collections
import json
import os
import socket
import ssl
import threading
import time
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


class SansIOConnection:
    """A websocket client connection that does all its socket I/O on the calling thread: websockets'
    Sans-I/O ClientProtocol builds and parses the frames, this class moves the bytes. Not thread-safe by
    design: one thread owns it (StreamingExecHandle._run). Pings from the server are answered as frames are
    read; when nothing has arrived for `ping_interval` seconds a ping is sent from inside recv(), and if
    nothing at all arrives within `ping_timeout` after it, recv() raises ConnectionError (the sync client's
    keepalive, without its thread)."""

    def __init__(self, sock, protocol, *, ping_interval: float | None, ping_timeout: float, io_timeout: float,
                 clock=time.monotonic):
        self._sock, self._proto, self._clock = sock, protocol, clock
        self._ping_interval, self._ping_timeout, self._io_timeout = ping_interval, ping_timeout, io_timeout
        self._events: collections.deque = collections.deque()  # parsed, not yet returned
        self._fragments: list[bytes] = []
        self._last_rx = clock()
        self._ping_sent_at: float | None = None
        self.subprotocol: str | None = None

    @classmethod
    def open(cls, url: str, *, ssl_context: ssl.SSLContext | None, headers: dict, subprotocols: list[str],
             timeout: float, ping_interval: float | None = 30.0, ping_timeout: float = 20.0) -> "SansIOConnection":
        from websockets.client import ClientProtocol
        from websockets.http11 import Response
        from websockets.uri import parse_uri
        uri = parse_uri(url)
        deadline = time.monotonic() + timeout
        sock = socket.create_connection((uri.host, uri.port), timeout=timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
            if uri.secure:
                sock = (ssl_context or ssl.create_default_context()).wrap_socket(sock, server_hostname=uri.host)
            proto = ClientProtocol(uri, subprotocols=subprotocols, max_size=None)
            conn = cls(sock, proto, ping_interval=ping_interval, ping_timeout=ping_timeout, io_timeout=timeout)
            request = proto.connect()
            request.headers.update(headers)
            proto.send_request(request)
            conn._flush()
            while not (conn._events and isinstance(conn._events[0], Response)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for the websocket handshake response")
                if not conn._read(remaining):
                    break
            if proto.handshake_exc is not None:
                raise proto.handshake_exc
            if not (conn._events and isinstance(conn._events[0], Response)):
                raise ConnectionError("connection closed during the websocket handshake")
            response = conn._events.popleft()
            conn.subprotocol = response.headers.get("Sec-WebSocket-Protocol")
            return conn
        except BaseException:
            sock.close()
            raise

    @property
    def tls_version(self) -> str | None:
        return self._sock.version() if isinstance(self._sock, ssl.SSLSocket) else None

    def _flush(self) -> None:
        self._sock.settimeout(self._io_timeout)
        for chunk in self._proto.data_to_send():
            if chunk:
                self._sock.sendall(chunk)
            else:  # the protocol's end-of-stream marker, after a close handshake
                try:
                    self._sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    def _read(self, timeout: float) -> bool:
        """One socket read (up to `timeout`): feed the protocol, queue its events, send what it owes (pongs,
        the close reply). False at end of stream; socket.timeout (a TimeoutError) propagates."""
        self._sock.settimeout(max(timeout, 0.001))
        data = self._sock.recv(65536)
        if data:
            self._proto.receive_data(data)
            self._last_rx, self._ping_sent_at = self._clock(), None
        else:
            self._proto.receive_eof()
        self._events.extend(self._proto.events_received())
        self._flush()
        return bool(data)

    def _next_message(self) -> bytes | None:
        from websockets.frames import Close, Opcode
        while self._events:
            ev = self._events.popleft()
            op = getattr(ev, "opcode", None)
            if op in (Opcode.BINARY, Opcode.TEXT, Opcode.CONT):
                self._fragments.append(ev.data)
                if ev.fin:
                    data, self._fragments = b"".join(self._fragments), []
                    return data
            elif op is Opcode.CLOSE:
                close = Close.parse(ev.data) if ev.data else None
                raise ConnectionError(f"websocket closed by the server ({close})" if close else
                                      "websocket closed by the server")
        return None

    def _keepalive(self, now: float) -> float | None:
        """Send a ping when idle, fail when one went unanswered; returns the seconds until the next check."""
        if self._ping_interval is None:
            return None
        if self._ping_sent_at is not None:
            left = self._ping_sent_at + self._ping_timeout - now
            if left <= 0:
                raise ConnectionError(f"no data within {self._ping_timeout:g}s of a keepalive ping")
            return left
        left = self._last_rx + self._ping_interval - now
        if left <= 0:
            self._proto.send_ping(os.urandom(4))
            self._flush()
            self._ping_sent_at = now
            return self._ping_timeout
        return left

    def send(self, data: bytes) -> None:
        self._proto.send_binary(data)
        self._flush()

    def recv(self, timeout: float | None = None) -> bytes:
        from websockets.protocol import State
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            msg = self._next_message()
            if msg is not None:
                return msg
            if self._proto.state is State.CLOSED:
                raise ConnectionError("websocket connection closed")
            now = self._clock()
            waits = [w for w in (self._keepalive(now), None if deadline is None else deadline - now) if w is not None]
            wait = min(waits) if waits else None
            if deadline is not None and deadline - now <= 0:
                raise TimeoutError()
            try:
                if not self._read(wait if wait is not None else 3600.0):
                    if self._proto.state is not State.CLOSED and not self._events:
                        raise ConnectionError("websocket connection lost")
            except TimeoutError:
                continue

    def close(self) -> None:
        try:
            from websockets.protocol import State
            if self._proto.state is State.OPEN:
                self._proto.send_close()
                self._flush()
        except Exception:
            pass
        finally:
            self._sock.close()


def connect_exec(client, pod: str, container: str, argv: list[str]) -> Connection:
    ctx = client.ssl_context()
    return SansIOConnection.open(client.exec_url(pod, container, argv), subprotocols=[SUBPROTOCOL],
                                 headers=client.auth_headers(), ssl_context=ctx if not isinstance(ctx, bool) else None,
                                 timeout=30)
