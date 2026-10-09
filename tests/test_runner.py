import os
import pathlib
import signal
import subprocess
import time
import uuid

from kubernetes_sandbox.runner import exec_argv, kill_argv, stdin_payload


def _run(script, stdin_data=None, *, login=False, timeout=30):
    exec_id = uuid.uuid4().hex[:12]
    argv = exec_argv(exec_id, login=login, timeout=timeout)
    p = subprocess.run(argv, input=stdin_payload(script, stdin_data), capture_output=True, timeout=60)
    return p, exec_id


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
        proc.stdin.close()
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
