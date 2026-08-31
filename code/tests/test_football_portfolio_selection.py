from __future__ import annotations

import sqlite3

from sports_ai.dashboard.portfolio_selection import (
    PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT,
    PORTFOLIO_STATE_NOT_SELECTED_MARKET_FAMILY_LIMIT,
    PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER,
    PORTFOLIO_STATE_SELECTED,
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PortfolioCandidate,
    PortfolioPolicy,
    select_fixture_portfolio,
)
from sports_ai.storage import football_portfolio_selection
from sports_ai.storage.football_paper_trading import connect


def _policy() -> PortfolioPolicy:
    return PortfolioPolicy(
        policy_id=PORTFOLIO_POLICY_ID_V1,
        policy_version=PORTFOLIO_POLICY_VERSION_V1,
        effective_from="2026-08-01T00:00:00+00:00",
    )


def _candidate(
    paper_trade_id: str,
    *,
    market: str,
    selection: str,
    line: float | None,
    edge: float,
    fixture_id: str = "cf_1",
    decision_timestamp: str = "2026-08-20T10:00:00+00:00",
) -> PortfolioCandidate:
    return PortfolioCandidate(
        paper_trade_id=paper_trade_id,
        canonical_fixture_id=fixture_id,
        market=market,
        selection=selection,
        line=line,
        settlement_aware_edge=edge,
        decision_timestamp=decision_timestamp,
        created_at=decision_timestamp,
        safeguard_state="PASSED",
        freshness_state="FRESH",
    )


def _candidate_payloads(candidates: list[PortfolioCandidate]) -> list[dict[str, object]]:
    return [
        {
            "paper_trade_id": candidate.paper_trade_id,
            "market": candidate.market,
            "selection": candidate.selection,
            "line": candidate.line,
            "edge_at_selection": candidate.settlement_aware_edge,
            "decision_timestamp": candidate.decision_timestamp,
            "created_at": candidate.created_at,
            "safeguard_state": candidate.safeguard_state,
        }
        for candidate in candidates
    ]


def _record_evaluation(
    conn,
    *,
    policy: PortfolioPolicy,
    canonical_fixture_id: str,
    evaluated_at: str,
    candidate_snapshot_at: str,
    candidates: list[PortfolioCandidate],
):
    result = select_fixture_portfolio(candidates, policy=policy)
    signature = football_portfolio_selection.portfolio_evaluation_signature(
        policy=policy,
        canonical_fixture_id=canonical_fixture_id,
        candidates=_candidate_payloads(candidates),
    )
    return football_portfolio_selection.record_portfolio_evaluation(
        conn,
        policy=policy,
        canonical_fixture_id=canonical_fixture_id,
        evaluated_at=evaluated_at,
        candidate_snapshot_at=candidate_snapshot_at,
        evaluation_signature=signature,
        dispositions=list(result.dispositions),
    )


