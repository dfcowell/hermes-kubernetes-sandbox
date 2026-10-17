#!/usr/bin/env python3
"""Live end-to-end check against a real cluster. Never run in CI.

Usage:  .venv/bin/python scripts/e2e.py <settings.yaml>

<settings.yaml> may have any filename; it is copied into a temporary directory as
``kubernetes-sandbox.yaml`` and loaded with ``load_settings`` (HERMES_K8S_* env vars still
override, as in production). The settings must name a token file, e.g.
``kubectl -n hermes create token hermes-sandbox-driver --duration=1h > /tmp/sbx-token``.
Which namespace/runtime is exercised (Kata ``agent-sandbox``, runc ``agent-sandbox-runc``)
is decided entirely by the settings file.

Prints only PASS/FAIL lines with short details (never the token or exec payloads). Exit 0 iff
every check passed. The probe variable E2E_FORWARDED_TOKEN is forwarded through the documented
HERMES_K8S_FORWARD_ENV override (the file's own forward_env plus the probe), so it passes the same
validation as an operator's setting. The Pod and the session's workspace PVC are always deleted at
the end: with a driver Role that has PVC delete the script deletes the PVC itself ("deleted
workspace PVC ..."); without it the deletion is refused and the script prints the kubectl command
to remove it (a WARN line, not a failed check).
"""
import json
import os
import pathlib
import re
import secrets
import shutil
import sys
import tempfile
import time

