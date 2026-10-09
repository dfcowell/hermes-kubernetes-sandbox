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
every check passed. The Pod is always deleted at the end.
"""
import pathlib
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

    from kubernetes_sandbox.environment import KubernetesEnvironment
    from kubernetes_sandbox.naming import Identity
    from kubernetes_sandbox.settings import load_settings

    with tempfile.TemporaryDirectory() as home:
        shutil.copy(src, pathlib.Path(home) / "kubernetes-sandbox.yaml")
        s = load_settings(pathlib.Path(home))

    env = KubernetesEnvironment(settings=s, identity=Identity("e2e", f"run-{int(time.time())}"),
                                cwd="/workspace", timeout=120)
    results: list[bool] = []

    def check(label, ok, detail=""):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + label + (f": {detail}" if detail else ""), flush=True)

    try:
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
        r = env.execute("exit 9")
        check("exit code", r["returncode"] == 9, f"returncode={r['returncode']}")
        if s.docker:
            r = env.execute("docker run --rm alpine:3.24.2 echo hello-from-inner", timeout=300)
            check("docker run", "hello-from-inner" in r["output"], r["output"].strip()[-100:])
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
        gone = False
        for _ in range(7):  # remote kill escalates TERM -> KILL within ~2 s
            if not alive("/tmp/e2e-sleeper.pid"):
                gone = True
                break
            time.sleep(0.5)
        check("timeout killed remote process", gone and control_ok,
              "" if gone else "still alive after ~3 s")
        env._client.delete_pod(env._id.pod_name, grace_seconds=0)
        env._wait_gone(env._id.pod_name)
        r = env.execute("echo back")  # cwd is /workspace/sub: the new Pod must recreate it
        check("recreated after deletion, cwd kept",
              r["returncode"] == 0 and "back" in r["output"] and r.get("environment_recreated") is True
              and env.cwd == "/workspace/sub",
              f"returncode={r.get('returncode')} environment_recreated={r.get('environment_recreated')} "
              f"cwd={env.cwd}")
    except Exception as e:  # report, don't leak payloads
        check("unexpected error", False, type(e).__name__ + ": " + str(e)[:200])
    finally:
        try:
            env._client.delete_pod(env._id.pod_name, grace_seconds=0)
        except Exception:
            pass
        env.cleanup()
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
