import importlib.util
import pathlib
import subprocess
import sys

import pytest

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
    from hermes_yaml import safe_load
    meta = safe_load((ROOT / "plugin.yaml").read_text())
    assert meta["name"] == "kubernetes-sandbox"
    assert meta["kind"] == "backend"


def test_register_adds_one_kubernetes_provider():
    mod = _load_plugin_root()
    ctx = FakeCtx()
    mod.register(ctx)
    assert len(ctx.providers) == 1
    assert ctx.providers[0].name == "kubernetes"


# Run in a fresh interpreter: blocking a module only means something before anything imported it.
_WITHOUT = r"""
import pathlib, sys, tempfile, types
for name in sys.argv[2:]:
    sys.modules[name] = None  # import raises ImportError, as when the package is not installed
if "hermes_yaml" in sys.argv[2:]:  # stand-in for PyYAML (not installed here): the same two names
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    sys.modules["yaml"] = types.SimpleNamespace(safe_load=YAML(typ="safe").load, YAMLError=YAMLError)
import importlib.util
spec = importlib.util.spec_from_file_location(
    "kubernetes_sandbox_plugin", pathlib.Path(sys.argv[1], "__init__.py"), submodule_search_locations=[sys.argv[1]])
mod = importlib.util.module_from_spec(spec)
sys.modules["kubernetes_sandbox_plugin"] = mod
spec.loader.exec_module(mod)
providers = []
mod.register(types.SimpleNamespace(register_terminal_environment_provider=providers.append))
assert [p.name for p in providers] == ["kubernetes"], providers
from kubernetes_sandbox_plugin.kubernetes_sandbox.settings import SettingsError, load_settings
with tempfile.TemporaryDirectory() as home:
    path = pathlib.Path(home, "kubernetes-sandbox.yaml")
    path.write_text("docker: false\nnamespace: sandboxes\n")
    s = load_settings(pathlib.Path(home))
    assert (s.docker, s.namespace) == (False, "sandboxes"), s
    path.write_text("docker: [false\n")
    try:
        load_settings(pathlib.Path(home))
    except SettingsError as e:
        assert "not valid YAML" in str(e), e
    else:
        raise AssertionError("invalid YAML was accepted")
print("ok")
"""


@pytest.mark.parametrize("blocked", [
    ["yaml"],  # Hermes >= v0.22 ships no PyYAML: the plugin reads YAML through Hermes's own loader
    ["hermes_yaml"],  # older Hermes, and the examples tooling outside Hermes: PyYAML
], ids=lambda b: "without-" + "-".join(b))
def test_plugin_registers_and_reads_settings_without(blocked):
    r = subprocess.run([sys.executable, "-c", _WITHOUT, str(ROOT), *blocked],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-3000:]
