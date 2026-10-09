"""Hermes plugin entry point: registers the `kubernetes` terminal backend."""


def register(ctx):
    from .kubernetes_sandbox.provider import KubernetesProvider
    ctx.register_terminal_environment_provider(KubernetesProvider())
