from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_SELECTED,
    PortfolioPolicy,
)
from sports_ai.dashboard.services import (
    CLIENT_MATCH_CARD_MARKETS,
    DashboardConfig,
    DashboardServices,
)


def _config(tmp_path: Path) -> DashboardConfig:
    performance_dir = tmp_path / "performance"
    performance_dir.mkdir()
    (performance_dir / "football_performance_summary.json").write_text(
        json.dumps({"generated_at": "2026-08-30T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return DashboardConfig(
        repo_root=Path(__file__).resolve().parents[2],
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=performance_dir,
        operational_data_dir=tmp_path,
        storage_mode="test",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        upcoming_horizon_days=3,
        enable_scheduler=False,
        portfolio_policy_effective_from="",
    )


def _fixture(
    fixture_id: str,
    *,
    home: str = "Swansea",
    away: str = "Watford",
    competition_id: str = "ENG2",
    competition: str = "Championship",
    kickoff_utc: str = "2026-09-01T18:45:00+00:00",
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "provider_fixture_id": fixture_id.split(":", 1)[-1],
        "provider": fixture_id.split(":", 1)[0],
        "competition_id": competition_id,
        "competition_name": competition,
        "division_code": "E1",
        "season": "2026",
        "home_team_id": "home",
        "home_team_name": home,
        "away_team_id": "away",
        "away_team_name": away,
        "kickoff_utc": kickoff_utc,
        "status": "SCHEDULED",
        "elapsed_minute": None,
        "home_score": None,
        "away_score": None,
        "last_provider_update": "2026-09-01T00:00:00+00:00",
        "source_version": "test",
        "provider_payload": {},
        "prediction": {"fixture_id": fixture_id, "prediction_status": "READY"},
    }


def _decision(
    fixture_id: str,
    market_family: str,
    selection: str,
    *,
    line: float | None = None,
    state: str = "BET",
    edge: float | None = 0.08,
    odds: float | None = 2.1,
    paper_trade_id: str | None = None,
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "competition_id": "ENG2",
        "competition_name": "Championship",
        "market_family": market_family,
        "selection": selection,
        "line": line,
        "decision": "PRELIMINARY_CANDIDATE" if state == "BET" else "NO_BET",
        "current_decision_state": state,
        "paper_entry_eligible": state == "BET",
        "model_probability": 0.6,
        "model_fair_decimal_price": 1.66,
        "best_available_price": odds,
        "best_available_bookmaker": "Testbook",
        "best_available_source_timestamp": "2026-09-01T10:00:00+00:00",
        "settlement_aware_edge": edge,
        "market_freshness": "FRESH",
        "day12_state": "PASS" if state == "BET" else "NOT_APPLICABLE",
        "reason_codes": reason_codes or [],
        "paper_trade_id": paper_trade_id,
        "decision_timestamp": "2026-09-01T10:00:00+00:00",
    }


def _service_with_runtime(
    tmp_path: Path,
    fixture: dict[str, Any],
    decision_rows: list[dict[str, Any]],
) -> DashboardServices:
    service = DashboardServices(config=_config(tmp_path))
    service._market_runtime_state["evaluations"] = {
        fixture["fixture_id"]: {
            "fixture_id": fixture["fixture_id"],
            "state": "READY",
            "decision_rows": decision_rows,
            "display_rows": decision_rows,
        }
    }
    return service


def test_client_match_cards_render_exact_five_markets_and_preserve_bet_no_bet(tmp_path: Path) -> None:
    fixture = _fixture("api-football:100")
    rows = [
        _decision(fixture["fixture_id"], "1x2", "AWAY", odds=4.2, paper_trade_id="t1"),
        _decision(fixture["fixture_id"], "btts", "YES", state="NO_BET_VALUE", odds=1.76, edge=0.5),
        _decision(fixture["fixture_id"], "goals_total", "OVER", line=2.0, state="NO_BET_VALUE", odds=1.47),
        _decision(fixture["fixture_id"], "corner_total", "UNDER", line=8.5, odds=2.92, paper_trade_id="t2"),
        _decision(fixture["fixture_id"], "asian_handicap", "AWAY", line=-1.0, odds=8.3, paper_trade_id="t3"),
    ]
    card = _service_with_runtime(tmp_path, fixture, rows)._build_client_match_cards([fixture])[0]

    assert [row["market_family"] for row in card["market_rows"]] == [market for market, _ in CLIENT_MATCH_CARD_MARKETS]
    assert [row["market_label"] for row in card["market_rows"]] == [
        "MATCH WINNER / 1X2",
        "BTTS",
        "GOALS",
        "CORNERS",
        "ASIAN HANDICAP",
    ]
    assert [row["action"] for row in card["market_rows"]] == ["BET", "NO BET", "NO BET", "BET", "BET"]
    assert card["valid_bets_label"] == "3 VALID BETS"
    assert card["kickoff_local_short"] == "Sep 2 · 2:45 AM PHT"


def test_client_match_cards_do_not_requalify_no_bet_from_edge(tmp_path: Path) -> None:
    fixture = _fixture("api-football:101")
    service = _service_with_runtime(
        tmp_path,
        fixture,
        [_decision(fixture["fixture_id"], "1x2", "HOME", state="NO_BET_VALUE", edge=9.99, odds=9.99)],
    )

    row = service._build_client_match_cards([fixture])[0]["market_rows"][0]

    assert row["action"] == "NO BET"
    assert row["reason"] == "Insufficient value"


def test_client_match_cards_collapse_adjacent_lines_and_prefer_portfolio_selection(tmp_path: Path) -> None:
    fixture = _fixture("api-football:102")
    selected = _decision(fixture["fixture_id"], "asian_handicap", "AWAY", line=-1.0, odds=8.3, edge=0.02, paper_trade_id="selected")
    higher_edge = _decision(fixture["fixture_id"], "asian_handicap", "AWAY", line=-0.5, odds=2.2, edge=0.2, paper_trade_id="higher")
    policy = PortfolioPolicy(
        policy_id=PORTFOLIO_POLICY_ID_V1,
        policy_version=PORTFOLIO_POLICY_VERSION_V1,
        effective_from="2026-09-01T00:00:00+00:00",
    )
    context = {
        "policy": policy,
        "publish_note": "active",
        "paper_rows_by_trade_id": {
            "selected": {"paper_trade_id": "selected", "decision_timestamp": selected["decision_timestamp"]},
            "higher": {"paper_trade_id": "higher", "decision_timestamp": higher_edge["decision_timestamp"]},
        },
        "by_trade_id": {
            "selected": {
                "selection_state": PORTFOLIO_STATE_SELECTED,
                "selection_rank": 1,
                "selection_reason": "Selected by test policy.",
                "canonical_fixture_id": "canonical-1",
            },
        },
        "selected_trade_ids": {"selected"},
    }
    service = _service_with_runtime(tmp_path, fixture, [higher_edge, selected])

    card = service._build_client_match_cards([fixture], portfolio_context=context)[0]
    ah_rows = [row for row in card["market_rows"] if row["market_family"] == "asian_handicap"]

    assert len(ah_rows) == 1
    assert ah_rows[0]["bet_label"] == "Watford +1"
    assert ah_rows[0]["portfolio_selected"] is True


def test_client_match_cards_fallback_to_strongest_bet_then_no_bet_reason(tmp_path: Path) -> None:
    fixture = _fixture("api-football:103")
    service = _service_with_runtime(
        tmp_path,
        fixture,
        [
            _decision(fixture["fixture_id"], "goals_total", "OVER", line=2.0, edge=0.01, odds=1.7),
            _decision(fixture["fixture_id"], "goals_total", "OVER", line=2.5, edge=0.08, odds=2.2),
            _decision(
                fixture["fixture_id"],
                "corner_total",
                "UNDER",
                line=8.5,
                state="NO_BET_MISSING_PRICE",
                edge=None,
                odds=None,
                reason_codes=["MISSING_REFERENCE_MARKET"],
            ),
        ],
    )

    card = service._build_client_match_cards([fixture])[0]
    goals = next(row for row in card["market_rows"] if row["market_family"] == "goals_total")
    corners = next(row for row in card["market_rows"] if row["market_family"] == "corner_total")

    assert goals["bet_label"] == "Over 2.5 Goals"
    assert goals["action"] == "BET"
    assert corners["action"] == "NO BET"
    assert corners["reason"] == "Missing Reference Market"


def test_client_match_cards_deduplicate_by_canonical_fixture_and_keep_competitions_distinct(tmp_path: Path) -> None:
    primary = _fixture("api-football:104")
    alias = _fixture("football-data.org:104")
    other_competition = _fixture("api-football:105", competition_id="ESP1", competition="La Liga")
    service = _service_with_runtime(tmp_path, primary, [_decision(primary["fixture_id"], "1x2", "HOME")])
    service._market_runtime_state["evaluations"][alias["fixture_id"]] = {
        "fixture_id": alias["fixture_id"],
        "state": "READY",
        "decision_rows": [_decision(alias["fixture_id"], "1x2", "HOME")],
        "display_rows": [],
    }
    service._market_runtime_state["evaluations"][other_competition["fixture_id"]] = {
        "fixture_id": other_competition["fixture_id"],
        "state": "READY",
        "decision_rows": [_decision(other_competition["fixture_id"], "1x2", "HOME")],
        "display_rows": [],
    }

    cards = service._build_client_match_cards([primary, alias, other_competition])

    assert len(cards) == 2
    assert sorted(card["competition_name"] for card in cards) == ["Championship", "La Liga"]


def test_client_match_cards_read_safety_uses_no_provider_writes_or_schema_init(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture("api-football:106")
    service = _service_with_runtime(tmp_path, fixture, [_decision(fixture["fixture_id"], "1x2", "HOME")])
    service.market_provider.get_market_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider call"))
    calls = {"canonical_schema": 0, "portfolio_schema": 0, "wal": 0}

    def fail_schema(*args, **kwargs):
        calls["canonical_schema"] += 1
        raise AssertionError("schema initialization")

    def fail_portfolio_schema(*args, **kwargs):
        calls["portfolio_schema"] += 1
        raise AssertionError("portfolio schema initialization")

    monkeypatch.setattr("sports_ai.storage.football_canonical_fixture.ensure_schema", fail_schema)
    monkeypatch.setattr("sports_ai.storage.football_portfolio_selection.ensure_schema", fail_portfolio_schema)
    monkeypatch.setattr(service, "_canonical_display_ids_for_fixtures", lambda rows: {})

    service._build_client_match_cards([fixture])

    assert calls == {"canonical_schema": 0, "portfolio_schema": 0, "wal": 0}


def test_dashboard_renders_client_cards_and_detail_link(tmp_path: Path) -> None:
    fixture = _fixture("api-football:107")
    service = _service_with_runtime(tmp_path, fixture, [_decision(fixture["fixture_id"], "1x2", "HOME")])
    service._all_fixtures_with_predictions = lambda: [fixture]
    service.initialize = _async_noop
    service.shutdown = _async_noop
    service.get_system_status = lambda: {
        "fixture_feed": {"state": "READY"},
        "odds_feed": {"state": "READY"},
        "current_time_local": datetime.now(timezone.utc).isoformat(),
    }
    app = create_app(services=service)

    with TestClient(app) as client:
        response = client.get("/today")

    assert response.status_code == 200
    assert "Upcoming Match Recommendations" in response.text
    assert "MATCH WINNER / 1X2" in response.text
    assert "ASIAN HANDICAP" in response.text
    assert 'href="/matches/api-football:107"' in response.text
    assert "client-match-card__teams" in response.text


async def _async_noop() -> None:
    return None
