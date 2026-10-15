"""Run plugin code in a fresh interpreter with chosen modules made unimportable.

Blocking a module only means something before anything imported it, so the YAML-loader tests run
in a subprocess rather than in the test process (where Hermes, and the plugin, are already loaded).
"""
import importlib.util
import pathlib
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Hermes v0.22+ ships no PyYAML, so the PyYAML-path tests skip in the main run; CI runs them in a
# second step after installing PyYAML (.github/workflows/ci.yml).
needs_pyyaml = pytest.mark.skipif(importlib.util.find_spec("yaml") is None,
                                  reason="PyYAML not installed (the older-Hermes fallback)")

# Loader under test -> the module to block so that the plugin must use it.
LOADERS = [
    pytest.param(["yaml"], id="hermes_yaml"),
    pytest.param(["hermes_yaml"], id="pyyaml", marks=needs_pyyaml),
]


def run_isolated(script: str, *, block=(), path=()) -> subprocess.CompletedProcess:
    """Run `script` with ROOT, then `path`, first on sys.path; every module in `block` is unimportable
    (as if not installed). argv[1] is the plugin root."""
    prelude = "import sys\n" + f"sys.path[:0] = {[str(ROOT), *map(str, path)]!r}\n" + "".join(
        f"sys.modules[{m!r}] = None\n" for m in block)
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(script), str(ROOT)],
                          capture_output=True, text=True, timeout=120)
