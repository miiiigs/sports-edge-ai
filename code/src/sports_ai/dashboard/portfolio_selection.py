"""Deterministic portfolio selection for qualified football paper opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

PORTFOLIO_POLICY_ID_V1 = "football_portfolio_selection_v1"
PORTFOLIO_POLICY_VERSION_V1 = "v1"
PORTFOLIO_LEGACY_PRE_POLICY = "LEGACY_PRE_POLICY"
PORTFOLIO_STATE_SELECTED = "SELECTED"
PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER = "NOT_SELECTED_SAME_MARKET_CLUSTER"
PORTFOLIO_STATE_NOT_SELECTED_MARKET_FAMILY_LIMIT = "NOT_SELECTED_MARKET_FAMILY_LIMIT"
PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT = "NOT_SELECTED_FIXTURE_LIMIT"
PORTFOLIO_STATE_PENDING = "QUALIFIED_PENDING_PORTFOLIO_SELECTION"
PORTFOLIO_STATE_POLICY_INACTIVE = "PORTFOLIO_POLICY_INACTIVE"
PORTFOLIO_STATE_NOT_APPLICABLE = "NOT_APPLICABLE"
PORTFOLIO_LIFECYCLE_CURRENT = "CURRENT_PORTFOLIO_SELECTION"
PORTFOLIO_MARKET_ORDER = {
    "1x2": 0,
    "btts": 1,
    "goals_total": 2,
    "asian_handicap": 3,
    "corner_total": 4,
}
MAX_SELECTED_PER_MARKET_FAMILY = 1
MAX_SELECTED_PER_FIXTURE = 3
MAX_SELECTED_PER_MARKET_SELECTION_CLUSTER = 1


@dataclass(frozen=True)
class PortfolioPolicy:
    policy_id: str
    policy_version: str
    effective_from: str
    lifecycle_stage: str = PORTFOLIO_LIFECYCLE_CURRENT


@dataclass(frozen=True)
class PortfolioCandidate:
    paper_trade_id: str
    canonical_fixture_id: str
    market: str
    selection: str
    line: float | None
    settlement_aware_edge: float | None
    decision_timestamp: str
    created_at: str
    safeguard_state: str
    freshness_state: str | None = None


@dataclass(frozen=True)
class PortfolioDisposition:
    paper_trade_id: str
    canonical_fixture_id: str
    market: str
    selection: str
    line: float | None
    selection_state: str
    selection_rank: int
    selection_reason: str
    edge_at_selection: float | None


@dataclass(frozen=True)
class PortfolioSelectionResult:
    policy: PortfolioPolicy
    fixture_id: str
    dispositions: tuple[PortfolioDisposition, ...]

    @property
    def selected_positions(self) -> tuple[PortfolioDisposition, ...]:
        return tuple(item for item in self.dispositions if item.selection_state == PORTFOLIO_STATE_SELECTED)


def _line_sort_value(line: float | None) -> tuple[int, float]:
    return (1, 0.0) if line is None else (0, float(line))


def _ranking_key(candidate: PortfolioCandidate) -> tuple[float, str, int, str, tuple[int, float], str]:
    edge = float(candidate.settlement_aware_edge) if candidate.settlement_aware_edge is not None else -1_000_000_000.0
    return (
        -edge,
        str(candidate.decision_timestamp),
        PORTFOLIO_MARKET_ORDER.get(str(candidate.market), 999),
        str(candidate.selection),
        _line_sort_value(candidate.line),
        str(candidate.paper_trade_id),
    )


def _selection_reason(*, state: str, candidate: PortfolioCandidate) -> str:
    if state == PORTFOLIO_STATE_SELECTED:
        return (
            "Selected by portfolio policy after deterministic market-family ranking "
            f"for {candidate.market} {candidate.selection}."
        )
    if state == PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER:
        return (
            "Not selected because a higher-ranked line already represents the same "
            f"{candidate.market} {candidate.selection} cluster."
        )
    if state == PORTFOLIO_STATE_NOT_SELECTED_MARKET_FAMILY_LIMIT:
        return (
            "Not selected because a higher-ranked position already occupies the same "
            f"{candidate.market} market-family slot."
        )
    if state == PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT:
        return "Not selected because the fixture portfolio already reached the three-position cap."
    raise ValueError(f"Unsupported portfolio selection state: {state}")


def select_fixture_portfolio(
    qualified_positions: Iterable[PortfolioCandidate],
    *,
    policy: PortfolioPolicy,
) -> PortfolioSelectionResult:
    candidates = tuple(sorted(qualified_positions, key=_ranking_key))
    if not candidates:
        return PortfolioSelectionResult(policy=policy, fixture_id="", dispositions=())
    fixture_ids = {candidate.canonical_fixture_id for candidate in candidates}
    if len(fixture_ids) != 1:
        raise ValueError("Portfolio selection requires candidates from exactly one canonical fixture")
    ranked_candidates = {candidate.paper_trade_id: index for index, candidate in enumerate(candidates, start=1)}
    selected_by_cluster: dict[tuple[str, str], PortfolioCandidate] = {}
    selected_by_market: dict[str, PortfolioCandidate] = {}
    selected_trade_ids: set[str] = set()
    cluster_blocked: set[str] = set()
    family_blocked: set[str] = set()
    fixture_blocked: set[str] = set()

    for candidate in candidates:
        cluster_key = (str(candidate.market), str(candidate.selection))
        if cluster_key in selected_by_cluster:
            cluster_blocked.add(candidate.paper_trade_id)
            continue
        selected_by_cluster[cluster_key] = candidate

    cluster_winners = tuple(sorted(selected_by_cluster.values(), key=_ranking_key))
    for candidate in cluster_winners:
        market_key = str(candidate.market)
        if market_key in selected_by_market:
            family_blocked.add(candidate.paper_trade_id)
            continue
        selected_by_market[market_key] = candidate

    market_winners = tuple(sorted(selected_by_market.values(), key=_ranking_key))
    for index, candidate in enumerate(market_winners):
        if index < MAX_SELECTED_PER_FIXTURE:
            selected_trade_ids.add(candidate.paper_trade_id)
        else:
            fixture_blocked.add(candidate.paper_trade_id)

    dispositions = []
    for candidate in candidates:
        if candidate.paper_trade_id in selected_trade_ids:
            state = PORTFOLIO_STATE_SELECTED
        elif candidate.paper_trade_id in cluster_blocked:
            state = PORTFOLIO_STATE_NOT_SELECTED_SAME_MARKET_CLUSTER
        elif candidate.paper_trade_id in family_blocked:
            state = PORTFOLIO_STATE_NOT_SELECTED_MARKET_FAMILY_LIMIT
        elif candidate.paper_trade_id in fixture_blocked:
            state = PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT
        else:
            state = PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT
        dispositions.append(
            PortfolioDisposition(
                paper_trade_id=candidate.paper_trade_id,
                canonical_fixture_id=candidate.canonical_fixture_id,
                market=candidate.market,
                selection=candidate.selection,
                line=candidate.line,
                selection_state=state,
                selection_rank=ranked_candidates[candidate.paper_trade_id],
                selection_reason=_selection_reason(state=state, candidate=candidate),
                edge_at_selection=candidate.settlement_aware_edge,
            )
        )
    return PortfolioSelectionResult(
        policy=policy,
        fixture_id=next(iter(fixture_ids)),
        dispositions=tuple(dispositions),
    )
