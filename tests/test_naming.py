import re
from kubernetes_sandbox.naming import Identity, slug

DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def test_pod_name_is_dns1123_deterministic_and_bounded():
    ident = Identity(profile="Mars_Lab.Profile", session_key="telegram:123/thread:9")
    assert ident.pod_name == Identity("Mars_Lab.Profile", "telegram:123/thread:9").pod_name
    assert DNS1123.match(ident.pod_name) and len(ident.pod_name) <= 63
    assert ident.pod_name.startswith("hs-mars-lab-profile-")


def test_different_sessions_differ_and_profile_is_part_of_hash():
    a, b = Identity("p", "s1"), Identity("p", "s2")
    c = Identity("q", "s1")
    assert len({a.pod_name, b.pod_name, c.key_hash, a.key_hash}) == 4


def test_slug_handles_empty_and_long():
    assert slug("") == "default"
    assert len(slug("x" * 200)) <= 40 and DNS1123.match(slug("--A--" + "b" * 100))


def test_workspace_claim_is_pod_name_plus_ws_and_dns1123():
    ident = Identity("x" * 80, "s" * 300)
    assert ident.workspace_claim == ident.pod_name + "-ws"
    assert DNS1123.match(ident.workspace_claim) and len(ident.workspace_claim) <= 63
