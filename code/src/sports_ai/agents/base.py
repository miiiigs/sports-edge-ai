"""BaseAgent + registry backed by manifest/agents_manifest.json.

Every concrete agent subclasses BaseAgent, declares its agent_id, and
implements analyze(). The base class enforces:
- the contract exists in the manifest
- Zone A agents get firewall-scanned payloads
- outputs validate against the standard AgentOutput model
"""

from __future__ import annotations

import json
import os
import pathlib
from abc import ABC, abstractmethod

from ..firewall import scan_for_market_contamination
from ..models.output import AgentOutput, Status

_MANIFEST_PATH = pathlib.Path(
    os.environ.get(
        "SPORTS_AI_MANIFEST",
        pathlib.Path(__file__).resolve().parents[4] / "manifest" / "agents_manifest.json",
    )
)


def load_manifest() -> dict[str, dict]:
    with open(_MANIFEST_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    return {agent["agent_id"]: agent for agent in data["agents"]}


class BaseAgent(ABC):
    agent_id: str = ""
    model_version: str = "1.0.0"

    def __init__(self, manifest: dict[str, dict] | None = None):
        manifest = manifest or load_manifest()
        if self.agent_id not in manifest:
            raise KeyError(f"agent {self.agent_id} not in manifest")
        self.contract = manifest[self.agent_id]
        self.zone = self.contract["zone"]

    def run(self, payload: dict) -> AgentOutput:
        if self.zone == "A_BLIND":
            # Defense in depth: refuse contaminated inputs even if the
            # payload builder was bypassed.
            scan_for_market_contamination(payload)
        try:
            out = self.analyze(payload)
        except Exception as exc:  # pragma: no cover - defensive wrapper
            out = AgentOutput(
                event_id=payload.get("event_id", "evt_unknown"),
                agent_id=self.agent_id,
                model_version=self.model_version,
                status=Status.ERROR,
                key_reasons=[f"{type(exc).__name__}: {exc}"][:1],
            )
        assert out.agent_id == self.agent_id
        return out

    @abstractmethod
    def analyze(self, payload: dict) -> AgentOutput:
        ...

    def insufficient(self, event_id: str, reason: str) -> AgentOutput:
        return AgentOutput.insufficient(event_id, self.agent_id, self.model_version, reason)
