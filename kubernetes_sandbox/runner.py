"""The exec protocol inside the sandbox. The websocket exec runs a fixed RUNNER; the Hermes
script and the caller's stdin arrive on stdin as  <script byte length>\\n<script><stdin...>.
`read -N` under LC_ALL=C reads exactly that many bytes without over-consuming stdin."""
from __future__ import annotations

PGID_DIR = "/tmp"

RUNNER = r'''
export LC_ALL=C
IFS= read -r __hs_n || exit 125
IFS= read -r -d '' -N "$__hs_n" __hs_script || [ "${#__hs_script}" -eq "$__hs_n" ] || exit 125
unset LC_ALL
__hs_pgid_file="/tmp/hermes-exec-$1.pgid"
__hs_login="$2"
__hs_timeout="$3"
if [ "$__hs_login" = 1 ]; then __hs_bash=(bash -l -c); else __hs_bash=(bash -c); fi
setsid --wait timeout --kill-after=10 "$((__hs_timeout + 5))" "${__hs_bash[@]}" '
  read -r _ _ _ _ __pg _ < /proc/$$/stat; echo "$__pg" > "$1"; shift; eval "$1"' \
  hermes-cmd "$__hs_pgid_file" "$__hs_script"
__hs_rc=$?
rm -f "$__hs_pgid_file"
exit "$__hs_rc"
'''


def exec_argv(exec_id: str, *, login: bool, timeout: int) -> list[str]:
    return ["bash", "-c", RUNNER, "hermes-runner", exec_id, "1" if login else "0", str(int(timeout))]


def stdin_payload(script: str, stdin_data: str | None) -> bytes:
    if "\0" in script:
        raise ValueError("script contains a NUL byte, which bash cannot execute")
    body = script.encode("utf-8")
    return f"{len(body)}\n".encode() + body + (stdin_data or "").encode("utf-8")


def kill_argv(exec_id: str) -> list[str]:
    f = f"{PGID_DIR}/hermes-exec-{exec_id}.pgid"
    return ["bash", "-c",
            f'pg=$(cat {f} 2>/dev/null) || exit 0; kill -TERM -- "-$pg" 2>/dev/null; '
            f'for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 -- "-$pg" 2>/dev/null || break; sleep 0.2; done; '
            f'kill -KILL -- "-$pg" 2>/dev/null; rm -f {f}; exit 0']
