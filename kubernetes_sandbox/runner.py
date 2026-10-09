"""The exec protocol inside the sandbox. The websocket exec runs a fixed RUNNER; the Hermes
script and the caller's stdin arrive on the exec's stdin as
    <script byte length>\\n<stdin byte length>\\n<script><stdin>
`read` under LC_ALL=C reads the two lengths and exactly the script bytes without over-consuming
stdin; the command then gets exactly <stdin byte length> bytes followed by EOF (`head -c`, or
/dev/null when it is 0).

The exec's stdin is NEVER closed. On Kata 3.28 / containerd 2.2.3 / Kubernetes 1.36.5 an exec
whose stdin has been closed loses all output the process writes after the close (dev cluster,
`echo one; sleep 0.3; echo two` x5: stdin closed 0/5 printed "two", stdin left open 5/5), which
also swallowed Hermes's trailing cwd marker. So EOF for the command is made here, in-band, and
`head` is killed when the command ends so it can never hold the runner open."""
from __future__ import annotations

PGID_DIR = "/tmp"

RUNNER = r'''
export LC_ALL=C
IFS= read -r __hs_n || exit 125
IFS= read -r __hs_in || exit 125
case "$__hs_n" in ""|*[!0-9]*) exit 125;; esac
case "$__hs_in" in ""|*[!0-9]*) exit 125;; esac
IFS= read -r -d '' -N "$__hs_n" __hs_script || [ "${#__hs_script}" -eq "$__hs_n" ] || exit 125
unset LC_ALL
__hs_pgid_file="/tmp/hermes-exec-$1.pgid"
__hs_login="$2"
__hs_timeout="$3"
__hs_head=
if [ "$__hs_in" -gt 0 ]; then
  exec 3< <(exec head -c "$__hs_in" 2>/dev/null)
  __hs_head=$!
else
  exec 3</dev/null
fi
if [ "$__hs_login" = 1 ]; then __hs_bash=(bash -l -c); else __hs_bash=(bash -c); fi
setsid --wait timeout --kill-after=10 "$((__hs_timeout + 5))" "${__hs_bash[@]}" '
  read -r _ _ _ _ __pg _ < /proc/$$/stat; echo "$__pg" > "$1"; shift; eval "$1"' \
  hermes-cmd "$__hs_pgid_file" "$__hs_script" <&3 3<&-
__hs_rc=$?
exec 3<&-
[ -n "$__hs_head" ] && kill "$__hs_head" 2>/dev/null
rm -f "$__hs_pgid_file"
exit "$__hs_rc"
'''


def exec_argv(exec_id: str, *, login: bool, timeout: int) -> list[str]:
    return ["bash", "-c", RUNNER, "hermes-runner", exec_id, "1" if login else "0", str(int(timeout))]


def stdin_payload(script: str, stdin_data: str | None) -> bytes:
    if "\0" in script:
        raise ValueError("script contains a NUL byte, which bash cannot execute")
    body, data = script.encode("utf-8"), (stdin_data or "").encode("utf-8")
    return f"{len(body)}\n{len(data)}\n".encode() + body + data


def kill_argv(exec_id: str) -> list[str]:
    f = f"{PGID_DIR}/hermes-exec-{exec_id}.pgid"
    return ["bash", "-c",
            f'pg=$(cat {f} 2>/dev/null) || exit 0; kill -TERM -- "-$pg" 2>/dev/null; '
            f'for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 -- "-$pg" 2>/dev/null || break; sleep 0.2; done; '
            f'kill -KILL -- "-$pg" 2>/dev/null; rm -f {f}; exit 0']
