import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("janitor", pathlib.Path(__file__).parents[1] / "janitor/janitor.py")
janitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(janitor)

NOW = 1_000_000.0


def pod(last_used_age=0, created_age=0, phase="Running", max_life=86400):
    iso = janitor.iso
    return {"metadata": {"name": "p", "creationTimestamp": iso(NOW - created_age),
                         "annotations": {"hermes-sandbox/last-used": iso(NOW - last_used_age),
                                         "hermes-sandbox/max-lifetime-seconds": str(max_life)}},
            "status": {"phase": phase}}


def test_keeps_fresh_pod():
    assert janitor.decide(pod(10, 10), NOW, 3600) is None


def test_idle_pod_reaped():
    assert janitor.decide(pod(4000, 4000), NOW, 3600) == "idle"


def test_max_lifetime_beats_activity():
    assert janitor.decide(pod(5, 90000), NOW, 3600) == "max-lifetime"


def test_finished_pods_reaped():
    assert janitor.decide(pod(phase="Failed"), NOW, 3600) == "finished"


def test_missing_annotations_fall_back_to_creation_time():
    p = pod(created_age=5000)
    p["metadata"]["annotations"] = {}
    assert janitor.decide(p, NOW, 3600) == "idle"
