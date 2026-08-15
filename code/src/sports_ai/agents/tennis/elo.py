"""T01 reference implementation: player strength via Elo.

This is the pattern every other agent follows: check sample, maybe return
INSUFFICIENT_DATA, compute, then return standard output with evidence.
Surface Elo (T02) can later reuse EloBook with per-surface books blended
by sample.
"""

from __future__ import annotations

import math

from ..base import BaseAgent
from ...models.output import AgentOutput, Status

MIN_MATCHES = 20


class EloBook:
    """Incremental Elo rebuilt chronologically for point-in-time correctness."""

    def __init__(self, k: float = 24.0, base: float = 1500.0):
        self.k = k
        self.base = base
        self.ratings: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def get(self, pid: str) -> float:
        return self.ratings.get(pid, self.base)

    def expect(self, a: str, b: str) -> float:
        return 1.0 / (1.0 + 10 ** ((self.get(b) - self.get(a)) / 400))

    def update(self, a: str, b: str, a_won: bool) -> None:
        ea = self.expect(a, b)
        sa = 1.0 if a_won else 0.0

        # K decays with experience so new players move fast, veterans slowly.
        ka = self.k * (1.5 if self.counts.get(a, 0) < 30 else 1.0)
        kb = self.k * (1.5 if self.counts.get(b, 0) < 30 else 1.0)

        self.ratings[a] = self.get(a) + ka * (sa - ea)
        self.ratings[b] = self.get(b) + kb * ((1 - sa) - (1 - ea))
        self.counts[a] = self.counts.get(a, 0) + 1
        self.counts[b] = self.counts.get(b, 0) + 1


class PlayerStrengthAgent(BaseAgent):
    agent_id = "T01"

    def __init__(self, elo_book: EloBook, **kwargs):
        super().__init__(**kwargs)
        self.book = elo_book

    def analyze(self, payload: dict) -> AgentOutput:
        event_id = payload["event_id"]
        pa = payload["player_a"]["player_id"]
        pb = payload["player_b"]["player_id"]
        na = self.book.counts.get(pa, 0)
        nb = self.book.counts.get(pb, 0)

        if min(na, nb) < MIN_MATCHES:
            return self.insufficient(
                event_id,
                f"rated matches: A={na}, B={nb} < {MIN_MATCHES}",
            )

        p_a = self.book.expect(pa, pb)
        sample_conf = 1 - math.exp(-min(na, nb) / 60)

        return AgentOutput(
            event_id=event_id,
            agent_id=self.agent_id,
            model_version=self.model_version,
            status=Status.OK,
            primary_score=p_a,
            confidence=round(sample_conf, 3),
            data_quality=round(min(1.0, min(na, nb) / 100), 3),
            side_a_advantage=round(2 * (p_a - 0.5), 3),
            side_b_advantage=round(-2 * (p_a - 0.5), 3),
            uncertainty=round(1 - sample_conf, 3),
            key_reasons=[
                (
                    f"Elo A={self.book.get(pa):.0f} ({na} matches) vs "
                    f"B={self.book.get(pb):.0f} ({nb} matches)"
                )
            ],
            details={
                "elo_a": self.book.get(pa),
                "elo_b": self.book.get(pb),
                "p_win_a_elo": p_a,
            },
        )
