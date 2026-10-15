import importlib.util
import sys

import pytest
from isolated import LOADERS, ROOT, run_isolated


class FakeCtx:
    def __init__(self):
        self.providers = []

    def register_terminal_environment_provider(self, provider):
        self.providers.append(provider)


def _load_plugin_root():
    spec = importlib.util.spec_from_file_location(
        "kubernetes_sandbox_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
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



_REGISTER = r"""
import pathlib, sys, tempfile, types
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


@pytest.mark.parametrize("block", LOADERS)
def test_plugin_registers_and_reads_settings_with_only(block):
    """Hermes v0.22+ ships no PyYAML (hermes_yaml); older Hermes ships no hermes_yaml (PyYAML)."""
    r = run_isolated(_REGISTER, block=block)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-3000:]


def test_broken_hermes_yaml_fails_with_its_own_cause_and_no_fallback(tmp_path):
    """A hermes_yaml that is installed but cannot import must not silently switch to PyYAML (other
    semantics), nor be reported as a missing PyYAML."""
    (tmp_path / "hermes_yaml.py").write_text("raise ImportError('hermes_yaml sentinel: ruamel.yaml is broken')\n")
    (tmp_path / "yaml.py").write_text("raise AssertionError('fell back to PyYAML')\n")
    r = run_isolated("import kubernetes_sandbox.settings\n", path=[tmp_path])
    assert r.returncode != 0
    assert "ImportError: hermes_yaml sentinel: ruamel.yaml is broken" in r.stderr, r.stderr[-3000:]
    assert "fell back to PyYAML" not in r.stderr


def test_no_yaml_loader_names_both():
    r = run_isolated("import kubernetes_sandbox.settings\n", block=["hermes_yaml", "yaml"])
    assert r.returncode != 0
    assert "neither is installed" in r.stderr and "hermes_yaml" in r.stderr and "PyYAML" in r.stderr, \
        r.stderr[-3000:]
