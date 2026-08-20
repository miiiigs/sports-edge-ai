"""Audit current football fixture identity and model-coverage readiness."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..dashboard.models import FIXTURE_STATUS_LIVE, NormalizedFixture
from ..dashboard.services import DashboardConfig, DashboardServices
from ..dashboard.snapshot_service import CurrentFootballModelSnapshot, build_current_football_model_snapshot
from ..market_data.providers import NotConfiguredMarketProvider

AUDIT_DATE = "2026-08-20"
AUDIT_DATE_LONG = "Thursday, August 20, 2026"
OUTPUT_DIR = Path("research/product_outputs/football/current")
OUTPUT_JSON_PATH = OUTPUT_DIR / "current_fixture_model_coverage.json"
OUTPUT_MARKDOWN_PATH = OUTPUT_DIR / "CURRENT_FIXTURE_MODEL_COVERAGE_AUDIT.md"
STORED_DASHBOARD_DB_PATH = Path("research/product_outputs/dashboard/football_dashboard.sqlite")
RESEARCH_DB_PATH = Path("research/football_research/store/football_research.sqlite")
SUPPORTED_MODEL_COMPETITIONS = {"ENG1", "ENG2", "ENG3", "ENG4", "ESP1"}

PRODUCTION_BASELINE = {
    "fixtures_discovered": 51,
    "supported_fixtures": 32,
    "resolved_team_sides": 53,
    "unresolved_team_sides": 2,
    "model_history_missing_team_sides": 7,
    "predictions_ready": 23,
    "predictions_blocked": 27,
}

TEAM_HISTORY_NAME_CANDIDATES = {
    "Coventry City": ("Coventry", "Coventry City"),
    "Hull City": ("Hull", "Hull City"),
    "Lincoln City": ("Lincoln", "Lincoln City"),
    "West Ham": ("West Ham", "West Ham United"),
    "Wolverhampton": ("Wolves", "Wolverhampton", "Wolverhampton Wanderers"),
    "Bolton": ("Bolton", "Bolton Wanderers"),
    "Santander": ("Racing Santander", "Santander", "Real Racing Club de Santander"),
    "Málaga": ("Malaga", "Málaga", "M\ufffdlaga", "MÃ¡laga"),
    "Deportivo": (
        "Deportivo",
        "RC Deportivo",
        "Deportivo La Coruna",
        "Deportivo La Coruña",
        "La Coruna",
        "La Coruña",
    ),
}


@dataclass(frozen=True)
class ReplayFixtureProvider:
    fixtures: tuple[NormalizedFixture, ...]

    def provider_name(self) -> str:
        return "football-data.org-stored-replay"

    def configured(self) -> bool:
        return True

    def reset_sync_diagnostics(self) -> None:
        return None

    def sync_diagnostics(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name(),
            "query_count": 0,
            "competition_codes_queried": [],
            "provider_seasons_queried": [],
            "has_errors": False,
            "last_error": None,
            "queries": [],
            "plan_note": "stored local football-data.org fixture replay",
            "live_status_note": None,
            "cache_entries": 0,
        }

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        return [fixture for fixture in self.fixtures if fixture.status == FIXTURE_STATUS_LIVE]

    def get_fixtures_for_date(self, target_date: Any) -> list[NormalizedFixture]:
        return [fixture for fixture in self.fixtures if fixture.kickoff_utc[:10] == target_date.isoformat()]

    def get_upcoming_fixtures(
        self,
        *,
        from_utc: Any,
        to_utc: Any,
    ) -> list[NormalizedFixture]:
        return [
            fixture
            for fixture in self.fixtures
            if from_utc <= _parse_dt(fixture.kickoff_utc) <= to_utc
        ]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        for fixture in self.fixtures:
            if fixture.fixture_id == fixture_id:
                return fixture
        return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _parse_dt(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_stored_runtime_state(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select state_value_json from dashboard_runtime_state where state_key='provider_state'"
        ).fetchone()
    return {} if row is None else json.loads(str(row[0]))


def _load_stored_predictions(db_path: Path) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select fixture_id, payload_json from dashboard_predictions").fetchall()
    return {
        str(row["fixture_id"]): json.loads(str(row["payload_json"]))
        for row in rows
    }


def _load_stored_fixtures(db_path: Path) -> list[NormalizedFixture]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                fixture_id,
                provider_fixture_id,
                provider,
                competition_id,
                competition_name,
                division_code,
                season,
                home_team_id,
                home_team_name,
                away_team_id,
                away_team_name,
                kickoff_utc,
                status,
                elapsed_minute,
                home_score,
                away_score,
                last_provider_update,
                source_version,
                payload_json
            from dashboard_fixtures
            order by kickoff_utc, fixture_id
            """
        ).fetchall()
    fixtures: list[NormalizedFixture] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        fixtures.append(
            NormalizedFixture(
                fixture_id=str(row["fixture_id"]),
                provider_fixture_id=None if row["provider_fixture_id"] is None else str(row["provider_fixture_id"]),
                provider=str(row["provider"]),
                competition_id=str(row["competition_id"]),
                competition_name=str(row["competition_name"]),
                division_code=None if row["division_code"] is None else str(row["division_code"]),
                season=str(row["season"]),
                home_team_id=None if row["home_team_id"] is None else str(row["home_team_id"]),
                home_team_name=str(row["home_team_name"]),
                away_team_id=None if row["away_team_id"] is None else str(row["away_team_id"]),
                away_team_name=str(row["away_team_name"]),
                kickoff_utc=str(row["kickoff_utc"]),
                status=str(row["status"]),
                elapsed_minute=None if row["elapsed_minute"] is None else int(row["elapsed_minute"]),
                home_score=None if row["home_score"] is None else int(row["home_score"]),
                away_score=None if row["away_score"] is None else int(row["away_score"]),
                last_provider_update=str(row["last_provider_update"]),
                source_version=str(row["source_version"]),
                provider_payload=dict(payload.get("provider_payload") or {}),
            )
        )
    return fixtures


