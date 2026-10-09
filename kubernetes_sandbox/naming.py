"""Deterministic sandbox identity: Pod name, labels and annotations."""
from __future__ import annotations

import dataclasses
import hashlib
import re

PREFIX = "hermes-sandbox/"
MANAGED_BY = {"app.kubernetes.io/managed-by": "hermes-kubernetes-sandbox"}
ANN_SESSION_KEY = PREFIX + "session-key"
ANN_LAST_USED = PREFIX + "last-used"
ANN_MAX_LIFETIME = PREFIX + "max-lifetime-seconds"
ANN_CREATED_BY = PREFIX + "created-by"
ANN_WS_RETENTION = PREFIX + "workspace-retention-seconds"
# Set by the janitor on an unmounted workspace PVC; cleared by the plugin on every Pod start.
ANN_UNUSED_SINCE = PREFIX + "unused-since"
LBL_PROFILE = PREFIX + "profile"
LBL_KEY_HASH = PREFIX + "key-hash"
LBL_TEMPLATE_HASH = PREFIX + "template-hash"
# Informational: Pods and workspace PVCs are told apart by API kind, never by this label.
LBL_COMPONENT = "app.kubernetes.io/component"


def slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:max_len].strip("-")
    return s or "default"


@dataclasses.dataclass(frozen=True)
class Identity:
    profile: str
    session_key: str

    @property
    def key_hash(self) -> str:
        return hashlib.sha256(f"{self.profile}\0{self.session_key}".encode()).hexdigest()[:10]

    @property
    def pod_name(self) -> str:
        return f"hs-{slug(self.profile)}-{self.key_hash}"

    @property
    def workspace_claim(self) -> str:
        """The session's workspace PVC; outlives every Pod of the session."""
        return f"{self.pod_name}-ws"
