"""SansIOConnection against a real TLS 1.3 websocket server (websockets' own Sans-I/O ServerProtocol on a
thread). The property that matters is the first test: every socket read and write happens on the calling
thread. websockets' sync client reads on a background thread while the caller writes, and OpenSSL does not
allow one SSL object to be used from two threads at once (CPython gh-143756): on 2026-10-27 that crashed the
Hermes gateway with SIGSEGV three times in a week, always in this exec path."""
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest
from kubernetes_sandbox.exec_stream import SUBPROTOCOL, SansIOConnection
from websockets.frames import Frame, Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI for a test cert")


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_3
    server.load_cert_chain(cert, key)
    return server, str(cert)


class Peer:
    """The server side of one connection, driven synchronously by the test's script."""

    def __init__(self, sock):
        self.sock, self.proto, self._events = sock, ServerProtocol(subprotocols=[SUBPROTOCOL], max_size=None), []
        self.request: Request | None = None

    def _pump(self, timeout=5.0):
        self.sock.settimeout(timeout)
        data = self.sock.recv(65536)
        self.proto.receive_eof() if not data else self.proto.receive_data(data)
        self._events += self.proto.events_received()
        self.flush()
        return bool(data)

    def flush(self):
        for chunk in self.proto.data_to_send():
            if chunk:
                self.sock.sendall(chunk)

    def handshake(self, *, then: list[bytes] = ()):
        """Accept the upgrade; `then` frames go out in the SAME sendall as the 101 response."""
        while not self._events:
            self._pump()
        self.request = self._events.pop(0)
        self.proto.send_response(self.proto.accept(self.request))
        for f in then:
            self.proto.send_binary(f)
        self.sock.sendall(b"".join(self.proto.data_to_send()))

    def recv_frame(self, opcodes=(Opcode.BINARY,), timeout=5.0) -> Frame:
        deadline = time.monotonic() + timeout
        while True:
            for i, ev in enumerate(self._events):
                if isinstance(ev, Frame) and ev.opcode in opcodes:
                    return self._events.pop(i)
            if not self._pump(max(0.01, deadline - time.monotonic())):
                raise EOFError

    def send(self, data: bytes):
        self.proto.send_binary(data)
        self.flush()


@pytest.fixture
def server(tls):
    """serve(script) -> wss URL; `script(peer)` runs on the server thread for one connection."""
    ctx, _ = tls
    listener = socket.create_server(("127.0.0.1", 0))
    errors, threads = [], []

    def serve(script):
        def run():
            raw, _ = listener.accept()
            with ctx.wrap_socket(raw, server_side=True) as s:
                try:
                    script(Peer(s))
                except Exception as e:  # surfaced by the fixture teardown
                    errors.append(e)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
        return f"wss://localhost:{listener.getsockname()[1]}/api/v1/namespaces/ns/pods/p/exec?command=sh"

    yield serve
    for t in threads:
        t.join(timeout=5)
    listener.close()
    assert not errors, errors


def _open(url, tls, **kw):
    ctx = ssl.create_default_context(cafile=tls[1])
    return SansIOConnection.open(url, ssl_context=ctx, headers={"Authorization": "Bearer t0k"},
                                 subprotocols=[SUBPROTOCOL], timeout=5, **kw)


def test_all_io_stays_on_the_calling_thread(server, tls, monkeypatch):
    started = []
    real_start = threading.Thread.start

    def script(peer):
        peer.handshake()
        assert peer.recv_frame().data == b"\x00stdin"
        peer.send(b"\x01out")
        peer.recv_frame((Opcode.CLOSE,))

    url = server(script)  # the server thread starts before the spy is installed

    def spy(self):
        started.append(self.name)
        real_start(self)
    monkeypatch.setattr(threading.Thread, "start", spy)
    conn = _open(url, tls)
    conn.send(b"\x00stdin")
    assert conn.recv(timeout=5) == b"\x01out"
    conn.close()
    assert started == []


