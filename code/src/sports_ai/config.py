"""Settings. Each zone gets its own database role/DSN - part of the firewall."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    dsn_blind: str = os.environ.get("SPORTS_AI_DSN_BLIND", "")
    dsn_market: str = os.environ.get("SPORTS_AI_DSN_MARKET", "")
    dsn_decision: str = os.environ.get("SPORTS_AI_DSN_DECISION", "")
    dsn_orchestrator: str = os.environ.get("SPORTS_AI_DSN_ORCH", "")

    # Per-competition data-quality floors (0-100). Backtest-tuned later.
    min_data_quality: dict = field(
        default_factory=lambda: {
            "ATP": 75,
            "CHALLENGER": 85,
            "ENG1": 70,
            "ENG2": 70,
            "ENG3": 72,
            "ENG4": 75,
            "SCO1": 72,
            "SCO2": 75,
            "SCO3": 78,
            "SCO4": 80,
        }
    )

    # V1 decision policy defaults (per competition; versioned in DB as source of truth).
    min_adjusted_ev: dict = field(
        default_factory=lambda: {
            "ATP": 0.03,
            "CHALLENGER": 0.06,
            "ENG1": 0.03,
            "ENG2": 0.035,
            "ENG3": 0.045,
            "ENG4": 0.055,
            "SCO1": 0.04,
            "SCO2": 0.05,
            "SCO3": 0.06,
            "SCO4": 0.07,
        }
    )

    simulation_runs: int = 10_000
    forbidden_decision_words: tuple = ("guaranteed", "certain", "sure thing", "lock")


settings = Settings()
