"""Leakage-safe confidence replay for Football Model Picks.

Reads the Day 10 walk-forward market prices, which are built from pre-match model
content only (``prematch_only_pricing_input_excludes_outcomes_and_market_data_v1``),
and grades them against the research DB results.

Every choice — confidence threshold, corner temperature, model/market blend
weight — is made on the SELECTION seasons only, then frozen and evaluated on the
HOLDOUT seasons. Holdout outcomes never influence a choice.

Probability semantics: Day 10 ``effective_win_stake_probability`` is unconditional
(W = P(full win) + 0.5 P(half win), with push mass in the denominator). The
calibrated quantity that break-even odds compare against is the push-excluded
stake share q = W / (W + L) = 1 / fair_decimal_price. This module uses q
throughout and grades realised results on the same basis.

Research only: no provider calls, no production writes, no live model change.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..dashboard.model_pick_policy import select_model_picks
from ..storage.football_paper_trading import settlement_state_for_market

EXPERIMENT_VERSION = "football_model_pick_confidence_replay_v1"
DEFAULT_SELECTION_SEASONS = ("2223", "2324")
DEFAULT_HOLDOUT_SEASONS = ("2425", "2526")
THRESHOLDS = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60, 0.65, 0.70, 0.75)
TEMPERATURE_GRID = tuple(round(0.50 + 0.05 * i, 2) for i in range(31))  # 0.50 .. 2.00
BLEND_GRID = tuple(i / 20 for i in range(21))
REPLAY_MARKETS = ("asian_handicap", "corner_total", "goals_total", "btts", "1x2")

# Stake shares per settlement state: (won, lost).
_STAKE_SHARE = {
    "WON": (1.0, 0.0),
    "HALF_WON": (0.5, 0.0),
    "HALF_WIN": (0.5, 0.0),
    "PUSH": (0.0, 0.0),
    "HALF_LOST": (0.0, 0.5),
    "HALF_LOSS": (0.0, 0.5),
    "LOST": (0.0, 1.0),
}


@dataclass(frozen=True)
class Fixture:
    event_id: str
    competition_id: str
    season_code: str
    home_goals: int
    away_goals: int
    total_corners: int | None
    pinnacle_1x2: Mapping[str, float] | None = None


@dataclass
class Cohort:
    """Stake-weighted win share with push exclusion; matches q's semantics."""

    n: int = 0
    won: float = 0.0
    lost: float = 0.0
    pushes: int = 0
    q_sum: float = 0.0
    fair_sum: float = 0.0
    states: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, state: str, q: float, fair: float) -> None:
        w, l = _STAKE_SHARE[state]
        self.n += 1
        self.won += w
        self.lost += l
        self.pushes += state == "PUSH"
        self.q_sum += q
        self.fair_sum += fair
        self.states[state] += 1

    @property
    def win_share(self) -> float | None:
        total = self.won + self.lost
        return self.won / total if total else None

    def to_dict(self, coverage_of: int | None = None) -> dict[str, Any]:
        lo, hi = wilson_interval(self.win_share, self.won + self.lost)
        out = {
            "n": self.n,
            "win_share": self.win_share,
            "ci95": [lo, hi],
            "mean_q": self.q_sum / self.n if self.n else None,
            "mean_fair_price": self.fair_sum / self.n if self.n else None,
            "break_even_price": (1.0 / self.win_share) if self.win_share else None,
            "states": dict(self.states),
        }
        if coverage_of:
            out["coverage"] = self.n / coverage_of
        return out


def wilson_interval(p: float | None, n: float, z: float = 1.96) -> tuple[float | None, float | None]:
    if p is None or n <= 0:
        return (None, None)
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def conditional_probability(row: Mapping[str, Any]) -> float:
    """Push-excluded stake-win probability q = 1 / fair price."""
    return 1.0 / float(row["fair_decimal_price"])


def grade(fixture: Fixture, market: str, selection: str, line: float | None) -> str:
    return settlement_state_for_market(
        market=market,
        selection=selection,
        line=line,
        actual_home_goals=fixture.home_goals,
        actual_away_goals=fixture.away_goals,
        actual_total_corners=fixture.total_corners,
    )