# Run as documented (from the repo root, no PYTHONPATH), sys.path[0] is scripts/; the plugin
# package lives in the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    src = pathlib.Path(argv[1])
    if not src.is_file():
        print(f"settings file not found: {src}", file=sys.stderr)
        return 2

    from kubernetes_sandbox import environment as envmod
    from kubernetes_sandbox.environment import KubernetesEnvironment
    from kubernetes_sandbox.kube import NotFound
    from kubernetes_sandbox.naming import Identity
    from kubernetes_sandbox.settings import SettingsError, load_settings

    # The forwarded-value probe goes through the real settings path, exactly as an operator would configure
    # it: the documented HERMES_K8S_FORWARD_ENV override, carrying the file's own forward_env plus the probe.
    fwd = "E2E_FORWARDED_TOKEN"
    os.environ[fwd] = "e2e-" + secrets.token_hex(16)
    with tempfile.TemporaryDirectory() as home:
        shutil.copy(src, pathlib.Path(home) / "kubernetes-sandbox.yaml")
        s = load_settings(pathlib.Path(home))
        prev = os.environ.get("HERMES_K8S_FORWARD_ENV")
        os.environ["HERMES_K8S_FORWARD_ENV"] = ",".join(dict.fromkeys((*s.forward_env, fwd)))
        s = load_settings(pathlib.Path(home))
        # Negative control: a denylisted credential name is refused by the same path.
        os.environ["HERMES_K8S_FORWARD_ENV"] = ",".join(dict.fromkeys((*s.forward_env, "LITELLM_API_KEY")))
        try:
            load_settings(pathlib.Path(home))
            denied = "accepted"
        except SettingsError as e:
            denied = "refused" if "LITELLM_API_KEY cannot be forwarded" in str(e) else f"other error: {e}"
        if prev is None:
            del os.environ["HERMES_K8S_FORWARD_ENV"]
        else:
            os.environ["HERMES_K8S_FORWARD_ENV"] = prev

    env = KubernetesEnvironment(settings=s, identity=Identity("e2e", f"run-{int(time.time())}"),
                                cwd="/workspace", timeout=120)
    results: list[bool] = []

    def check(label, ok, detail=""):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label + (f": {detail}" if detail else ""), flush=True)

    def delete_pod_gracefully(which):
        """Model a real out-of-band deletion (janitor, eviction): graceful, never a force delete,
        which would let the next Pod mount the workspace while the old one still holds it."""
        env._client.delete_pod(env._id.pod_name, grace_seconds=envmod._DELETE_GRACE)
        gone = env._wait_gone(env._id.pod_name)
        check(f"{which} Pod gone after a graceful delete", gone, "" if gone else "still terminating")

    try:
        check(f"forward_env {fwd} accepted by load_settings", fwd in s.forward_env)
        check("forward_env LITELLM_API_KEY refused by load_settings", denied == "refused", denied)
        t0 = time.time()
        r = env.execute("echo ready")  # first execute creates the Pod and builds the snapshot
        check("pod ready + session snapshot + first command",
              r["returncode"] == 0 and "ready" in r["output"], f"{time.time() - t0:.1f}s")
        r = env.execute("echo one; sleep 0.5; echo two")
        check("late output survives (stdin never closed)",
              r["returncode"] == 0 and "one" in r["output"] and "two" in r["output"],
              r["output"].strip()[:100])
        env.execute("cd /tmp")
        check("cwd tracked", env.cwd == "/tmp", f"cwd={env.cwd}")
        env.execute("cd /workspace")
        env.execute("mkdir -p sub && cd sub && export E2E=yes")
        r = env.execute('echo "$E2E $(pwd)"; a=(x y); echo ${a[1]} {1..3}')
        check("cd/export persist; real bash",
              "yes /workspace/sub" in r["output"] and "y 1 2 3" in r["output"], r["output"].strip()[:100])
        big = "z" * 2_000_000
        r = env.execute("wc -c", stdin_data=big)
        check("2 MB stdin exact", r["output"].strip().endswith(str(len(big))), r["output"].strip()[-40:])
        r = env.execute("exit 3", stdin_data="u" * 8_000_000)
        check("8 MB stdin, command never reads it: real exit code", r["returncode"] == 3,
              f"returncode={r['returncode']}")
        r = env.execute("exit 9")
        check("exit code", r["returncode"] == 9, f"returncode={r['returncode']}")
        val = os.environ[fwd]
        r = env.execute(f'printf "%s\\n" "${fwd}"')
        check("forwarded variable reaches the command", r["output"].strip().endswith(val))
        r = env.execute('for f in /proc/[0-9]*/cmdline; do tr "\\0" " " < "$f" 2>/dev/null; echo; done '
                        f'| grep -c -F -f <(printf "%s\\n" "${fwd}") || true; '
                        f'grep -rlF -f <(printf "%s\\n" "${fwd}") /workspace /tmp 2>/dev/null | wc -l')
        tail = r["output"].strip().splitlines()[-2:]
        check("forwarded value in no process argv and no file under /workspace or /tmp", tail == ["0", "0"],
              " ".join(tail))
        check("forwarded value absent from the Pod object",
              val not in json.dumps(env._client.get_pod(env._id.pod_name)))
        # git_credentials configures git through GIT_CONFIG_* environment, which needs git >= 2.31.
        r = env.execute("git --version")
        m = re.search(r"git version (\d+)\.(\d+)", r["output"])
        check("work image git >= 2.31 (GIT_CONFIG_* credentials)",
              m is not None and (int(m[1]), int(m[2])) >= (2, 31), r["output"].strip()[-40:])
        if s.docker:
            r = env.execute("docker run --rm alpine:3.24.2 echo hello-from-inner", timeout=300)
            check("docker run", "hello-from-inner" in r["output"], r["output"].strip()[-100:])
        check_background_notify(env, check)
        def alive(pidfile):
            probe = (f'p=$(cat {pidfile}) && if kill -0 "$p" 2>/dev/null; '
                     'then echo alive; else echo gone; fi')
            return env.execute(probe)["output"].strip().endswith("alive")

        # Positive control: prove the pid probe can see a live process before trusting "gone".
        env.execute("sleep 300 > /dev/null 2>&1 & echo $! > /tmp/e2e-control.pid")
        control_ok = alive("/tmp/e2e-control.pid")
        check("pid probe sees a live process (positive control)", control_ok,
              "" if control_ok else "probe cannot see processes")
        env.execute('kill "$(cat /tmp/e2e-control.pid)"')
        t0 = time.time()
        r = env.execute("echo $$ > /tmp/e2e-sleeper.pid; exec sleep 120", timeout=5)
        elapsed = time.time() - t0
        check("timeout returns promptly with returncode 124", elapsed < 30 and r["returncode"] == 124,
              f"{elapsed:.1f}s returncode={r['returncode']}")
        pid_ok = env.execute('test -s /tmp/e2e-sleeper.pid && echo present')["output"].strip().endswith("present")
        check("sleeper pidfile written (so 'gone' below means killed)", pid_ok)
        gone = False
        for _ in range(7):  # remote kill escalates TERM -> KILL within ~2 s
            if not alive("/tmp/e2e-sleeper.pid"):
                gone = True
                break
            time.sleep(0.5)
        check("timeout killed remote process", gone and control_ok and pid_ok,
              "" if gone else "still alive after ~3 s")
        delete_pod_gracefully("first")
        # timeout=10 is far shorter than a Kata cold start: the recreate must happen before the
        # command's bounded wait starts, never inside it (no synthetic 124).
        t0 = time.time()
        r = env.execute("echo back", timeout=10)  # cwd is /workspace/sub: the new Pod must recreate it
        check("recreate is outside the command timeout (no synthetic 124)", r.get("returncode") != 124,
              f"returncode={r.get('returncode')} after {time.time() - t0:.1f}s")
        check("recreated after deletion, cwd kept",
              r["returncode"] == 0 and "back" in r["output"] and r.get("environment_recreated") is True
              and env.cwd == "/workspace/sub",
              f"returncode={r.get('returncode')} environment_recreated={r.get('environment_recreated')} "
              f"cwd={env.cwd}")
        # Workspace persistence: files under /workspace survive the Pod. The cat is the FIRST
        # command after the deletion, so it is also the one that carries environment_recreated.
        marker = secrets.token_hex(16)
        r = env.execute(f"printf %s {marker} > /workspace/e2e-persist.txt && sync")
        check("marker written to /workspace", r["returncode"] == 0, f"returncode={r['returncode']}")
        delete_pod_gracefully("second")
        r = env.execute("cat /workspace/e2e-persist.txt")
        check("workspace survives Pod deletion",
              r["returncode"] == 0 and r["output"].strip() == marker and r.get("environment_recreated") is True,
              f"returncode={r['returncode']} same_content={r['output'].strip() == marker} "
              f"environment_recreated={r.get('environment_recreated')}")
        claim = env._client.get_pvc(env._id.workspace_claim)
        env.cleanup()
        pod_gone = env._wait_gone(env._id.pod_name)
        try:
            after = env._client.get_pvc(env._id.workspace_claim)
            kept = (after["metadata"]["uid"] == claim["metadata"]["uid"]
                    and not after["metadata"].get("deletionTimestamp"))
        except NotFound:
            kept = False
        check("cleanup keeps the volume", pod_gone and kept, f"pod_gone={pod_gone} pvc_kept={kept}")
    except Exception as e:  # report, don't leak payloads
        check("unexpected error", False, type(e).__name__ + ": " + str(e)[:200])
    finally:
        env.cleanup()  # graceful, like every plugin-side delete
        delete_workspace(env)
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


