"""Low-memory Football dashboard entrypoint for the read-only web process.

The production supervisor already isolates web serving from scheduler work. The scheduler
is the only process that needs the full in-memory Football model snapshot in order to
create predictions. The web process serves persisted fixtures, predictions, decisions,
performance and runtime state, so rebuilding the historical model there duplicates a
large amount of memory without improving dashboard output.

This module keeps the existing DashboardServices read APIs and runtime contracts while
substituting a tiny persisted snapshot view in the web process. No prediction, pricing,
provider, settlement, portfolio or model logic is changed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import storage
from .app import create_app
from .services import DashboardServices


@dataclass(frozen=True)
class PersistedSnapshotView:
    """Minimal model metadata needed by read-only dashboard/status paths."""

    snapshot_id: str
    model_as_of: str
    supported_competitions: tuple[str, ...]
    diagnostics: dict[str, Any]
    route_a_artifact: None = None


class LowMemoryWebDashboardServices(DashboardServices):
    """DashboardServices variant that never replays the model in the web process."""

    def _load_persisted_snapshot_view(self) -> None:
        try:
            with storage.connect(self.config.dashboard_db_path) as conn:
                row = conn.execute(
                    """
                    WITH latest_snapshot AS (
                        SELECT model_snapshot_id, model_as_of
                        FROM predictions
                        WHERE model_snapshot_id IS NOT NULL
                          AND TRIM(model_snapshot_id) <> ''
                          AND model_as_of IS NOT NULL
                        ORDER BY generated_at DESC, prediction_id DESC
                        LIMIT 1
                    )
                    SELECT
                        ls.model_snapshot_id,
                        ls.model_as_of,
                        GROUP_CONCAT(DISTINCT f.competition_id) AS competition_ids
                    FROM latest_snapshot ls
                    LEFT JOIN predictions p
                      ON p.model_snapshot_id = ls.model_snapshot_id
                    LEFT JOIN fixtures f
                      ON f.fixture_id = p.fixture_id
                    GROUP BY ls.model_snapshot_id, ls.model_as_of
                    """
                ).fetchone()
        except sqlite3.OperationalError as exc:
            self.snapshot = None
            self.snapshot_error = f"persisted model metadata unavailable: {exc}"
            return

        if row is None or not row["model_snapshot_id"]:
            self.snapshot = None
            self.snapshot_error = "persisted model metadata not yet available from scheduler"
            return

        competition_ids = tuple(
            sorted(
                {
                    value.strip()
                    for value in str(row["competition_ids"] or "").split(",")
                    if value.strip()
                }
            )
        )
        model_as_of = str(row["model_as_of"])
        self.snapshot = PersistedSnapshotView(
            snapshot_id=str(row["model_snapshot_id"]),
            model_as_of=model_as_of,
            supported_competitions=competition_ids,
            diagnostics={
                "model_as_of": model_as_of,
                "source": "PERSISTED_SCHEDULER_EVIDENCE",
                "route_a": {"overall_status": "REJECTED"},
            },
        )
        self.snapshot_error = None

    def _build_snapshot(self) -> None:
        # Deliberately do not call build_current_football_model_snapshot here. The
        # scheduler process remains the sole model owner and persists its outputs.
        self._load_persisted_snapshot_view()

    def _refresh_persisted_runtime_state_for_web(self) -> None:
        super()._refresh_persisted_runtime_state_for_web()
        self._load_persisted_snapshot_view()


app = create_app(services=LowMemoryWebDashboardServices())
