import json
import queue
import threading

import pytest
from kubernetes_sandbox.exec_stream import (CLOSE, ERROR, STDERR, STDIN, STDOUT, ExecDisconnected, ExecFailed,
                                            StreamingExecHandle, encode_frame, parse_status)


class FakeConn:
    """Scripted server: yields `frames` (bytes) then optionally raises."""

    def __init__(self, frames, end_with=None):
        self.sent, self._q, self.closed = [], queue.Queue(), False
        for f in frames:
            self._q.put(f)
        self._end = end_with

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout=None):
        try:
            return self._q.get(timeout=0.05)
        except queue.Empty:
            raise self._end or TimeoutError()

    def close(self):
        self.closed = True


def _status(code):
    if code == 0:
        return encode_frame(ERROR, json.dumps({"status": "Success"}).encode())
    return encode_frame(ERROR, json.dumps({"status": "Failure", "reason": "NonZeroExitCode", "details": {
        "causes": [{"reason": "ExitCode", "message": str(code)}]}}).encode())


def test_frame_codec():
    assert encode_frame(STDIN, b"hi") == b"\x00hi"


def test_parse_status():
    assert parse_status(json.dumps({"status": "Success"}).encode()) == 0
    assert parse_status(_status(42)[1:]) == 42
    with pytest.raises(ExecFailed, match="container not found"):
        parse_status(json.dumps({"status": "Failure", "message": "container not found"}).encode())


def test_handle_streams_output_and_never_closes_stdin():
    conn = FakeConn([encode_frame(STDOUT, b"one\n"), encode_frame(STDERR, b"err\n"),
                     encode_frame(STDOUT, b"two\n"), _status(3)], end_with=ConnectionError("closed"))
    h = StreamingExecHandle(lambda: conn, b"PAYLOAD", on_kill=lambda: None)
    assert h.wait(timeout=5) == 3
    assert h.stdout.read() == "one\nerr\ntwo\n"
    # Kata drops output written after the exec's stdin is closed: never send a CLOSE frame.
    assert conn.sent == [encode_frame(STDIN, b"PAYLOAD")]
    assert not any(f[:1] == bytes([CLOSE]) for f in conn.sent)
    assert h.error is None and conn.closed


def test_handle_raises_connection_error_without_status():
    conn = FakeConn([encode_frame(STDOUT, b"partial")], end_with=ConnectionError("reset"))
    h = StreamingExecHandle(lambda: conn, b"", on_kill=lambda: None)
    h.wait(timeout=5)
    assert isinstance(h.error, ExecDisconnected)
    assert h.returncode is not None and h.returncode != 0


def test_large_payload_is_chunked():
    conn = FakeConn([_status(0)], end_with=ConnectionError())
    StreamingExecHandle(lambda: conn, b"x" * 200_000, on_kill=lambda: None).wait(timeout=5)
    data = b"".join(f[1:] for f in conn.sent if f[:1] == bytes([STDIN]))
    assert data == b"x" * 200_000 and all(len(f) <= 64 * 1024 + 1 for f in conn.sent)
    assert all(f[:1] == bytes([STDIN]) for f in conn.sent)


def test_kill_calls_on_kill_and_closes():
    killed = threading.Event()
    conn = FakeConn([], end_with=None)  # never ends on its own
    h = StreamingExecHandle(lambda: conn, b"", on_kill=killed.set)
    h.kill()
    assert killed.wait(2)  # on_kill runs on its own thread
    assert h.wait(timeout=5) is not None and conn.closed


def test_kill_before_connect_sends_nothing():
    gate = threading.Event()
    conn = FakeConn([_status(0)], end_with=ConnectionError())

    def connect():
        gate.wait(5)
        return conn

    h = StreamingExecHandle(connect, b"PAYLOAD", on_kill=lambda: None)
    h.kill()
    gate.set()
    assert h.wait(timeout=5) == 137
    assert conn.sent == [] and conn.closed
    assert h.error is None  # a requested kill is not a disconnect


def test_kill_during_chatty_stream():
    class Chatty(FakeConn):
        def recv(self, timeout=None):
            threading.Event().wait(0.05)
            return encode_frame(STDOUT, b"x")

    conn = Chatty([])
    h = StreamingExecHandle(lambda: conn, b"", on_kill=lambda: None)
    threading.Event().wait(0.2)
    h.kill()
    assert h.wait(timeout=2) == 137 and conn.closed


def test_blocking_on_kill_does_not_block_kill():
    release, ran = threading.Event(), threading.Event()

    def slow():
        ran.set()
        release.wait(5)

    h = StreamingExecHandle(lambda: FakeConn([]), b"", on_kill=slow)
    import time
    t = time.monotonic()
    h.kill()
    assert time.monotonic() - t < 0.5
    assert ran.wait(2)
    release.set()
    assert h.wait(timeout=5) == 137


def test_kill_mid_send_stops_sending():
    started = threading.Event()

    class SlowSend(FakeConn):
        def send(self, data):
            started.set()
            threading.Event().wait(0.05)
            super().send(data)

    conn = SlowSend([], end_with=None)
    payload = b"x" * (64 * 1024 * 100)  # 100 chunks, ~5 s at 50 ms each
    h = StreamingExecHandle(lambda: conn, payload, on_kill=lambda: None)
    assert started.wait(2)
    h.kill()
    assert h.wait(timeout=2) == 137
    assert len(conn.sent) < 10 and conn.closed
    assert h.error is None


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'{"status": "Failure", "details": {"causes": '
                                 b'[{"reason": "ExitCode", "message": "x"}]}}', b"\xff\xfe"])
def test_parse_status_malformed_is_exec_failed(raw):
    with pytest.raises(ExecFailed):
        parse_status(raw)


def test_write_loops_on_partial_writes(monkeypatch):
    import os as _os
    from kubernetes_sandbox import exec_stream
    calls = []
    real = _os.write

    def short_write(fd, data):
        calls.append(len(data))
        return real(fd, bytes(data[:3]))

    conn = FakeConn([encode_frame(STDOUT, b"0123456789"), _status(0)], end_with=ConnectionError())
    monkeypatch.setattr(exec_stream.os, "write", short_write)
    h = StreamingExecHandle(lambda: conn, b"", on_kill=lambda: None)
    assert h.wait(timeout=5) == 0
    assert h.stdout.read() == "0123456789" and len(calls) == 4
