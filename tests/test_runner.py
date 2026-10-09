import os
import pathlib
import signal
import subprocess
import threading
import time
import uuid

import pytest

from kubernetes_sandbox.runner import exec_argv, kill_argv, stdin_payload


class _Result:
    def __init__(self, returncode, stdout, stderr, elapsed):
        self.returncode, self.stdout, self.stderr, self.elapsed = returncode, stdout, stderr, elapsed


def _run(script, stdin_data=None, *, login=False, timeout=30, wait=60):
    """Run the RUNNER like the websocket exec does: write the payload and leave stdin OPEN
    (the exec's stdin is never closed), wait for the runner to exit, then close stdin."""
    exec_id = uuid.uuid4().hex[:12]
    proc = subprocess.Popen(exec_argv(exec_id, login=login, timeout=timeout), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = [], []
    readers = [threading.Thread(target=lambda f=f, b=b: b.append(f.read()), daemon=True)
               for f, b in ((proc.stdout, out), (proc.stderr, err))]
    payload = stdin_payload(script, stdin_data)

    def write():
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except BrokenPipeError:
            pass  # the command never read its stdin and the runner already exited

    writer = threading.Thread(target=write, daemon=True)
    start = time.monotonic()
    for t in (*readers, writer):
        t.start()
    try:
        rc = proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(f"runner did not exit within {wait}s with stdin left open")
    finally:
        elapsed = time.monotonic() - start
        writer.join(5)
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        for t in readers:
            t.join(5)
    return _Result(rc, b"".join(out), b"".join(err), elapsed), exec_id


def test_runner_exact_script_and_large_stdin():
    script = r'''printf '%s|' "a'b" 'c"d' "$(echo $'tab\there')" ünïcødé; cat <<'EOF'
heredoc $NOT_EXPANDED
EOF
wc -c'''
    payload = ("x" * 1023 + "\n") * 1024  # 1 MiB
    p, _ = _run(script, payload)
    out = p.stdout.decode()
    assert p.returncode == 0, p.stderr
    assert out.startswith("a'b|c\"d|tab\there|ünïcødé|heredoc $NOT_EXPANDED\n")
    assert out.rstrip().endswith(str(len(payload)))


def test_runner_exit_code_and_stderr_pass_through():
    p, _ = _run("echo oops >&2; exit 7")
    assert p.returncode == 7 and b"oops" in p.stderr


def test_runner_no_stdin_data_gives_eof():
    p, _ = _run("cat; echo done")
    assert p.stdout == b"done\n"


def test_runner_records_pgid_and_kill_stops_process_group():
    exec_id = uuid.uuid4().hex[:12]
    pgid_file = pathlib.Path(f"/tmp/hermes-exec-{exec_id}.pgid")
    proc = subprocess.Popen(exec_argv(exec_id, login=False, timeout=60), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pgid = None
    try:
        proc.stdin.write(stdin_payload("sleep 300 & sleep 300; echo never", None))
        proc.stdin.flush()  # stdin stays open, as on the real exec
        for _ in range(50):
            if pgid_file.exists() and pgid_file.read_text().strip():
                break
            time.sleep(0.1)
        pgid = int(pgid_file.read_text())
        subprocess.run(kill_argv(exec_id), check=True, timeout=10)
        assert proc.wait(timeout=10) != 0
        time.sleep(0.3)
        try:
            os.killpg(pgid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive and not pgid_file.exists()
    finally:
        # never leave sleeps behind, even on failure
        if pgid is None and pgid_file.exists() and pgid_file.read_text().strip():
            pgid = int(pgid_file.read_text())
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.kill()
        proc.wait()
        pgid_file.unlink(missing_ok=True)


def test_runner_timeout_backstop():
    start = time.time()
    p, _ = _run("sleep 30", timeout=1)
    assert p.returncode in (124, 137) and time.time() - start < 15


def test_stdin_payload_rejects_nul_in_script():
    with pytest.raises(ValueError, match="NUL"):
        stdin_payload("echo a\0b; cat", "STDIN")


def test_nul_bytes_in_stdin_data_pass_through_exactly():
    p, _ = _run("od -An -c", "a\0b\0")
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [b"a", b"\\0", b"b", b"\\0"]
    p, _ = _run("wc -c", "a\0b")
    assert p.stdout.strip() == b"3"


def test_stdin_payload_carries_both_lengths():
    assert stdin_payload("echo ü", "ab") == b"7\n2\necho \xc3\xbcab"
    assert stdin_payload("true", None) == b"4\n0\ntrue"


def test_large_stdin_reaches_command_exactly_and_ends_in_eof():
    data = "y" * 200_000
    p, _ = _run("wc -c; echo after-eof", data, wait=20)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [b"200000", b"after-eof"]


def test_empty_stdin_gives_immediate_eof_with_stdin_open():
    p, _ = _run("cat; echo done", None, wait=10)
    assert p.returncode == 0 and p.stdout == b"done\n" and p.elapsed < 5


def test_output_written_after_a_delay_is_kept():
    p, _ = _run("echo one; sleep 0.3; echo two", None, wait=10)
    assert p.stdout == b"one\ntwo\n"


def test_unread_large_stdin_does_not_block_exit():
    p, _ = _run("exit 7", "q" * 200_000, wait=10)
    assert p.returncode == 7 and p.elapsed < 5


def _run_no_reader(script, data: bytes, *, watchdog=60):
    """Write the whole payload SYNCHRONOUSLY before reading any output and never close stdin
    until the runner exits: the runner must drain stdin itself whether or not the command reads
    it. A watchdog kills the runner if the write or the wait deadlocks."""
    exec_id = uuid.uuid4().hex[:12]
    proc = subprocess.Popen(exec_argv(exec_id, login=False, timeout=60), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fired = threading.Event()

    def bark():
        fired.set()
        proc.kill()

    dog = threading.Timer(watchdog, bark)
    dog.start()
    try:
        body = script.encode()
        proc.stdin.write(f"{len(body)}\n{len(data)}\n".encode() + body + data)
        proc.stdin.flush()  # stdin stays open, as on the real exec
        out = proc.stdout.read()
        err = proc.stderr.read()
        rc = proc.wait()
    finally:
        dog.cancel()
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    assert not fired.is_set(), "runner deadlocked (watchdog fired)"
    return rc, out, err, exec_id


EIGHT_MB = bytes(range(256)) * (8 * 1024 * 1024 // 256)


def test_8mb_stdin_unread_command_gets_its_real_exit_code():
    rc, _, err, exec_id = _run_no_reader("exit 3", EIGHT_MB)
    assert rc == 3, err
    assert not list(pathlib.Path("/tmp").glob(f"hermes-exec-{exec_id}.*"))  # spool + markers removed


def test_8mb_stdin_cat_with_no_concurrent_reader_is_exact():
    import hashlib
    rc, out, err, _ = _run_no_reader("cat", EIGHT_MB)
    assert rc == 0, err
    assert len(out) == len(EIGHT_MB) and hashlib.sha256(out).digest() == hashlib.sha256(EIGHT_MB).digest()


def test_spooled_stdin_file_is_private():
    rc, out, err, _ = _run_no_reader("stat -L -c %a /proc/self/fd/0", b"secret")
    assert rc == 0, err
    assert out.strip() == b"600"


def test_tombstone_present_means_the_command_never_runs(tmp_path):
    exec_id = uuid.uuid4().hex[:12]
    tomb = pathlib.Path(f"/tmp/hermes-exec-{exec_id}.killed")
    marker = tmp_path / "ran"
    tomb.touch()
    try:
        proc = subprocess.Popen(exec_argv(exec_id, login=False, timeout=30), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.write(stdin_payload(f"touch {marker}", "x"))
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 137
        proc.stdin.close()
        assert not marker.exists() and not tomb.exists()
    finally:
        tomb.unlink(missing_ok=True)


def test_kill_during_spooling_stops_the_command_before_it_starts(tmp_path):
    exec_id = uuid.uuid4().hex[:12]
    marker = tmp_path / "ran"
    script = f"touch {marker}; cat >/dev/null".encode()
    proc = subprocess.Popen(exec_argv(exec_id, login=False, timeout=30), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(f"{len(script)}\n10\n".encode() + script + b"12345")  # half the stdin
        proc.stdin.flush()
        time.sleep(0.3)
        subprocess.run(kill_argv(exec_id), check=True, timeout=10)  # no pgid yet: tombstone only
        proc.stdin.write(b"67890")
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 137
        assert not marker.exists()
        assert not list(pathlib.Path("/tmp").glob(f"hermes-exec-{exec_id}.*"))
    finally:
        proc.kill()
        proc.wait()
        pathlib.Path(f"/tmp/hermes-exec-{exec_id}.killed").unlink(missing_ok=True)

