from __future__ import annotations

from sports_ai.dashboard.live_production_services import _summarize_prediction_rows


def _eligibility(prediction):
    if prediction is None:
        return "PREDICTION_PENDING"
    return str(prediction.get("eligibility") or "UNKNOWN")


def test_current_blocker_summary_separates_ready_pending_and_blocked_rows():
    rows = [
        {
            "fixture_id": "ready-1",
            "competition_id": "ENG1",
            "kickoff_utc": "2026-09-14T10:00:00+00:00",
            "prediction": {"eligibility": "PREDICTION_READY", "blockers": []},
        },
        {
            "fixture_id": "pending-1",
            "competition_id": "ENG2",
            "kickoff_utc": "2026-09-14T11:00:00+00:00",
            "prediction": None,
        },
        {
            "fixture_id": "blocked-1",
            "competition_id": "ESP1",
            "kickoff_utc": "2026-09-14T12:00:00+00:00",
            "prediction": {
                "eligibility": "MODEL_STATE_STALE",
                "blockers": ["MODEL_STATE_STALE"],
            },
        },
        {
            "fixture_id": "blocked-2",
            "competition_id": "GER1",
            "kickoff_utc": "2026-09-14T13:00:00+00:00",
            "prediction": {
                "eligibility": "TEAM_IDENTITY_UNRESOLVED",
                "blockers": ["TEAM_IDENTITY_UNRESOLVED", "MODEL_HISTORY_MISSING"],
            },
        },
    ]

    summary = _summarize_prediction_rows(rows, eligibility_resolver=_eligibility)

    assert summary["fixtures"] == 4
    assert summary["ready"] == 1
    assert summary["pending"] == 1
    assert summary["blocked"] == 2
    assert summary["blocker_reasons"] == {
        "MODEL_STATE_STALE": 1,
        "MODEL_HISTORY_MISSING": 1,
        "TEAM_IDENTITY_UNRESOLVED": 1,
    }
    assert [row["fixture_id"] for row in summary["blocked_examples"]] == [
        "blocked-1",
        "blocked-2",
    ]
