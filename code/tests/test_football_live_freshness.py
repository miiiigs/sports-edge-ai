from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE
from sports_ai.dashboard.services import DashboardServices


def _live_row(*, fixture_id: str, kickoff_utc: str, last_provider_update: str) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "kickoff_utc": kickoff_utc,
        "last_provider_update": last_provider_update,
        "status": FIXTURE_STATUS_LIVE,
    }


def test_live_bucket_hides_stale_and_invalid_future_live_rows() -> None:
    now_utc = datetime.now(timezone.utc)
    services = DashboardServices.__new__(DashboardServices)
    services.config = SimpleNamespace(display_timezone="Asia/Manila")

    current = _live_row(
        fixture_id="current",
        kickoff_utc=(now_utc - timedelta(hours=1)).isoformat(),
        last_provider_update=(now_utc - timedelta(minutes=20)).isoformat(),
    )
    stale = _live_row(
        fixture_id="stale",
        kickoff_utc=(now_utc - timedelta(days=3)).isoformat(),
        last_provider_update=(now_utc - timedelta(days=3)).isoformat(),
    )
    invalid_future = _live_row(
        fixture_id="future",
        kickoff_utc=(now_utc + timedelta(days=2)).isoformat(),
        last_provider_update=now_utc.isoformat(),
    )

    rows = services._filter_fixture_rows(
        bucket="live",
        fixture_rows=[current, stale, invalid_future],
    )

    assert [row["fixture_id"] for row in rows] == ["current"]


def test_live_bucket_falls_back_to_kickoff_when_update_timestamp_is_missing() -> None:
    now_utc = datetime.now(timezone.utc)
    services = DashboardServices.__new__(DashboardServices)
    services.config = SimpleNamespace(display_timezone="Asia/Manila")

    recent = _live_row(
        fixture_id="recent-fallback",
        kickoff_utc=(now_utc - timedelta(hours=2)).isoformat(),
        last_provider_update="",
    )
    stale = _live_row(
        fixture_id="stale-fallback",
        kickoff_utc=(now_utc - timedelta(hours=8)).isoformat(),
        last_provider_update="",
    )

    rows = services._filter_fixture_rows(
        bucket="live",
        fixture_rows=[recent, stale],
    )

    assert [row["fixture_id"] for row in rows] == ["recent-fallback"]