# ------------------------------------------------------------------ loading
def load_fixtures(research_db: Path) -> dict[str, Fixture]:
    conn = sqlite3.connect(f"file:{Path(research_db).as_posix()}?mode=ro", uri=True)
    try:
        pin = {
            event_id: {k.upper(): float(v) for k, v in json.loads(prices).items()}
            for event_id, prices in conn.execute(
                "SELECT event_id, selection_prices_json FROM football_market_quotes WHERE source = 'pinnacle_closing_1x2'"
            )
        }
        out = {}
        for event_id, comp, season, hg, ag, hc, ac in conn.execute(
            "SELECT event_id, competition_id, season_code, home_goals, away_goals, home_corners, away_corners FROM football_events"
        ):
            if hg is None or ag is None:
                continue
            corners = hc + ac if hc is not None and ac is not None else None
            out[event_id] = Fixture(event_id, comp, str(season), int(hg), int(ag), corners, pin.get(event_id))
        return out
    finally:
        conn.close()


def load_day10_rows(path: Path, markets: Sequence[str] = REPLAY_MARKETS) -> dict[str, list[dict[str, Any]]]:
    wanted = set(markets)
    by_fixture: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row["market_family"] not in wanted:
                continue
            by_fixture[row["event_id"]].append(
                {
                    "market_family": row["market_family"],
                    "line": row["line"],
                    "selection": row["selection"],
                    "fair_decimal_price": row["fair_decimal_price"],
                    "effective_win_stake_probability": row["effective_win_stake_probability"],
                }
            )
    return by_fixture


# ------------------------------------------------------------------ analyses
def balanced_policy_picks(
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]], fixtures: Mapping[str, Fixture]
) -> list[dict[str, Any]]:
    """Apply the production balanced-line policy to every replay fixture."""
    out = []
    for event_id, rows in rows_by_fixture.items():
        fixture = fixtures.get(event_id)
        if fixture is None:
            continue
        picks = select_model_picks(rows)
        for market in ("asian_handicap", "corner_total"):
            pick = picks.get(market)
            if pick is None:
                continue
            pick = pick if isinstance(pick, Mapping) else pick.__dict__
            fair = float(pick["model_fair_price"])
            out.append(
                {
                    "event_id": event_id,
                    "competition_id": fixture.competition_id,
                    "season_code": fixture.season_code,
                    "market": market,
                    "selection": pick["selection"],
                    "line": pick["line"],
                    "q": 1.0 / fair,
                    "fair": fair,
                    "state": grade(fixture, market, pick["selection"], pick["line"]),
                }
            )
    return out


def cohort_of(picks: Iterable[Mapping[str, Any]]) -> Cohort:
    c = Cohort()
    for p in picks:
        c.add(p["state"], p["q"], p["fair"])
    return c


def threshold_sweep(
    picks: Sequence[Mapping[str, Any]], *, selection: set[str], holdout: set[str], thresholds: Sequence[float] = THRESHOLDS
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for market in sorted({p["market"] for p in picks}):
        sel = [p for p in picks if p["market"] == market and p["season_code"] in selection]
        hold = [p for p in picks if p["market"] == market and p["season_code"] in holdout]
        out[market] = {
            f"{t:.2f}": {
                "selection": cohort_of(p for p in sel if p["q"] >= t).to_dict(len(sel)),
                "holdout": cohort_of(p for p in hold if p["q"] >= t).to_dict(len(hold)),
            }
            for t in thresholds
        }
    return out


def choose_threshold(
    picks: Sequence[Mapping[str, Any]],
    *,
    market: str,
    selection: set[str],
    target: float,
    min_selected: int = 200,
    thresholds: Sequence[float] = THRESHOLDS,
) -> float | None:
    """Lowest threshold whose SELECTION-season win share reaches ``target`` with enough picks."""
    sel = [p for p in picks if p["market"] == market and p["season_code"] in selection]
    for t in sorted(thresholds):
        c = cohort_of(p for p in sel if p["q"] >= t)
        if c.n >= min_selected and c.win_share is not None and c.win_share >= target:
            return t
    return None


def high_probability_line_cohort(
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]],
    fixtures: Mapping[str, Fixture],
    *,
    market: str,
    threshold: float,
    seasons: set[str],
) -> dict[str, Any]:
    """Per fixture, the catalog line closest to ``threshold`` from above (longest fair price with q >= t)."""
    c = Cohort()
    total = 0
    for event_id, rows in rows_by_fixture.items():
        fixture = fixtures.get(event_id)
        if fixture is None or fixture.season_code not in seasons:
            continue
        total += 1
        cands = [r for r in rows if r["market_family"] == market and conditional_probability(r) >= threshold]
        if not cands:
            continue
        r = max(cands, key=lambda r: float(r["fair_decimal_price"]))
        c.add(grade(fixture, market, r["selection"], r["line"]), conditional_probability(r), float(r["fair_decimal_price"]))
    return c.to_dict(total)