def _evaluation_history(conn) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                evaluation_id,
                predecessor_evaluation_id,
                evaluated_at,
                candidate_snapshot_at,
                candidate_count,
                selected_count,
                evaluation_signature
            FROM football_portfolio_evaluations
            ORDER BY evaluated_at, created_at, evaluation_id
            """
        ).fetchall()
    ]


def test_adjacent_goals_lines_select_one_highest_edge() -> None:
    result = select_fixture_portfolio(
        [
            _candidate("paper_goals_225", market="goals_total", selection="OVER", line=2.25, edge=0.09),
            _candidate("paper_goals_250", market="goals_total", selection="OVER", line=2.5, edge=0.08),
            _candidate("paper_goals_275", market="goals_total", selection="OVER", line=2.75, edge=0.05),
        ],
        policy=_policy(),
    )

    by_trade_id = {row.paper_trade_id: row for row in result.dispositions}
    assert by_trade_id["paper_goals_225"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_goals_250"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER
    assert by_trade_id["paper_goals_275"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER


def test_adjacent_corner_lines_select_one_highest_edge() -> None:
    result = select_fixture_portfolio(
        [
            _candidate("paper_corners_95", market="corner_total", selection="OVER", line=9.5, edge=0.04),
            _candidate("paper_corners_105", market="corner_total", selection="OVER", line=10.5, edge=0.07),
            _candidate("paper_corners_115", market="corner_total", selection="OVER", line=11.5, edge=0.03),
        ],
        policy=_policy(),
    )

    by_trade_id = {row.paper_trade_id: row for row in result.dispositions}
    assert by_trade_id["paper_corners_105"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_corners_95"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER
    assert by_trade_id["paper_corners_115"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER


def test_adjacent_asian_handicap_lines_select_one_highest_edge() -> None:
    result = select_fixture_portfolio(
        [
            _candidate("paper_ah_025", market="asian_handicap", selection="HOME", line=-0.25, edge=0.04),
            _candidate("paper_ah_050", market="asian_handicap", selection="HOME", line=-0.5, edge=0.07),
            _candidate("paper_ah_075", market="asian_handicap", selection="HOME", line=-0.75, edge=0.06),
        ],
        policy=_policy(),
    )

    by_trade_id = {row.paper_trade_id: row for row in result.dispositions}
    assert by_trade_id["paper_ah_050"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_ah_025"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER
    assert by_trade_id["paper_ah_075"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER


def test_market_family_limit_selects_one_side_only() -> None:
    result = select_fixture_portfolio(
        [
            _candidate("paper_over", market="goals_total", selection="OVER", line=2.5, edge=0.08),
            _candidate("paper_under", market="goals_total", selection="UNDER", line=3.5, edge=0.10),
        ],
        policy=_policy(),
    )

    by_trade_id = {row.paper_trade_id: row for row in result.dispositions}
    assert by_trade_id["paper_under"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_over"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_MARKET_FAMILY_LIMIT


def test_fixture_limit_caps_selection_at_three() -> None:
    result = select_fixture_portfolio(
        [
            _candidate("paper_goals", market="goals_total", selection="OVER", line=2.5, edge=0.09),
            _candidate("paper_1x2", market="1x2", selection="HOME", line=None, edge=0.07),
            _candidate("paper_btts", market="btts", selection="YES", line=None, edge=0.06),
            _candidate("paper_corners", market="corner_total", selection="OVER", line=9.5, edge=0.04),
        ],
        policy=_policy(),
    )

    by_trade_id = {row.paper_trade_id: row for row in result.dispositions}
    assert by_trade_id["paper_goals"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_1x2"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_btts"].selection_state == PORTFOLIO_STATE_SELECTED
    assert by_trade_id["paper_corners"].selection_state == PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT
    assert len(result.selected_positions) == 3


def test_fixture_with_twenty_three_qualified_positions_still_selects_only_three() -> None:
    candidates = [
        _candidate(f"paper_goals_over_{index}", market="goals_total", selection="OVER", line=2.0 + (index * 0.25), edge=0.20 - (index * 0.01))
        for index in range(5)
    ]
    candidates.extend(
        _candidate(f"paper_goals_under_{index}", market="goals_total", selection="UNDER", line=3.0 + (index * 0.25), edge=0.14 - (index * 0.01))
        for index in range(4)
    )
    candidates.extend(
        _candidate(f"paper_corners_over_{index}", market="corner_total", selection="OVER", line=8.5 + index, edge=0.13 - (index * 0.01))
        for index in range(5)
    )
    candidates.extend(
        _candidate(f"paper_ah_home_{index}", market="asian_handicap", selection="HOME", line=-0.25 - (index * 0.25), edge=0.12 - (index * 0.01))
        for index in range(4)
    )
    candidates.extend(
        _candidate(f"paper_1x2_{selection}", market="1x2", selection=selection, line=None, edge=edge)
        for selection, edge in (("HOME", 0.11), ("DRAW", 0.10), ("AWAY", 0.09))
    )
    candidates.extend(
        _candidate(f"paper_btts_{selection}", market="btts", selection=selection, line=None, edge=edge)
        for selection, edge in (("YES", 0.08), ("NO", 0.07))
    )

    result = select_fixture_portfolio(candidates, policy=_policy())

    assert len(candidates) == 23
    assert len(result.selected_positions) == 3
    assert {row.selection_state for row in result.selected_positions} == {PORTFOLIO_STATE_SELECTED}


def test_selection_is_deterministic_for_ties() -> None:
    candidates = [
        _candidate("paper_b", market="goals_total", selection="OVER", line=2.5, edge=0.05),
        _candidate("paper_a", market="goals_total", selection="OVER", line=2.5, edge=0.05),
    ]

    first = select_fixture_portfolio(candidates, policy=_policy())
    second = select_fixture_portfolio(list(reversed(candidates)), policy=_policy())

    assert first.dispositions == second.dispositions


def test_portfolio_evaluation_storage_is_idempotent_and_prefers_newer_snapshot(tmp_path) -> None:
    db_path = tmp_path / "football_portfolio_selection.sqlite"
    policy = _policy()
    first_candidates = [
        _candidate("paper_1", market="1x2", selection="HOME", line=None, edge=0.07),
    ]
    first_result = select_fixture_portfolio(first_candidates, policy=policy)
    second_candidates = [
        _candidate("paper_1", market="1x2", selection="HOME", line=None, edge=0.07),
        _candidate(
            "paper_2",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.09,
            decision_timestamp="2026-08-20T10:05:00+00:00",
        ),
    ]
    second_result = select_fixture_portfolio(second_candidates, policy=policy)

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        first_signature = football_portfolio_selection.portfolio_evaluation_signature(
            policy=policy,
            canonical_fixture_id="cf_1",
            candidates=_candidate_payloads(first_candidates),
        )
        first_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_1",
            evaluated_at="2026-08-20T10:00:00+00:00",
            candidate_snapshot_at="2026-08-20T10:00:00+00:00",
            candidates=first_candidates,
        )
        duplicate_write = football_portfolio_selection.record_portfolio_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_1",
            evaluated_at="2026-08-20T10:00:00+00:00",
            candidate_snapshot_at="2026-08-20T10:00:00+00:00",
            evaluation_signature=first_signature,
            dispositions=list(first_result.dispositions),
        )
        second_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_1",
            evaluated_at="2026-08-20T10:06:00+00:00",
            candidate_snapshot_at="2026-08-20T10:05:00+00:00",
            candidates=second_candidates,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert first_write.inserted is True
    assert duplicate_write.inserted is False
    assert second_write.inserted is True
    assert {row["paper_trade_id"] for row in current_rows} == {"paper_1", "paper_2"}
    assert {row["selection_state"] for row in current_rows} == {PORTFOLIO_STATE_SELECTED}


def test_consecutive_identical_state_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "portfolio_consecutive_identical.sqlite"
    policy = _policy()
    candidates = [
        _candidate("paper_a", market="1x2", selection="HOME", line=None, edge=0.07, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        first_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_consecutive",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=candidates,
        )
        second_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_consecutive",
            evaluated_at="2026-08-31T10:30:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=candidates,
        )
        history = _evaluation_history(conn)
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert first_write.inserted is True
    assert second_write.inserted is False
    assert second_write.evaluation_id == first_write.evaluation_id
    assert len(history) == 1
    assert history[0]["predecessor_evaluation_id"] is None
    assert {row["paper_trade_id"] for row in current_rows} == {"paper_a"}


def test_state_recurrence_appends_new_lifecycle_event_and_preserves_history(tmp_path) -> None:
    db_path = tmp_path / "portfolio_state_recurrence.sqlite"
    policy = _policy()
    recurring_candidates = [
        _candidate("paper_a", market="goals_total", selection="OVER", line=2.5, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        first_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_recurrence",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=recurring_candidates,
        )
        empty_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_recurrence",
            evaluated_at="2026-08-31T10:30:00+00:00",
            candidate_snapshot_at="2026-08-31T10:30:00+00:00",
            candidates=[],
        )
        recurring_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_recurrence",
            evaluated_at="2026-08-31T11:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=recurring_candidates,
        )
        history = _evaluation_history(conn)
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )
        selection_rows = conn.execute(
            """
            SELECT evaluation_id, paper_trade_id, selection_state
            FROM football_portfolio_selections
            WHERE paper_trade_id = ?
            ORDER BY evaluated_at, created_at, portfolio_selection_id
            """,
            ("paper_a",),
        ).fetchall()

    assert first_write.inserted is True
    assert empty_write.inserted is True
    assert recurring_write.inserted is True
    assert len(history) == 3
    assert history[0]["evaluation_signature"] == history[2]["evaluation_signature"]
    assert history[0]["evaluation_id"] != history[2]["evaluation_id"]
    assert history[0]["predecessor_evaluation_id"] is None
    assert history[1]["predecessor_evaluation_id"] == history[0]["evaluation_id"]
    assert history[2]["predecessor_evaluation_id"] == history[1]["evaluation_id"]
    assert current_evaluations[0]["evaluation_id"] == history[2]["evaluation_id"]
    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T11:00:00+00:00"
    assert {row["paper_trade_id"] for row in current_rows} == {"paper_a"}
    assert [dict(row) for row in selection_rows] == [
        {
            "evaluation_id": history[0]["evaluation_id"],
            "paper_trade_id": "paper_a",
            "selection_state": PORTFOLIO_STATE_SELECTED,
        },
        {
            "evaluation_id": history[2]["evaluation_id"],
            "paper_trade_id": "paper_a",
            "selection_state": PORTFOLIO_STATE_SELECTED,
        },
    ]


def test_alternating_state_reuses_content_signature_but_appends_new_event(tmp_path) -> None:
    db_path = tmp_path / "portfolio_alternating.sqlite"
    policy = _policy()
    candidates_a = [
        _candidate("paper_a", market="1x2", selection="HOME", line=None, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]
    candidates_b = [
        _candidate("paper_b", market="btts", selection="YES", line=None, edge=0.07, decision_timestamp="2026-08-31T10:15:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_alternating",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=candidates_a,
        )
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_alternating",
            evaluated_at="2026-08-31T10:30:00+00:00",
            candidate_snapshot_at="2026-08-31T10:15:00+00:00",
            candidates=candidates_b,
        )
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_alternating",
            evaluated_at="2026-08-31T11:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=candidates_a,
        )
        history = _evaluation_history(conn)
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert len(history) == 3
    assert history[0]["evaluation_signature"] == history[2]["evaluation_signature"]
    assert history[0]["evaluation_id"] != history[2]["evaluation_id"]
    assert {row["paper_trade_id"] for row in current_rows} == {"paper_a"}


def test_consecutive_empty_state_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "portfolio_consecutive_empty.sqlite"
    policy = _policy()
    first_candidates = [
        _candidate("paper_a", market="goals_total", selection="OVER", line=2.5, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_empty",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=first_candidates,
        )
        first_empty = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_empty",
            evaluated_at="2026-08-31T10:30:00+00:00",
            candidate_snapshot_at="2026-08-31T10:30:00+00:00",
            candidates=[],
        )
        second_empty = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_empty",
            evaluated_at="2026-08-31T11:00:00+00:00",
            candidate_snapshot_at="2026-08-31T11:00:00+00:00",
            candidates=[],
        )
        history = _evaluation_history(conn)
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert first_empty.inserted is True
    assert second_empty.inserted is False
    assert second_empty.evaluation_id == first_empty.evaluation_id
    assert len(history) == 2
    assert current_rows == []


def test_retry_same_transition_is_idempotent_after_intervening_state(tmp_path) -> None:
    db_path = tmp_path / "portfolio_retry_transition.sqlite"
    policy = _policy()
    recurring_candidates = [
        _candidate("paper_a", market="1x2", selection="HOME", line=None, edge=0.07, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_retry",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=recurring_candidates,
        )
        empty_write = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_retry",
            evaluated_at="2026-08-31T10:30:00+00:00",
            candidate_snapshot_at="2026-08-31T10:30:00+00:00",
            candidates=[],
        )
        first_retry = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_retry",
            evaluated_at="2026-08-31T11:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=recurring_candidates,
        )
        second_retry = _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_retry",
            evaluated_at="2026-08-31T11:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=recurring_candidates,
        )
        history = _evaluation_history(conn)
        matching_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM football_portfolio_evaluations
            WHERE predecessor_evaluation_id = ?
              AND evaluation_signature = ?
            """,
            (empty_write.evaluation_id, history[2]["evaluation_signature"]),
        ).fetchone()[0]

    assert first_retry.inserted is True
    assert second_retry.inserted is False
    assert second_retry.evaluation_id == first_retry.evaluation_id
    assert len(history) == 3
    assert matching_rows == 1


