import base64
import os
import secrets
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kubernetes_sandbox.credentials import credential_names, git_config_env, helper_command
from kubernetes_sandbox.settings import GitCredential

C = GitCredential("https://forge.example.test", "my-bot", "FORGE_TOKEN")
FILL = "protocol=https\nhost=forge.example.test\n\n"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(env_extra, args, stdin, home, cwd=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", **env_extra}
    return subprocess.run(["git", *args], input=stdin, capture_output=True, text=True, env=env, timeout=30,
                          cwd=cwd)


def test_no_credentials_no_environment():
    assert git_config_env(()) == {} and credential_names(()) == set()


def test_environment_shape():
    env = git_config_env((C,))
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == env["GIT_CONFIG_KEY_1"] == "credential.https://forge.example.test.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""  # resets helpers inherited from system/global config for this URL
    assert env["GIT_CONFIG_VALUE_1"] == helper_command(C)
    assert credential_names((C,)) == set(env) | {"FORGE_TOKEN"}


def test_helper_names_the_variable_never_a_value():
    h = helper_command(C)
    assert "${FORGE_TOKEN:?" in h and "username=my-bot" in h


def test_two_forges_are_numbered_consecutively():
    other = GitCredential("https://git.example.test/org", "bot2", "OTHER_TOKEN")
    env = git_config_env((C, other))
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_2"] == env["GIT_CONFIG_KEY_3"] == "credential.https://git.example.test/org.helper"
    assert env["GIT_CONFIG_VALUE_2"] == "" and env["GIT_CONFIG_VALUE_3"] == helper_command(other)
    assert credential_names((C, other)) == set(env) | {"FORGE_TOKEN", "OTHER_TOKEN"}


@needs_git
def test_git_fill_answers_from_the_forwarded_variable(tmp_path):
    r = _git({**git_config_env((C,)), "FORGE_TOKEN": "tok-123"}, ["credential", "fill"], FILL, tmp_path)
    assert r.returncode == 0, r.stderr
    assert {"username=my-bot", "password=tok-123"} <= set(r.stdout.splitlines())


@needs_git
def test_token_is_passed_through_verbatim(tmp_path):
    # printf, not echo: a shell whose echo interprets backslashes (dash) must not mangle the secret.
    token = r"a\nb\\c\c$(id)`id`'\"-n%s%d"
    r = _git({**git_config_env((C,)), "FORGE_TOKEN": token}, ["credential", "fill"], FILL, tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"password={token}" in r.stdout.splitlines()


@needs_git
def test_missing_variable_fails_loudly_and_never_prompts(tmp_path):
    r = _git(git_config_env((C,)), ["credential", "fill"], FILL, tmp_path)
    assert r.returncode != 0
    assert "FORGE_TOKEN is not forwarded into this sandbox" in r.stderr


@needs_git
def test_empty_variable_fails_like_a_missing_one(tmp_path):
    r = _git({**git_config_env((C,)), "FORGE_TOKEN": ""}, ["credential", "fill"], FILL, tmp_path)
    assert r.returncode != 0
    assert "FORGE_TOKEN is not forwarded into this sandbox" in r.stderr


@needs_git
def test_helper_ignores_store_and_erase(tmp_path):
    env = {**git_config_env((C,)), "FORGE_TOKEN": "tok-123"}
    for action in ("approve", "reject"):
        r = _git(env, ["credential", action], FILL.rstrip("\n") + "\nusername=my-bot\npassword=tok-123\n\n", tmp_path)
        assert r.returncode == 0 and "tok-123" not in r.stdout + r.stderr


@needs_git
def test_username_cannot_inject_shell(tmp_path):
    # Settings validation forbids this username; the helper quotes it anyway (defence in depth).
    marker = tmp_path / "pwned"
    evil = GitCredential("https://forge.example.test", f"x'; touch {marker}; echo '$(touch {marker})", "FORGE_TOKEN")
    r = _git({**git_config_env((evil,)), "FORGE_TOKEN": "tok-123"}, ["credential", "fill"], FILL, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    assert f"username={evil.username}" in r.stdout.splitlines()


@needs_git
def test_inherited_store_helper_is_reset_so_nothing_is_written(tmp_path):
    store = tmp_path / "stored-credentials"
    (tmp_path / ".gitconfig").write_text(f"[credential]\n\thelper = store --file={store}\n")
    env = {**git_config_env((C,)), "FORGE_TOKEN": "tok-123"}
    fill = _git(env, ["credential", "fill"], FILL, tmp_path)
    assert fill.returncode == 0, fill.stderr
    approve = _git(env, ["credential", "approve"], fill.stdout, tmp_path)
    assert approve.returncode == 0, approve.stderr
    assert not store.exists()


@needs_git
def test_other_hosts_are_not_answered(tmp_path):
    r = _git({**git_config_env((C,)), "FORGE_TOKEN": "tok-123"}, ["credential", "fill"],
             "protocol=https\nhost=elsewhere.example.test\n\n", tmp_path)
    assert r.returncode != 0 and "tok-123" not in r.stdout


# --- End to end: clone from and push to a bare repository behind a real HTTPS + basic-auth server ---------


class _GitHTTP(BaseHTTPRequestHandler):
    """`git http-backend` as CGI behind HTTP basic auth (user and token fixed per server)."""

    def log_message(self, *args):
        pass

    def _serve(self):
        expected = "Basic " + base64.b64encode(f"{self.server.user}:{self.server.token}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="forge"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = b""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while (size := int(self.rfile.readline().strip(), 16)):
                body += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        env = {"PATH": os.environ["PATH"], "GIT_PROJECT_ROOT": str(self.server.root), "GIT_HTTP_EXPORT_ALL": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.server.root),
               "REQUEST_METHOD": self.command, "PATH_INFO": path, "QUERY_STRING": query,
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "REMOTE_USER": self.server.user, "REMOTE_ADDR": "127.0.0.1",
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")}
        out = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env, timeout=30).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve


@pytest.fixture
def forge(tmp_path):
    if shutil.which("git") is None or shutil.which("openssl") is None:
        pytest.skip("needs git and openssl")
    srv_dir = tmp_path / "server"
    (srv_dir / "org").mkdir(parents=True)
    cert, key = srv_dir / "cert.pem", srv_dir / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=60)
    seed_home = tmp_path / "seed-home"
    seed_home.mkdir()
    seed = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    bare = srv_dir / "org" / "repo.git"
    assert _git(seed, ["init", "--bare", "-b", "main", str(bare)], None, seed_home).returncode == 0
    work = tmp_path / "seed-work"
    assert _git(seed, ["clone", str(bare), str(work)], None, seed_home).returncode == 0
    (work / "README").write_text("hello\n")
    for args in (["add", "README"], ["commit", "-m", "init"], ["push", "origin", "HEAD:main"]):
        r = _git(seed, args, None, seed_home, cwd=work)
        assert r.returncode == 0, r.stderr
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHTTP)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.root, server.user, server.token = srv_dir, "my-bot", "tok-" + secrets.token_hex(16)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, cert, bare
    finally:
        server.shutdown()
        server.server_close()


def _files_containing(roots, needle: bytes, since: float):
    hits = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root, onerror=lambda e: None):
            for name in files:
                p = Path(dirpath) / name
                try:
                    st = p.lstat()
                    if not p.is_file() or p.is_symlink() or st.st_mtime < since or st.st_size > 64 << 20:
                        continue
                    if needle in p.read_bytes():
                        hits.append(p)
                except OSError:
                    continue
    return hits