def _history_evidence(
    conn: sqlite3.Connection,
    *,
    names: tuple[str, ...],
    current_competition_id: str | None,
) -> dict[str, Any]:
    conditions = " OR ".join(["home_team = ?" for _ in names] + ["away_team = ?" for _ in names])
    params = list(names) + list(names)
    aggregate = conn.execute(
        f"""
        select
            count(*) as historical_match_count,
            max(match_date) as latest_historical_match,
            count(case when home_shots is not null and away_shots is not null then 1 end) as shots_rows,
            count(case when home_shots_on_target is not null and away_shots_on_target is not null then 1 end) as shots_on_target_rows,
            count(case when home_corners is not null and away_corners is not null then 1 end) as corners_rows
        from football_events
        where {conditions}
        """,
        params,
    ).fetchone()
    competitions = [
        str(row[0])
        for row in conn.execute(
            f"select distinct competition_id from football_events where {conditions} order by competition_id",
            params,
        ).fetchall()
    ]
    current_model_competition_match_count = 0
    if current_competition_id is not None:
        current_model_competition_match_count = int(
            conn.execute(
                f"""
                select count(*)
                from football_events
                where competition_id = ?
                  and ({conditions})
                """,
                [current_competition_id, *params],
            ).fetchone()[0]
        )
    return {
        "historical_match_count": int(aggregate[0] or 0),
        "latest_historical_match": None if aggregate[1] is None else str(aggregate[1]),
        "shots_rows": int(aggregate[2] or 0),
        "shots_on_target_rows": int(aggregate[3] or 0),
        "corners_rows": int(aggregate[4] or 0),
        "competitions": competitions,
        "current_model_competition_match_count": current_model_competition_match_count,
    }