def test_current_evaluation_prefers_later_evaluated_at_when_same_snapshot_becomes_empty(tmp_path) -> None:
    db_path = tmp_path / "portfolio_latest_empty.sqlite"
    policy = _policy()
    first_candidates = [
        _candidate("paper_1", market="goals_total", selection="OVER", line=2.5, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_bug",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=first_candidates,
        )
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_bug",
            evaluated_at="2026-08-31T12:30:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=[],
        )
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T12:30:00+00:00"
    assert current_evaluations[0]["candidate_snapshot_at"] == "2026-08-31T09:55:00+00:00"
    assert current_evaluations[0]["candidate_count"] == 0
    assert current_rows == []


def test_current_evaluation_prefers_later_evaluated_at_when_same_snapshot_grows(tmp_path) -> None:
    db_path = tmp_path / "portfolio_latest_growth.sqlite"
    policy = _policy()
    first_candidates = [
        _candidate("paper_1", market="1x2", selection="HOME", line=None, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]
    second_candidates = [
        _candidate("paper_1", market="1x2", selection="HOME", line=None, edge=0.08, decision_timestamp="2026-08-31T09:55:00+00:00"),
        _candidate("paper_2", market="goals_total", selection="OVER", line=2.5, edge=0.09, decision_timestamp="2026-08-31T09:55:00+00:00"),
        _candidate("paper_3", market="btts", selection="YES", line=None, edge=0.07, decision_timestamp="2026-08-31T09:55:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_growth",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=first_candidates,
        )
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_growth",
            evaluated_at="2026-08-31T12:30:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            candidates=second_candidates,
        )
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T12:30:00+00:00"
    assert current_evaluations[0]["candidate_count"] == 3
    assert {row["paper_trade_id"] for row in current_rows} == {"paper_1", "paper_2", "paper_3"}


def test_current_evaluation_prefers_later_evaluated_at_over_snapshot_metadata(tmp_path) -> None:
    db_path = tmp_path / "portfolio_latest_recency.sqlite"
    policy = _policy()
    older_candidates = [
        _candidate("paper_old", market="goals_total", selection="OVER", line=2.5, edge=0.08, decision_timestamp="2026-08-31T10:05:00+00:00"),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_recency",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T10:05:00+00:00",
            candidates=older_candidates,
        )
        _record_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_recency",
            evaluated_at="2026-08-31T12:30:00+00:00",
            candidate_snapshot_at="2026-08-31T10:00:00+00:00",
            candidates=[],
        )
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
        )

    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T12:30:00+00:00"
    assert current_evaluations[0]["candidate_snapshot_at"] == "2026-08-31T10:00:00+00:00"
    assert current_rows == []


