import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeCtx:
    def __init__(self):
        self.providers = []

    def register_terminal_environment_provider(self, provider):
        self.providers.append(provider)


def _load_plugin_root():
    spec = importlib.util.spec_from_file_location(
        "kubernetes_sandbox_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["kubernetes_sandbox_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manifest_is_backend_kind():
    import yaml
    meta = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert meta["name"] == "kubernetes-sandbox"
    assert meta["kind"] == "backend"


def test_register_adds_one_kubernetes_provider():
    mod = _load_plugin_root()
    ctx = FakeCtx()
    mod.register(ctx)
    assert len(ctx.providers) == 1
    assert ctx.providers[0].name == "kubernetes"