@pytest.mark.parametrize("cred_path", ["", "/org"])
def test_clone_and_push_leave_the_token_in_no_file(forge, tmp_path, cred_path):
    server, cert, bare = forge
    start = time.time() - 1
    port = server.server_address[1]
    cred = GitCredential(f"https://localhost:{port}{cred_path}", "my-bot", "FORGE_TOKEN")
    home = tmp_path / "home"
    home.mkdir()
    store = home / ".git-credentials"
    # A pre-existing global store helper: without the reset it would write the token to ~/.git-credentials.
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n[user]\n\tname = t\n\temail = t@t\n")
    env = {**git_config_env((cred,)), "GIT_SSL_CAINFO": str(cert)}
    url = f"https://localhost:{port}/org/repo.git"
    clone = tmp_path / "clone"

    denied = _git(env, ["clone", url, str(clone)], None, home)
    assert denied.returncode != 0 and "FORGE_TOKEN is not forwarded into this sandbox" in denied.stderr
    wrong = _git({**env, "FORGE_TOKEN": "wrong"}, ["clone", url, str(clone)], None, home)
    assert wrong.returncode != 0 and not store.exists()

    env["FORGE_TOKEN"] = server.token
    r = _git(env, ["clone", url, str(clone)], None, home)
    assert r.returncode == 0, r.stderr
    (clone / "CHANGE").write_text("pushed\n")
    for args in (["add", "CHANGE"], ["commit", "-m", "change"], ["push", "origin", "HEAD:main"]):
        r = _git(env, args, None, home, cwd=clone)
        assert r.returncode == 0, r.stderr
    log = _git({}, ["--git-dir", str(bare), "log", "--format=%s", "main"], None, home)
    assert log.stdout.split() == ["change", "init"]

    assert not store.exists()
    assert not _files_containing([home, clone, bare, Path("/tmp")], server.token.encode(), start)
