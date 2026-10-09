import os
import pytest


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in list(os.environ):
        if key.startswith("HERMES_K8S_"):
            monkeypatch.delenv(key, raising=False)
    yield home


@pytest.fixture(autouse=True)
def _fresh_served_pods():
    from kubernetes_sandbox import environment
    environment._SERVED_PODS.clear()
    environment._ACCESS_WARNED.clear()
    yield
    environment._SERVED_PODS.clear()
    environment._ACCESS_WARNED.clear()