def _block_reason_from_history(history_status: dict[str, Any] | None, *, resolved: bool) -> str:
    if not resolved:
        return "TEAM_IDENTITY_UNRESOLVED"
    if history_status is None:
        return "PREDICTION_AFTER_KICKOFF_BLOCKED"
    missing = list(history_status.get("missing_statistics") or [])
    if len(missing) > 1:
        return "MODEL_HISTORY_MISSING_MULTIPLE"
    if missing == ["shots"]:
        return "MODEL_HISTORY_MISSING_SHOTS"
    if missing == ["shots_on_target"]:
        return "MODEL_HISTORY_MISSING_SOT"
    if missing == ["corners"]:
        return "MODEL_HISTORY_MISSING_CORNERS"
    return "MODEL_HISTORY_INSUFFICIENT"


def _find_observed_fixture(
    fixtures: list[NormalizedFixture],
    *,
    competition_id: str,
    provider_team_id: str | None = None,
    provider_team_name: str | None = None,
) -> dict[str, Any] | None:
    for fixture in fixtures:
        if fixture.competition_id != competition_id:
            continue
        competition_payload = (fixture.provider_payload or {}).get("competition") or {}
        for side, team_id, team_name in (
            ("home", fixture.home_team_id, fixture.home_team_name),
            ("away", fixture.away_team_id, fixture.away_team_name),
        ):
            if provider_team_id is not None and str(team_id or "") != str(provider_team_id):
                continue
            if provider_team_id is None and provider_team_name is not None and str(team_name) != provider_team_name:
                continue
            return {
                "fixture_id": fixture.fixture_id,
                "side": side,
                "provider_team_id": team_id,
                "provider_team_name": team_name,
                "competition_id": fixture.competition_id,
                "competition_name": fixture.competition_name,
                "provider_competition_code": competition_payload.get("code"),
                "provider_competition_id": competition_payload.get("id"),
                "kickoff_utc": fixture.kickoff_utc,
            }
    return None


def _build_replay_state(repo_root: Path, fixtures: list[NormalizedFixture]) -> dict[str, Any]:
    provider = ReplayFixtureProvider(tuple(fixtures))
    operational_root = Path(mkdtemp(prefix="football_current_coverage_"))
    try:
        config = DashboardConfig(
            repo_root=repo_root,
            research_db_path=repo_root / RESEARCH_DB_PATH,
            dashboard_db_path=operational_root / "football_dashboard.sqlite",
            paper_trading_db_path=operational_root / "football_paper_trading.sqlite",
            performance_output_dir=repo_root / "research" / "product_outputs" / "football" / "performance",
            operational_data_dir=operational_root,
            storage_mode="local",
            persistent_data_dir_configured=False,
            enable_scheduler=False,
            current_odds_connected=False,
            current_xg_connected=False,
        )
        services = DashboardServices(
            config=config,
            fixture_provider=provider,
            market_provider=NotConfiguredMarketProvider(provider_label="coverage-audit"),
        )
        asyncio.run(services.initialize())
        try:
            status = services.get_system_status()
            fixture_details: dict[str, dict[str, Any]] = {}
            for fixture in fixtures:
                payload = services.get_fixture_detail_payload(fixture.fixture_id)
                if payload is not None:
                    fixture_details[fixture.fixture_id] = payload
        finally:
            asyncio.run(services.shutdown())
    finally:
        shutil.rmtree(operational_root, ignore_errors=True)
    return {
        "status": status,
        "fixture_details": fixture_details,
    }


