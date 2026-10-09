# hermes-kubernetes-sandbox

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) terminal backend plugin that runs
each session's commands in its own Kubernetes Pod — optionally with its own Docker daemon, so
agents can run container-based test suites — instead of on the gateway host.

**Status: pre-alpha. Nothing to install yet.**

## Shape

- A `TerminalEnvironmentProvider` plugin; the provider name is `kubernetes`.
- One sandbox Pod per Hermes session, reaped when idle.
- The RuntimeClass is configuration. Docker-in-sandbox is optional, and the plugin refuses to run
  a privileged Docker daemon without a VM-isolated runtime unless explicitly told to.
- Mainstream images only: Hermes's own sandbox image for the shell, the official `docker:*-dind`
  image for the daemon.
- `examples/kata/` (planned): the Kata Containers happy path — RuntimeClass sizing, admission
  policy, network policy, RBAC and node configuration, validated by a multi-hour soak.

## License

MIT

## Development

    uv venv -p 3.13 .venv
    git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git .hermes-src && git -C .hermes-src checkout --quiet 524041b9d0437cd4424a524af4b9e26145e69ab0 && uv pip install --python .venv -e .hermes-src pytest==9.1.1 ruff==0.15.10
    .venv/bin/ruff check . && .venv/bin/pytest
