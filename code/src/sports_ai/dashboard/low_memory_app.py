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

MODEL_SNAPSHOT_STATE_KEY = "model_snapshot_state"


@dataclass(frozen=True)
class PersistedSnapshotView:
    """Minimal model metadata needed by read-only dashboard/status paths."""

    snapshot_id: str
    model_as_of: str
    supported_competitions: tuple[str, ...]
    diagnostics: dict[str, Any]
    model_version: str | None = None
    route_a_artifact: None = None


class LowMemoryWebDashboardServices(DashboardServices):
    """DashboardServices variant that never replays the model in the web process."""

    def _snapshot_from_runtime_state(self, conn: sqlite3.Connection) -> PersistedSnapshotView | None:
        state = storage.get_runtime_state(
            conn,
            MODEL_SNAPSHOT_STATE_KEY,
            initialize_schema=False,
        )
        if not isinstance(state, dict) or not state.get("snapshot_id") or not state.get("model_as_of"):
            return None
        supported = tuple(
            sorted(
                {
                    str(value).strip()
                    for value in state.get("supported_competitions") or []
                    if str(value).strip()
                }
            )
        )
        model_as_of = str(state["model_as_of"])
        return PersistedSnapshotView(
            snapshot_id=str(state["snapshot_id"]),
            model_as_of=model_as_of,
            supported_competitions=supported,
            model_version=None if state.get("model_version") is None else str(state["model_version"]),
            diagnostics={
                "model_as_of": str(state.get("source_match_cutoff") or model_as_of),
                "source": "PERSISTED_SCHEDULER_STATE",
                "route_a": {
                    "overall_status": state.get("route_a_status"),
                    "artifact_id": state.get("route_a_artifact_id"),
                },
            },
        )

    def _snapshot_from_prediction_evidence(self, conn: sqlite3.Connection) -> PersistedSnapshotView | None:
        """Backward-compatible fallback until a scheduler has published runtime metadata."""
        row = conn.execute(
            """
            WITH latest_snapshot AS (
                SELECT model_snapshot_id, model_as_of
                FROM dashboard_predictions
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
            LEFT JOIN dashboard_predictions p
              ON p.model_snapshot_id = ls.model_snapshot_id
            LEFT JOIN dashboard_fixtures f
              ON f.fixture_id = p.fixture_id
            GROUP BY ls.model_snapshot_id, ls.model_as_of
            """
        ).fetchone()
        if row is None or not row["model_snapshot_id"]:
            return None
        supported = tuple(
            sorted(
                {
                    value.strip()
                    for value in str(row["competition_ids"] or "").split(",")
                    if value.strip()
                }
            )
        )
        model_as_of = str(row["model_as_of"])
        return PersistedSnapshotView(
            snapshot_id=str(row["model_snapshot_id"]),
            model_as_of=model_as_of,
            supported_competitions=supported,
            diagnostics={
                "model_as_of": model_as_of,
                "source": "PERSISTED_PREDICTION_EVIDENCE",
                "route_a": {"overall_status": None, "artifact_id": None},
            },
        )

    def _load_persisted_snapshot_view(self) -> None:
        try:
            with storage.connect(self.config.dashboard_db_path) as conn:
                snapshot = self._snapshot_from_runtime_state(conn)
                if snapshot is None:
                    snapshot = self._snapshot_from_prediction_evidence(conn)
        except sqlite3.OperationalError as exc:
            self.snapshot = None
            self.snapshot_error = f"persisted model metadata unavailable: {exc}"
            return

        if snapshot is None:
            self.snapshot = None
            self.snapshot_error = "persisted model metadata not yet available from scheduler"
            return

        self.snapshot = snapshot
        self.snapshot_error = None

    def _build_snapshot(self) -> None:
        # Deliberately do not call build_current_football_model_snapshot here. The
        # scheduler process remains the sole model owner and persists its outputs.
        self._load_persisted_snapshot_view()

    def _refresh_persisted_runtime_state_for_web(self) -> None:
        super()._refresh_persisted_runtime_state_for_web()
        self._load_persisted_snapshot_view()


app = create_app(services=LowMemoryWebDashboardServices())
