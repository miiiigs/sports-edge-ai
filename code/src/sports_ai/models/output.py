"""The standard agent output contract."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Status(str, Enum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


class AgentOutput(BaseModel):
    event_id: str = Field(pattern=r"^evt_")
    agent_id: str = Field(pattern=r"^[FTM](0[1-9]|1[0-9]|20)$")
    model_version: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Status
    primary_score: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    data_quality: float | None = Field(default=None, ge=0, le=1)
    side_a_advantage: float | None = Field(default=None, ge=-1, le=1)
    side_b_advantage: float | None = Field(default=None, ge=-1, le=1)
    key_reasons: list[str] = Field(default_factory=list, max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)
    uncertainty: float | None = Field(default=None, ge=0, le=1)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ok_requires_numbers(self) -> "AgentOutput":
        if self.status == Status.OK:
            missing = [
                field
                for field in ("primary_score", "confidence", "data_quality", "uncertainty")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(f"status=OK requires {missing}")
        return self

    @classmethod
    def insufficient(
        cls,
        event_id: str,
        agent_id: str,
        model_version: str,
        reason: str,
    ) -> "AgentOutput":
        """The always-valid answer. Never fabricate a confident number."""

        return cls(
            event_id=event_id,
            agent_id=agent_id,
            model_version=model_version,
            status=Status.INSUFFICIENT_DATA,
            key_reasons=[reason],
        )