def test_handshake_negotiates_tls13_subprotocol_and_sends_headers(server, tls):
    seen = {}

    def script(peer):
        peer.handshake()
        seen["auth"] = peer.request.headers.get("Authorization")
        seen["path"] = peer.request.path
        peer.recv_frame((Opcode.CLOSE,))

    conn = _open(server(script), tls)
    assert conn.tls_version == "TLSv1.3"
    assert conn.subprotocol == SUBPROTOCOL
    conn.close()
    time.sleep(0.1)
    assert seen == {"auth": "Bearer t0k", "path": "/api/v1/namespaces/ns/pods/p/exec?command=sh"}


def test_frames_arriving_with_the_handshake_response_are_kept(server, tls):
    def script(peer):
        peer.handshake(then=[b"\x01early", b"\x03{}"])
        peer.recv_frame((Opcode.CLOSE,))

    conn = _open(server(script), tls)
    assert [conn.recv(timeout=5), conn.recv(timeout=5)] == [b"\x01early", b"\x03{}"]
    conn.close()


def test_recv_timeout_raises_timeouterror_and_the_connection_stays_usable(server, tls):
    go = threading.Event()

    def script(peer):
        peer.handshake()
        go.wait(5)
        peer.send(b"\x01late")
        peer.recv_frame((Opcode.CLOSE,))

    conn = _open(server(script), tls)
    with pytest.raises(TimeoutError):
        conn.recv(timeout=0.2)
    go.set()
    assert conn.recv(timeout=5) == b"\x01late"
    conn.close()


def test_server_ping_is_answered(server, tls):
    def script(peer):
        peer.handshake()
        peer.proto.send_ping(b"are-you-there")
        peer.flush()
        assert peer.recv_frame((Opcode.PONG,)).data == b"are-you-there"
        peer.send(b"\x01after-ping")
        peer.recv_frame((Opcode.CLOSE,))

    conn = _open(server(script), tls)
    assert conn.recv(timeout=5) == b"\x01after-ping"
    conn.close()


def test_fragmented_message_is_reassembled(server, tls):
    def script(peer):
        peer.handshake()
        peer.proto.send_binary(b"\x01ab", fin=False)
        peer.proto.send_continuation(b"cd", fin=True)
        peer.flush()
        peer.recv_frame((Opcode.CLOSE,))

    conn = _open(server(script), tls)
    assert conn.recv(timeout=5) == b"\x01abcd"
    conn.close()


def test_server_close_raises_connectionerror(server, tls):
    def script(peer):
        peer.handshake()
        peer.proto.send_close(1000, "bye")
        peer.flush()

    conn = _open(server(script), tls)
    with pytest.raises(ConnectionError):
        conn.recv(timeout=5)
    conn.close()


def test_server_hangup_without_close_raises_connectionerror(server, tls):
    conn = _open(server(lambda peer: peer.handshake()), tls)
    with pytest.raises(ConnectionError):
        conn.recv(timeout=5)
    conn.close()


def test_silent_peer_fails_the_keepalive(server, tls):
    stop = threading.Event()

    def script(peer):
        peer.handshake()
        stop.wait(5)  # never reads, so never answers the client's ping

    conn = _open(server(script), tls, ping_interval=0.2, ping_timeout=0.3)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="keepalive"):
        while True:
            try:
                conn.recv(timeout=0.1)  # how StreamingExecHandle._run polls
            except TimeoutError:
                assert time.monotonic() - t0 < 3
    stop.set()
    conn.close()


def test_rejected_upgrade_raises(tls):
    ctx, _ = tls
    listener = socket.create_server(("127.0.0.1", 0))

    def run():
        raw, _ = listener.accept()
        with ctx.wrap_socket(raw, server_side=True) as s:
            s.recv(65536)
            s.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 9\r\nConnection: close\r\n\r\nforbidden")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    with pytest.raises(Exception, match="403"):
        _open(f"wss://localhost:{listener.getsockname()[1]}/x", tls)
    t.join(5)
    listener.close()