def _binary_rows(rows_by_fixture, fixtures, market, seasons):
    for event_id, rows in rows_by_fixture.items():
        fixture = fixtures.get(event_id)
        if fixture is None or fixture.season_code not in seasons:
            continue
        for r in rows:
            if r["market_family"] != market:
                continue
            w, l = _STAKE_SHARE[grade(fixture, market, r["selection"], r["line"])]
            if w + l == 0:
                continue
            yield conditional_probability(r), w, l


def _temper(q: float, temperature: float) -> float:
    q = min(max(q, 1e-9), 1 - 1e-9)
    z = math.log(q / (1 - q)) / temperature
    return 1.0 / (1.0 + math.exp(-z))


def calibration(samples: Iterable[tuple[float, float, float]], temperature: float = 1.0) -> dict[str, Any]:
    bins = defaultdict(lambda: [0.0, 0.0, 0.0])
    ll = 0.0
    weight = 0.0
    for q, w, l in samples:
        q = _temper(q, temperature)
        b = min(int(q * 10), 9)
        bins[b][0] += w + l
        bins[b][1] += q * (w + l)
        bins[b][2] += w
        ll += -(w * math.log(max(q, 1e-12)) + l * math.log(max(1 - q, 1e-12)))
        weight += w + l
    if not weight:
        return {"weight": 0, "ece": None, "log_loss": None, "bins": []}
    ece = sum(v[0] / weight * abs(v[1] / v[0] - v[2] / v[0]) for v in bins.values() if v[0])
    return {
        "weight": weight,
        "ece": ece,
        "log_loss": ll / weight,
        "bins": [
            {"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "weight": v[0], "mean_q": v[1] / v[0], "observed": v[2] / v[0]}
            for b, v in sorted(bins.items())
            if v[0]
        ],
    }


def fit_temperature(samples: Sequence[tuple[float, float, float]], grid: Sequence[float] = TEMPERATURE_GRID) -> float:
    return min(grid, key=lambda t: calibration(samples, t)["log_loss"])


def model_vs_market_1x2(
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]],
    fixtures: Mapping[str, Fixture],
    *,
    selection: set[str],
    holdout: set[str],
) -> dict[str, Any]:
    def probs(event_id):
        model = {r["selection"]: float(r["effective_win_stake_probability"]) for r in rows_by_fixture[event_id] if r["market_family"] == "1x2"}
        inv = {k: 1.0 / v for k, v in fixtures[event_id].pinnacle_1x2.items()}
        total = sum(inv.values())
        return model, {k: v / total for k, v in inv.items()}

    def result(event_id):
        f = fixtures[event_id]
        return "HOME" if f.home_goals > f.away_goals else "AWAY" if f.away_goals > f.home_goals else "DRAW"

    def log_loss(ids, w):
        tot = 0.0
        for e in ids:
            m, q = probs(e)
            raw = {k: math.exp(w * math.log(max(m[k], 1e-9)) + (1 - w) * math.log(max(q[k], 1e-9))) for k in m}
            s = sum(raw.values())
            tot += -math.log(max(raw[result(e)] / s, 1e-12))
        return tot / len(ids) if ids else None

    common = [
        e for e in rows_by_fixture
        if e in fixtures and fixtures[e].pinnacle_1x2 and len([r for r in rows_by_fixture[e] if r["market_family"] == "1x2"]) == 3
    ]
    sel = [e for e in common if fixtures[e].season_code in selection]
    hold = [e for e in common if fixtures[e].season_code in holdout]
    if not sel or not hold:
        return {"available": False}
    weight = min(BLEND_GRID, key=lambda w: log_loss(sel, w))
    return {
        "available": True,
        "selection_n": len(sel),
        "holdout_n": len(hold),
        "blend_weight_on_model_chosen_on_selection": weight,
        "holdout_log_loss": {"model": log_loss(hold, 1.0), "market": log_loss(hold, 0.0), "blend": log_loss(hold, weight)},
    }


