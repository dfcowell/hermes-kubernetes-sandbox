"""The exec protocol inside the sandbox. The websocket exec runs a fixed RUNNER; the Hermes
script, the forwarded environment and the caller's stdin arrive on the exec's stdin as
    <script byte length>\\n<stdin byte length>\\n<env byte length>\\n<script><env><stdin>
`read` under LC_ALL=C reads the three lengths and exactly the script and env bytes without
over-consuming stdin.

The env section (K11) is shell text (`export NAME='value'` / `unset NAME…`) built by the plugin. The
runner evals it in ITS OWN shell, so the command inherits the values while no process argv
(/proc/<pid>/cmdline, `ps`) carries them: the script itself is an argv of the command's bash.
The eval fails closed: if ANY command of the section fails (a builder bug: a syntax error, a readonly
name), the runner exits 125 and the command never runs with a partial environment. The eval's stderr is
discarded, because bash echoes the offending line, value included, and Hermes returns stderr as output.
The runner leaves POSIX mode (POSIXLY_CORRECT in the container's environment) for the eval, where a
failing special builtin would exit the shell with its own status, and restores it for the command.

The lengths are computed by stdin_payload. A stream shorter than they announce (the client went away
mid-send with the connection held open) blocks in `read`/`head` until Hermes's own deadline kills
the exec.

The exec's stdin is NEVER closed. On Kata 3.28 / containerd 2.2.3 / Kubernetes 1.36.5 an exec
whose stdin has been closed loses all output the process writes after the close (measured with
`echo one; sleep 0.3; echo two` x5: stdin closed 0/5 printed "two", stdin left open 5/5), which
also swallowed Hermes's trailing cwd marker. So EOF for the command is made here, in-band.

The runner SPOOLS exactly <stdin length> bytes to a private (0600) file before it starts the
command, and the command reads that file (or /dev/null when the length is 0). The runner always
drains the upload whether or not the command reads stdin, and the command writes no output until
the upload is complete, so the client can send everything before reading without a send/recv
deadlock, and a command that exits without reading stdin still returns its own exit code. A
short spool means the client went away: exit 125. A truncated upload with the connection held
open blocks in `head` until Hermes's own deadline kills the exec (the client never does that).

Kill tombstone: kill_argv() creates /tmp/hermes-exec-<id>.killed and THEN reads the pgid file;
the command wrapper writes the pgid file and THEN exits 137 if the tombstone exists; the runner
also checks the tombstone after spooling, before starting the command. Each side writes before
it reads, so a kill that lands before the pgid exists (e.g. during spooling) is never missed.
The runner removes the spool, pgid and tombstone files when it exits."""
from __future__ import annotations

PGID_DIR = "/tmp"

RUNNER = r'''
__hs_posix=${POSIXLY_CORRECT+1}
__hs_posix_value=${POSIXLY_CORRECT-}
set +o posix
export LC_ALL=C
IFS= read -r __hs_n || exit 125
IFS= read -r __hs_in || exit 125
IFS= read -r __hs_en || exit 125
case "$__hs_n" in ""|*[!0-9]*) exit 125;; esac
case "$__hs_in" in ""|*[!0-9]*) exit 125;; esac
case "$__hs_en" in ""|*[!0-9]*) exit 125;; esac
IFS= read -r -d '' -N "$__hs_n" __hs_script || [ "${#__hs_script}" -eq "$__hs_n" ] || exit 125
__hs_env=
if [ "$__hs_en" -gt 0 ]; then
  IFS= read -r -d '' -N "$__hs_en" __hs_env || [ "${#__hs_env}" -eq "$__hs_en" ] || exit 125
fi
unset LC_ALL
__hs_base="/tmp/hermes-exec-$1"
__hs_pgid_file="$__hs_base.pgid"
__hs_tomb="$__hs_base.killed"
__hs_spool="$__hs_base.in"
__hs_login="$2"
__hs_timeout="$3"
trap 'rm -f "$__hs_spool" "$__hs_pgid_file" "$__hs_tomb"' EXIT
__hs_src=/dev/null
if [ "$__hs_in" -gt 0 ]; then
  (umask 077; exec head -c "$__hs_in" > "$__hs_spool") 2>/dev/null || exit 125
  [ "$(wc -c < "$__hs_spool")" -eq "$__hs_in" ] || exit 125
  __hs_src="$__hs_spool"
fi
[ -e "$__hs_tomb" ] && exit 137
__hs_ok=1
trap '__hs_ok=0' ERR
{ eval "$__hs_env"; } 2>/dev/null
trap - ERR
unset __hs_env
[ "$__hs_ok" = 1 ] || { echo "hermes-runner: the forwarded environment did not evaluate" >&2; exit 125; }
[ "$__hs_posix" = 1 ] && export POSIXLY_CORRECT="$__hs_posix_value"
if [ "$__hs_login" = 1 ]; then __hs_bash=(bash -l -c); else __hs_bash=(bash -c); fi
setsid --wait timeout --kill-after=10 "$((__hs_timeout + 5))" "${__hs_bash[@]}" '
  read -r _ _ _ _ __pg _ < /proc/$$/stat; echo "$__pg" > "$1"; [ -e "$2" ] && exit 137
  shift 2; eval "$1"' \
  hermes-cmd "$__hs_pgid_file" "$__hs_tomb" "$__hs_script" < "$__hs_src"
exit $?
'''


def exec_argv(exec_id: str, *, login: bool, timeout: int) -> list[str]:
    return ["bash", "-c", RUNNER, "hermes-runner", exec_id, "1" if login else "0", str(int(timeout))]


def stdin_payload(script: str, stdin_data: str | None, env_script: str = "") -> bytes:
    if "\0" in script:
        raise ValueError("script contains a NUL byte, which bash cannot execute")
    if "\0" in env_script:
        raise ValueError("forwarded environment contains a NUL byte")
    body, data, env = script.encode("utf-8"), (stdin_data or "").encode("utf-8"), env_script.encode("utf-8")
    return f"{len(body)}\n{len(data)}\n{len(env)}\n".encode() + body + env + data


def kill_argv(exec_id: str) -> list[str]:
    base = f"{PGID_DIR}/hermes-exec-{exec_id}"
    # Tombstone FIRST, then the pgid (see the module docstring). With no pgid the tombstone stays
    # for the runner to find; after a kill the runner (or this) removes it.
    return ["bash", "-c",
            f': > {base}.killed; pg=$(cat {base}.pgid 2>/dev/null) && [ -n "$pg" ] || exit 0; '
            f'kill -TERM -- "-$pg" 2>/dev/null; '
            f'for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 -- "-$pg" 2>/dev/null || break; sleep 0.2; done; '
            f'kill -KILL -- "-$pg" 2>/dev/null; rm -f {base}.pgid {base}.killed; exit 0']
