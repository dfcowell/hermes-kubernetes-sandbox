"""Git authentication inside the sandbox, from forwarded environment only (K11).

git reads `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` as command-scope configuration
(git >= 2.31), read after the system and global files. Per configured URL this sets
`credential.<url>.helper` twice: first to the empty string, which resets the helper list inherited from the
image's or the user's configuration (so no `store` helper ever writes the secret to disk), then to a shell
helper that answers `get` with the username and the forwarded variable's value. The helper text names the
variable, never its value: the secret is read from the environment each time git asks, and a missing or
empty variable fails with a message naming it. `GIT_TERMINAL_PROMPT=0` makes that failure final instead of
hanging on a prompt."""
from __future__ import annotations

import shlex

from .settings import GitCredential


def helper_command(c: GitCredential) -> str:
    # Settings already restrict username and password_env to shell-safe characters; the username is quoted
    # anyway. printf rather than echo: some shells' echo interprets backslashes in the secret.
    return ("!f() { test \"$1\" = get || exit 0; printf '%s\\n' " + shlex.quote(f"username={c.username}") + "; "
            "printf 'password=%s\\n' \"${" + c.password_env + ":?" + c.password_env
            + " is not forwarded into this sandbox}\"; }; f")


def git_config_env(creds: tuple[GitCredential, ...]) -> dict[str, str]:
    if not creds:
        return {}
    pairs: list[tuple[str, str]] = []
    for c in creds:
        key = f"credential.{c.url}.helper"
        pairs += [(key, ""), (key, helper_command(c))]
    env = {"GIT_CONFIG_COUNT": str(len(pairs)), "GIT_TERMINAL_PROMPT": "0"}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def credential_names(creds: tuple[GitCredential, ...]) -> set[str]:
    """Every variable the plugin sets for git, plus the password variables: none may enter the snapshot."""
    return set(git_config_env(creds)) | {c.password_env for c in creds}
