import json
import queue
import threading

import pytest
from kubernetes_sandbox.exec_stream import (CLOSE, ERROR, STDERR, STDIN, STDOUT, ExecDisconnected, ExecFailed,
                                            StreamingExecHandle, close_stdin_frame, encode_frame, parse_status)


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
    assert close_stdin_frame() == bytes([CLOSE, STDIN])


def test_parse_status():
    assert parse_status(json.dumps({"status": "Success"}).encode()) == 0
    assert parse_status(_status(42)[1:]) == 42
    with pytest.raises(ExecFailed, match="container not found"):
        parse_status(json.dumps({"status": "Failure", "message": "container not found"}).encode())


def test_handle_streams_output_and_sends_payload_then_close():
    conn = FakeConn([encode_frame(STDOUT, b"one\n"), encode_frame(STDERR, b"err\n"),
                     encode_frame(STDOUT, b"two\n"), _status(3)], end_with=ConnectionError("closed"))
    h = StreamingExecHandle(lambda: conn, b"PAYLOAD", on_kill=lambda: None)
    assert h.wait(timeout=5) == 3
    assert h.stdout.read() == "one\nerr\ntwo\n"
    assert conn.sent[0] == encode_frame(STDIN, b"PAYLOAD") and conn.sent[-1] == close_stdin_frame()
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


def test_kill_calls_on_kill_and_closes():
    killed = threading.Event()
    conn = FakeConn([], end_with=None)  # never ends on its own
    h = StreamingExecHandle(lambda: conn, b"", on_kill=killed.set)
    h.kill()
    assert killed.is_set()
    assert h.wait(timeout=5) is not None and conn.closed