def run_replay(
    *,
    day10_prices: Path,
    research_db: Path,
    selection_seasons: Sequence[str] = DEFAULT_SELECTION_SEASONS,
    holdout_seasons: Sequence[str] = DEFAULT_HOLDOUT_SEASONS,
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    fixtures: Mapping[str, Fixture] | None = None,
) -> dict[str, Any]:
    selection, holdout = set(selection_seasons), set(holdout_seasons)
    if selection & holdout:
        raise ValueError("selection and holdout seasons must not overlap")
    fixtures = fixtures if fixtures is not None else load_fixtures(research_db)
    rows_by_fixture = rows_by_fixture if rows_by_fixture is not None else load_day10_rows(day10_prices)
    picks = balanced_policy_picks(rows_by_fixture, fixtures)

    summary: dict[str, Any] = {
        "experiment_version": EXPERIMENT_VERSION,
        "selection_seasons": sorted(selection),
        "holdout_seasons": sorted(holdout),
        "probability_semantics": "q = 1 / fair_decimal_price (push-excluded stake share)",
        "balanced_policy": {},
        "threshold_sweep": threshold_sweep(picks, selection=selection, holdout=holdout),
        "chosen_thresholds": {},
        "high_probability_lines": {},
        "calibration_holdout": {},
        "corner_temperature": {},
    }
    for market in ("asian_handicap", "corner_total"):
        mp = [p for p in picks if p["market"] == market]
        summary["balanced_policy"][market] = {
            "all": cohort_of(mp).to_dict(),
            "by_season": {s: cohort_of(p for p in mp if p["season_code"] == s).to_dict() for s in sorted({p["season_code"] for p in mp})},
            "by_competition": {c: cohort_of(p for p in mp if p["competition_id"] == c).to_dict() for c in sorted({p["competition_id"] for p in mp})},
        }
        for target in (0.65, 0.70):
            t = choose_threshold(picks, market=market, selection=selection, target=target)
            hold = [p for p in mp if p["season_code"] in holdout]
            summary["chosen_thresholds"][f"{market}@{target:.2f}"] = {
                "threshold_chosen_on_selection": t,
                "holdout": cohort_of(p for p in hold if t is not None and p["q"] >= t).to_dict(len(hold)) if t is not None else None,
            }
    for market in ("asian_handicap", "corner_total", "1x2"):
        summary["high_probability_lines"][market] = {
            f"{t:.2f}": {
                "selection": high_probability_line_cohort(rows_by_fixture, fixtures, market=market, threshold=t, seasons=selection),
                "holdout": high_probability_line_cohort(rows_by_fixture, fixtures, market=market, threshold=t, seasons=holdout),
            }
            for t in (0.60, 0.65, 0.70, 0.75)
        }
    for market in REPLAY_MARKETS:
        summary["calibration_holdout"][market] = calibration(_binary_rows(rows_by_fixture, fixtures, market, holdout))
    corner_sel = list(_binary_rows(rows_by_fixture, fixtures, "corner_total", selection))
    temperature = fit_temperature(corner_sel)
    corner_hold = list(_binary_rows(rows_by_fixture, fixtures, "corner_total", holdout))
    summary["corner_temperature"] = {
        "temperature_chosen_on_selection": temperature,
        "holdout_before": {k: v for k, v in calibration(corner_hold).items() if k != "bins"},
        "holdout_after": calibration(corner_hold, temperature),
    }
    summary["model_vs_market_1x2"] = model_vs_market_1x2(rows_by_fixture, fixtures, selection=selection, holdout=holdout)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day10-prices", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSON summary path")
    parser.add_argument("--selection-seasons", nargs="+", default=list(DEFAULT_SELECTION_SEASONS))
    parser.add_argument("--holdout-seasons", nargs="+", default=list(DEFAULT_HOLDOUT_SEASONS))
    args = parser.parse_args(argv)
    summary = run_replay(
        day10_prices=args.day10_prices,
        research_db=args.research_db,
        selection_seasons=args.selection_seasons,
        holdout_seasons=args.holdout_seasons,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True, default=float), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