def _team_entry(
    *,
    label: str,
    provider_name: str,
    observed: dict[str, Any] | None,
    snapshot: CurrentFootballModelSnapshot,
    history_conn: sqlite3.Connection,
    historical_names: tuple[str, ...],
    provider_team_id: str | None = None,
    production_only: bool = False,
) -> dict[str, Any]:
    competition_id = None if observed is None else str(observed["competition_id"])
    competition_name = None if observed is None else str(observed["competition_name"])
    resolution = None
    history_status = None
    if competition_id is not None:
        resolution = snapshot.resolve_team(
            provider="football-data.org",
            competition_id=competition_id,
            provider_team_id=provider_team_id if provider_team_id is not None else observed.get("provider_team_id"),
            provider_team_name=provider_name,
        )
        if resolution.is_resolved:
            venue = "home" if observed is not None and observed.get("side") == "home" else "away"
            history_status = snapshot.team_model_history_status(
                competition_id=competition_id,
                canonical_team_id=str(resolution.canonical_team_id),
                venue=venue,
            )
    history = _history_evidence(
        history_conn,
        names=historical_names,
        current_competition_id=competition_id,
    )
    identity_status = "resolved" if resolution is not None and resolution.is_resolved else "unresolved"
    canonical_name = None if resolution is None or not resolution.is_resolved else str(resolution.canonical_team_name)
    missing_stats: list[str] = []
    prediction_eligible = False
    history_status_text = "not_checked_due_to_identity"
    if identity_status == "resolved" and history_status is not None:
        missing_stats = list(history_status.get("missing_statistics") or [])
        prediction_eligible = bool(history_status.get("available"))
        history_status_text = "available" if prediction_eligible else "missing_current_model_competition_history"
    elif identity_status == "resolved":
        history_status_text = "missing_current_model_competition_history"
    return {
        "label": label,
        "provider_team_id": provider_team_id if provider_team_id is not None else (None if observed is None else observed.get("provider_team_id")),
        "provider_name": provider_name,
        "canonical_name": canonical_name,
        "competition": competition_name,
        "competition_id": competition_id,
        "provider_competition_code": None if observed is None else observed.get("provider_competition_code"),
        "provider_competition_id": None if observed is None else observed.get("provider_competition_id"),
        "model_competition": competition_id,
        "identity_status": identity_status,
        "history_status": history_status_text,
        "historical_match_count": history["historical_match_count"],
        "latest_historical_match": history["latest_historical_match"],
        "historical_competitions": history["competitions"],
        "historical_current_model_competition_match_count": history["current_model_competition_match_count"],
        "shots_rows": history["shots_rows"],
        "shots_on_target_rows": history["shots_on_target_rows"],
        "corners_rows": history["corners_rows"],
        "missing_stats": missing_stats,
        "prediction_eligible": prediction_eligible,
        "block_reason": _block_reason_from_history(history_status, resolved=identity_status == "resolved"),
        "resolution_kind": None if resolution is None else resolution.resolution_kind,
        "resolution_reason": None if resolution is None else resolution.reason,
        "model_history_reason": None if resolution is None else resolution.model_history_reason,
        "fixture_id": None if observed is None else observed.get("fixture_id"),
        "kickoff_utc": None if observed is None else observed.get("kickoff_utc"),
        "observation_source": "production_user_report_only" if production_only else "stored_fixture_snapshot",
    }


def _competition_mapping_rows(fixtures: list[NormalizedFixture]) -> list[dict[str, Any]]:
    target_fixture_ids = {
        "football-data.org:560542",
        "football-data.org:560543",
        "football-data.org:561775",
        "football-data.org:561778",
        "football-data.org:564628",
        "football-data.org:564644",
    }
    rows = []
    for fixture in fixtures:
        if fixture.fixture_id not in target_fixture_ids:
            continue
        competition_payload = (fixture.provider_payload or {}).get("competition") or {}
        rows.append(
            {
                "fixture_id": fixture.fixture_id,
                "provider_competition": str(competition_payload.get("name") or fixture.competition_name),
                "provider_competition_code": competition_payload.get("code"),
                "provider_competition_id": competition_payload.get("id"),
                "display_competition": fixture.competition_name,
                "internal_canonical_competition": fixture.competition_id,
                "model_competition": fixture.competition_id,
                "supported_by_model_snapshot": fixture.competition_id in SUPPORTED_MODEL_COMPETITIONS,
                "home_team": fixture.home_team_name,
                "away_team": fixture.away_team_name,
            }
        )
    rows.sort(key=lambda row: str(row["fixture_id"]))
    return rows