def check_background_notify(env, check) -> None:
    """A background job through Hermes's own path -- the terminal tool's background=True,
    notify=True (spawn_background_process -> spawn_via_env -> the env poller) -- must queue its
    completion after it exits. The poller's exit test is `kill -0 <wrapper pid>`, which succeeds on a
    zombie, so this fails under a PID 1 that does not reap (F-P3-5)."""
    import tempfile
    prev = os.environ.get("HERMES_HOME")
    sid = None
    done = False
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home  # the registry checkpoints here, not in the operator's ~/.hermes
        try:
            from tools.process_registry import process_registry
            from tools.terminal_tool_background import spawn_background_process
            started = json.loads(spawn_background_process(
                command="sleep 5; echo bg-done", env=env, env_type="kubernetes", effective_task_id="e2e-bg",
                task_id="e2e-bg", session_key="", workdir=None, cwd="/workspace", effective_pty=False,
                notify_on_complete=True, watch_patterns=None, approval_note=None, pty_disabled_reason=None))
            sid, pid = started.get("session_id"), started.get("pid")
            check("background job started with notify (Hermes's terminal-tool path)",
                  bool(pid) and started.get("notify_on_complete") is True,
                  f"pid={pid} notify_on_complete={started.get('notify_on_complete')}")
            evt, held, deadline = None, [], time.time() + 60
            while evt is None and time.time() < deadline:
                try:
                    e = process_registry.completion_queue.get(timeout=1)
                except Exception:
                    continue
                if e.get("session_id") == sid and e.get("type") == "completion":
                    evt = e
                else:
                    held.append(e)
            for e in held:
                process_registry.completion_queue.put(e)
            done = evt is not None
            check("completion queued after the background job exits",
                  done and evt.get("exit_code") == 0 and evt.get("completion_reason") == "exited",
                  "no completion within 60 s" if not done else
                  f"exit_code={evt.get('exit_code')} reason={evt.get('completion_reason')} "
                  f"output_seen={'bg-done' in (evt.get('output') or '')}")
            if pid:
                r = env.execute(f"kill -0 {int(pid)} 2>/dev/null && echo alive || echo gone")
                check("wrapper pid gone after exit (kill -0 fails)", r["output"].strip().endswith("gone"),
                      r["output"].strip()[-20:])
            zombies = []
            for _ in range(3):  # a process that exited this instant is not reaped yet: re-look
                r = env.execute("ps -eo pid=,stat=,comm=")
                zombies = [ln.strip() for ln in r["output"].splitlines()
                           if len(ln.split()) >= 2 and ln.split()[1].startswith("Z")]
                if not zombies:
                    break
                time.sleep(1)
            check("no zombie in the work container", r["returncode"] == 0 and not zombies,
                  f"returncode={r['returncode']} zombies={zombies[:5]}")
            r = env.execute("tr '\\0' ' ' < /proc/1/cmdline")
            check("work PID 1 is the reaping bash loop", "wait $!" in r["output"], r["output"].strip()[-80:])
        finally:
            if sid and not done:
                try:  # stop the poller; never leave it running against the Pod
                    process_registry.kill_process(sid)
                except Exception:
                    pass
            if prev is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = prev


def delete_workspace(env) -> None:
    """Test artifacts must not linger: delete the session's workspace PVC (needs PVC delete)."""
    from kubernetes_sandbox.kube import KubeError, NotFound
    name, ns = env._id.workspace_claim, env._s.namespace
    try:
        md = env._client.get_pvc(name)["metadata"]
        env._client.delete_pvc(name, uid=md["uid"], resource_version=md["resourceVersion"])
        print(f"deleted workspace PVC {name}", flush=True)
    except NotFound:
        pass
    except KubeError as e:
        print(f"WARN workspace PVC {name} not deleted ({e.status} {e.reason}); "
              f"remove it: kubectl -n {ns} delete pvc {name}", flush=True)
    except Exception as e:
        print(f"WARN workspace PVC {name} not deleted ({type(e).__name__}); "
              f"remove it: kubectl -n {ns} delete pvc {name}", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