def test_portfolio_storage_operates_after_explicit_boundary_init_without_reinitializing(tmp_path) -> None:
    db_path = tmp_path / "portfolio_boundary.sqlite"
    policy = _policy()
    candidates = [
        _candidate("paper_boundary", market="1x2", selection="HOME", line=None, edge=0.08),
    ]

    with connect(db_path) as conn:
        football_portfolio_selection.ensure_schema(conn)
        result = select_fixture_portfolio(candidates, policy=policy)
        signature = football_portfolio_selection.portfolio_evaluation_signature(
            policy=policy,
            canonical_fixture_id="cf_boundary",
            candidates=_candidate_payloads(candidates),
        )
        write = football_portfolio_selection.record_portfolio_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id="cf_boundary",
            evaluated_at="2026-08-31T10:00:00+00:00",
            candidate_snapshot_at="2026-08-31T09:55:00+00:00",
            evaluation_signature=signature,
            dispositions=list(result.dispositions),
            initialize_schema=False,
        )
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            initialize_schema=False,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            initialize_schema=False,
        )

    assert write.inserted is True
    assert len(current_evaluations) == 1
    assert len(current_rows) == 1
    assert current_rows[0]["paper_trade_id"] == "paper_boundary"


def test_portfolio_reads_fail_clearly_without_boundary_init(tmp_path) -> None:
    db_path = tmp_path / "portfolio_uninitialized.sqlite"
    policy = _policy()

    with connect(db_path) as conn:
        try:
            football_portfolio_selection.fetch_current_portfolio_evaluations(
                conn,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                initialize_schema=False,
            )
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected fetch_current_portfolio_evaluations to fail without explicit initialization")

        try:
            football_portfolio_selection.fetch_current_portfolio_selections(
                conn,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                initialize_schema=False,
            )
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected fetch_current_portfolio_selections to fail without explicit initialization")