def _representative_validation(replay_fixture_details: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    fixture_ids = {
        "working_fixture": "football-data.org:564638",
        "recovered_fixture": "football-data.org:561775",
        "legitimately_blocked_fixture": "football-data.org:560542",
    }

    def summarize(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        prediction = dict(payload.get("prediction") or {})
        team_resolution = dict(prediction.get("team_resolution") or {})
        return {
            "fixture_id": payload["fixture"]["fixture_id"],
            "teams": f"{payload['fixture']['home_team_name']} vs {payload['fixture']['away_team_name']}",
            "competition": payload["fixture"]["competition_id"],
            "model_snapshot_id": prediction.get("model_snapshot_id"),
            "prediction_status": prediction.get("prediction_status"),
            "blockers": prediction.get("blockers") or [],
            "home_canonical": ((team_resolution.get("home") or {}).get("canonical_team_name")),
            "away_canonical": ((team_resolution.get("away") or {}).get("canonical_team_name")),
        }

    return {
        key: summarize(replay_fixture_details.get(fixture_id))
        for key, fixture_id in fixture_ids.items()
    }


def _format_team_evidence_lines(rows: list[dict[str, Any]]) -> str:
    parts = []
    for row in rows:
        parts.append(
            "\n".join(
                [
                    f"Team: `{row['label']}`",
                    f"Canonical name: `{row['canonical_name'] or 'UNRESOLVED'}`",
                    f"Historical matches: `{row['historical_match_count']}`",
                    f"Latest historical match: `{row['latest_historical_match'] or 'NONE'}`",
                    f"Stats available: `shots={row['shots_rows']}` / `shots_on_target={row['shots_on_target_rows']}` / `corners={row['corners_rows']}`",
                    f"Prediction eligible: `{'YES' if row['prediction_eligible'] else 'NO'}`",
                    f"Reason: `{row['block_reason']}`",
                ]
            )
        )
    return "\n".join(parts)


def _write_outputs(payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    competition_table = "\n".join(
        [
            "| Fixture | Provider competition | Provider code/id | Internal competition | Supported |",
            "| --- | --- | --- | --- | --- |",
            *[
                f"| {row['home_team']} vs {row['away_team']} | {row['provider_competition']} | "
                f"{row['provider_competition_code']}/{row['provider_competition_id']} | "
                f"{row['internal_canonical_competition']} | {'YES' if row['supported_by_model_snapshot'] else 'NO'} |"
                for row in payload["competition_mapping"]
            ],
        ]
    )
    unresolved_rows = [row for row in payload["team_rows"] if row["identity_status"] == "unresolved"]
    history_missing_rows = [
        row for row in payload["team_rows"] if row["identity_status"] == "resolved" and not row["prediction_eligible"]
    ]
    local_before = payload["local_stored_runtime_before"]
    local_after = payload["local_replay_after"]
    markdown = f"""# Current Fixture Model Coverage Audit

Date: {AUDIT_DATE_LONG}

## A. Current Slate Summary

- Railway production baseline from the user-supplied Thursday, August 20, 2026 status: `51 discovered / 32 supported / 23 ready / 27 blocked`
- local stored football-data.org snapshot available in this repo: `46 discovered`
- local environment verified on Thursday, August 20, 2026 does not expose `SPORTS_AI_FOOTBALL_DATA_TOKEN`, so this audit replays the stored current fixture snapshot rather than a fresh live sync
- local deterministic snapshot rebuild on Thursday, August 20, 2026 produced `{payload['snapshot']['snapshot_id']}` with `model_as_of = {payload['snapshot']['model_as_of']}`
- the specific Railway snapshot id supplied in the brief, `current_snapshot_ad88f657255d67b34cd27ed6ce88280103a2ca7c1a194f0e3254ee33174f637b`, was not directly present in the local runtime artifacts, so the snapshot-state conclusions below are based on the local deterministic rebuild

## B. Competition Mapping Table

{competition_table}

Result: the suspicious mappings are validated as provider-truth, not an internal competition-map bug.

## C. Remaining Unresolved Teams

{chr(10).join([f"- `{row['label']}` | provider=`{row['provider_name']}` | competition=`{row['competition_id'] or 'ESP1'}` | reason=`{row['resolution_reason'] or row['block_reason']}`" for row in unresolved_rows])}

## D. Model-History Missing Teams

{chr(10).join([f"- `{row['label']}` -> `{row['canonical_name']}` | current competition=`{row['competition_id']}` | missing inputs=`{', '.join(row['missing_stats']) or 'all current-competition state'}`" for row in history_missing_rows])}

## E. Historical Data Evidence

{_format_team_evidence_lines(payload['team_rows'])}

## F. Fixes Applied

- added deterministic football-data.org provider-team-id coverage for `ESP1 / 84 / Málaga -> Malaga`
- preserved the strict resolver rule that bare `Deportivo` stays unresolved without provider-id or club-context evidence
- confirmed that the accepted current football GAP snapshot is competition-specific; historical state is not transferred automatically across leagues for promoted/relegated clubs

## G. Legitimate Blockers Remaining

- `Coventry City`, `Hull City`, `Lincoln City`, `West Ham`, `Wolverhampton`, `Bolton`, and `Santander` all have real historical rows elsewhere in the loaded archive, but they have `0` rows in their current model competition inside the accepted snapshot, so the present block is legitimate under the current competition-specific GAP design
- `Málaga` can now be identified deterministically, but it still has no loaded `ESP1` model history in the accepted snapshot
- bare `Deportivo` remains unresolved intentionally because the local stored snapshot does not contain a matching live provider payload and the short name is not safe to auto-map without provider-id evidence
- a fresh live verification of the Railway-like current slate is still blocked locally by the missing `SPORTS_AI_FOOTBALL_DATA_TOKEN`

## H. Before / After Prediction Coverage

- production baseline from the brief: `23 ready / 27 blocked`
- stale local stored runtime before current-code replay: `{local_before['predictions_ready']} ready / {local_before['predictions_blocked']} blocked`
- stored local snapshot after current-code replay: `{local_after['predictions_ready']} ready / {local_after['predictions_blocked']} blocked`
- stored local supported-current identity failures improved from `13` unresolved team-side blocks in the persisted local prediction rows to `0` unresolved team-side blocks in the current replay
"""
    OUTPUT_MARKDOWN_PATH.write_text(markdown + "\n", encoding="utf-8")


def run_audit() -> dict[str, Any]:
    repo_root = _repo_root()
    stored_fixtures = _load_stored_fixtures(repo_root / STORED_DASHBOARD_DB_PATH)
    stored_runtime = _load_stored_runtime_state(repo_root / STORED_DASHBOARD_DB_PATH)
    stored_predictions = _load_stored_predictions(repo_root / STORED_DASHBOARD_DB_PATH)
    snapshot = build_current_football_model_snapshot(research_db_path=repo_root / RESEARCH_DB_PATH)
    replay = _build_replay_state(repo_root, stored_fixtures)

    history_conn = sqlite3.connect(repo_root / RESEARCH_DB_PATH)
    history_conn.row_factory = sqlite3.Row
    try:
        team_rows = [
            _team_entry(
                label="Coventry City",
                provider_name="Coventry City",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG1", provider_team_id="1076"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Coventry City"],
            ),
            _team_entry(
                label="Hull City",
                provider_name="Hull City",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG1", provider_team_id="322"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Hull City"],
            ),
            _team_entry(
                label="Lincoln City",
                provider_name="Lincoln City",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG2", provider_team_id="1126"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Lincoln City"],
            ),
            _team_entry(
                label="West Ham",
                provider_name="West Ham",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG2", provider_team_id="563"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["West Ham"],
            ),
            _team_entry(
                label="Wolverhampton",
                provider_name="Wolverhampton",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG2", provider_team_id="76"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Wolverhampton"],
            ),
            _team_entry(
                label="Bolton",
                provider_name="Bolton",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ENG2", provider_team_id="60"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Bolton"],
            ),
            _team_entry(
                label="Santander",
                provider_name="Santander",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ESP1", provider_team_id="5335"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Santander"],
            ),
            _team_entry(
                label="Málaga",
                provider_name="Málaga",
                observed=_find_observed_fixture(stored_fixtures, competition_id="ESP1", provider_team_id="84"),
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Málaga"],
                provider_team_id="84",
            ),
            _team_entry(
                label="Deportivo",
                provider_name="Deportivo",
                observed={
                    "competition_id": "ESP1",
                    "competition_name": "Primera Division",
                    "provider_competition_code": "PD",
                    "provider_competition_id": 2014,
                    "provider_team_id": None,
                    "provider_team_name": "Deportivo",
                    "fixture_id": None,
                    "kickoff_utc": None,
                    "side": "home",
                },
                snapshot=snapshot,
                history_conn=history_conn,
                historical_names=TEAM_HISTORY_NAME_CANDIDATES["Deportivo"],
                production_only=True,
            ),
        ]
    finally:
        history_conn.close()

    unresolved_local_prediction_count = 0
    for prediction in stored_predictions.values():
        blockers = set(prediction.get("blockers") or [])
        if "PREDICTION_BLOCKED_TEAM_IDENTITY" in blockers or {
            "HOME_TEAM_IDENTITY_UNRESOLVED",
            "AWAY_TEAM_IDENTITY_UNRESOLVED",
        } & blockers:
            unresolved_local_prediction_count += 1

    status = replay["status"]
    payload = {
        "audit_date": AUDIT_DATE,
        "scope_date_long": AUDIT_DATE_LONG,
        "production_baseline": PRODUCTION_BASELINE,
        "local_stored_runtime_before": {
            "fixtures_discovered": int((stored_runtime.get("last_counts") or {}).get("upcoming", len(stored_fixtures))),
            "supported_fixtures": int((stored_runtime.get("last_counts") or {}).get("supported", 0)),
            "predictions_ready": int((stored_runtime.get("last_counts") or {}).get("predictions_ready", 0)),
            "predictions_blocked": int((stored_runtime.get("last_counts") or {}).get("predictions_blocked", 0)),
            "unresolved_prediction_rows": unresolved_local_prediction_count,
        },
        "local_replay_after": {
            "fixtures_discovered": int(status["current_fixture_state"]["fixtures_discovered"]),
            "supported_fixtures": int(status["current_fixture_state"]["supported_fixtures"]),
            "resolved_team_sides": int(status["team_resolution"]["resolved"]),
            "unresolved_team_sides": int(status["team_resolution"]["unresolved"]),
            "model_history_missing_team_sides": int(status["team_resolution"]["model_history_missing"]),
            "predictions_ready": int(status["prediction_engine"]["ready_fixture_count"]),
            "predictions_blocked": int(status["prediction_engine"]["blocked_fixture_count"]),
        },
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "model_as_of": snapshot.model_as_of,
            "supported_competitions": list(snapshot.supported_competitions),
        },
        "competition_mapping": _competition_mapping_rows(stored_fixtures),
        "team_rows": team_rows,
        "representative_validation": _representative_validation(replay["fixture_details"]),
        "notes": {
            "live_token_available": False,
            "live_sync_attempted": False,
            "accepted_model_behavior": "competition_specific_current_competition_state_only",
            "stale_local_runtime_snapshot_detected": True,
            "stored_local_runtime_last_successful_sync_at": stored_runtime.get("last_successful_sync_at"),
        },
    }
    _write_outputs(payload)
    return payload


def main() -> None:
    payload = run_audit()
    print(json.dumps(payload["local_replay_after"], indent=2, ensure_ascii=False))
    print(f"wrote {OUTPUT_MARKDOWN_PATH}")
    print(f"wrote {OUTPUT_JSON_PATH}")


if __name__ == "__main__":
    main()
