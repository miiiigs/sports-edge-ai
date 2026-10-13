"""High-confidence AH lane vs market and simple baselines (leakage-safe, time-ordered).

Lanes, one pick per fixture:
- MODEL_HC: catalog AH line with the longest fair price whose model q >= threshold;
  requires >= MIN_VENUE_HISTORY prior venue matches for both teams (as #120).
- MARKET_HC: the same rule with market-implied q.
- BALANCED: the production balanced-line Model Pick.
- FAVOURITE_DNB / FAVOURITE_WIN: back the de-vigged Pinnacle 1X2 favourite at AH 0.0 / -0.5.

Market-implied AH: there are no historical AH prices, so independent Poisson goal
means are solved per fixture to reproduce the de-vigged Pinnacle closing 1X2
(home/away). Every catalog line is then priced through the paper settlement
engine. This is an approximation of a real AH market; its own calibration is
reported so the benchmark can be judged.

Controls:
- one pick per fixture;
- paired comparisons on identical (fixture, line, side);
- cluster bootstrap by match date;
- per-league breakdown;
- the 0.70 threshold is pre-registered by the owner (other thresholds are
  exploratory);
- coverage is reported per lane.

The model is never tuned on holdout data.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import football_model_pick_confidence_replay as rep

EXPERIMENT_VERSION = "football_high_confidence_benchmark_v1"
PREREGISTERED_THRESHOLD = 0.70
EXPLORATORY_THRESHOLDS = (0.60, 0.65, 0.75, 0.80)
MIN_VENUE_HISTORY = 2
AH_LINES = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
MAX_GOALS = 10
BOOTSTRAP_DRAWS = 1000


# ------------------------------------------------------------------ market-implied AH
def _poisson(lam: float) -> list[float]:
    out = [math.exp(-lam)]
    for k in range(1, MAX_GOALS + 1):
        out.append(out[-1] * lam / k)
    return out


def _outcome_probs(lh: float, la: float) -> tuple[float, float]:
    ph, pa = _poisson(lh), _poisson(la)
    home = sum(ph[i] * pa[j] for i in range(MAX_GOALS + 1) for j in range(i))
    away = sum(ph[i] * pa[j] for i in range(MAX_GOALS + 1) for j in range(i + 1, MAX_GOALS + 1))
    return home, away


def solve_poisson_means(p_home: float, p_away: float, *, iterations: int = 60) -> tuple[float, float] | None:
    """Goal means (lh, la) whose independent-Poisson 1X2 reproduces (p_home, p_away)."""
    lh, la = 1.4, 1.1
    for _ in range(iterations):
        h, a = _outcome_probs(lh, la)
        fh, fa = h - p_home, a - p_away
        if abs(fh) < 1e-10 and abs(fa) < 1e-10:
            return lh, la
        eps = 1e-6
        h1, a1 = _outcome_probs(lh + eps, la)
        h2, a2 = _outcome_probs(lh, la + eps)
        j11, j21 = (h1 - h) / eps, (a1 - a) / eps
        j12, j22 = (h2 - h) / eps, (a2 - a) / eps
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-14:
            return None
        dl = (fh * j22 - fa * j12) / det
        da = (fa * j11 - fh * j21) / det
        lh = min(max(lh - dl, 0.05), 6.0)
        la = min(max(la - da, 0.05), 6.0)
    h, a = _outcome_probs(lh, la)
    return (lh, la) if abs(h - p_home) < 1e-6 and abs(a - p_away) < 1e-6 else None


def devig(prices: Mapping[str, float]) -> dict[str, float]:
    inv = {k: 1.0 / float(v) for k, v in prices.items()}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()}


def market_ah_rows(prices: Mapping[str, float]) -> list[dict[str, Any]] | None:
    probs = devig(prices)
    means = solve_poisson_means(probs["HOME"], probs["AWAY"])
    if means is None:
        return None
    ph, pa = _poisson(means[0]), _poisson(means[1])
    rows = []
    for line in AH_LINES:
        for selection in ("HOME", "AWAY"):
            w = l = 0.0
            for i in range(MAX_GOALS + 1):
                for j in range(MAX_GOALS + 1):
                    sw, sl = rep._STAKE_SHARE[rep.settlement_state_for_market(
                        market="asian_handicap", selection=selection, line=line, actual_home_goals=i, actual_away_goals=j
                    )]
                    w += ph[i] * pa[j] * sw
                    l += ph[i] * pa[j] * sl
            if w > 0 and l > 0:
                rows.append({"line": line, "selection": selection, "q": w / (w + l), "fair": 1.0 + l / w})
    return rows


# ------------------------------------------------------------------ history (live definition)
def venue_history(research_db: Path) -> dict[str, int]:
    """min(prior home matches of home team, prior away matches of away team) within the competition."""
    conn = sqlite3.connect(f"file:{Path(research_db).as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT event_id, competition_id, home_team, away_team FROM football_events "
            "ORDER BY match_date, COALESCE(kickoff_time, ''), event_id"
        ).fetchall()
    finally:
        conn.close()
    seen: dict[tuple[str, str, str], int] = defaultdict(int)
    out = {}
    for event_id, comp, home, away in rows:
        out[event_id] = min(seen[(comp, home, "home")], seen[(comp, away, "away")])
        seen[(comp, home, "home")] += 1
        seen[(comp, away, "away")] += 1
    return out


# ------------------------------------------------------------------ lanes
def _highest_fair_at_threshold(rows: Sequence[Mapping[str, Any]], threshold: float) -> Mapping[str, Any] | None:
    cands = [r for r in rows if r["q"] >= threshold]
    return max(cands, key=lambda r: r["fair"]) if cands else None


def build_lanes(
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]],
    fixtures: Mapping[str, rep.Fixture],
    history: Mapping[str, int],
    *,
    seasons: set[str],
    threshold: float = PREREGISTERED_THRESHOLD,
    match_dates: Mapping[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    lanes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    balanced = {(p["event_id"]): p for p in rep.balanced_policy_picks(rows_by_fixture, fixtures) if p["market"] == "asian_handicap"}
    for event_id, rows in rows_by_fixture.items():
        f = fixtures.get(event_id)
        if f is None or f.season_code not in seasons:
            continue
        model_rows = [
            {"line": r["line"], "selection": r["selection"], "q": rep.conditional_probability(r), "fair": float(r["fair_decimal_price"])}
            for r in rows if r["market_family"] == "asian_handicap"
        ]
        market = market_ah_rows(f.pinnacle_1x2) if f.pinnacle_1x2 else None
        mq = {(r["line"], r["selection"]): r["q"] for r in market or []}
        base = {"event_id": event_id, "competition_id": f.competition_id, "date": (match_dates or {}).get(event_id, "")}

        def add(lane, pick):
            state = rep.grade(f, "asian_handicap", pick["selection"], pick["line"])
            lanes[lane].append({**base, "line": pick["line"], "selection": pick["selection"], "q": pick["q"],
                                "fair": pick["fair"], "market_q": mq.get((pick["line"], pick["selection"])), "state": state})

        lanes["_ELIGIBLE"].append(base)
        if history.get(event_id, 0) >= MIN_VENUE_HISTORY:
            pick = _highest_fair_at_threshold(model_rows, threshold)
            if pick:
                add("MODEL_HC", pick)
        if market:
            pick = _highest_fair_at_threshold(market, threshold)
            if pick:
                add("MARKET_HC", {**pick})
            probs = devig(f.pinnacle_1x2)
            fav = "HOME" if probs["HOME"] >= probs["AWAY"] else "AWAY"
            for lane, line in (("FAVOURITE_DNB", 0.0), ("FAVOURITE_WIN", -0.5 if fav == "HOME" else 0.5)):
                row = next(r for r in market if r["line"] == line and r["selection"] == fav)
                add(lane, row)
        if event_id in balanced:
            b = balanced[event_id]
            add("BALANCED", {"line": b["line"], "selection": b["selection"], "q": b["q"], "fair": b["fair"]})
    return lanes


def _stake(state: str) -> tuple[float, float]:
    return rep._STAKE_SHARE[state]


def lane_summary(picks: Sequence[Mapping[str, Any]], eligible: int) -> dict[str, Any]:
    c = rep.Cohort()
    for p in picks:
        c.add(p["state"], p["q"], p["fair"])
    out = c.to_dict(eligible)
    mq = [p["market_q"] for p in picks if p.get("market_q") is not None]
    out["mean_market_q"] = sum(mq) / len(mq) if mq else None
    return out


def _log_loss(samples):
    tot = wt = 0.0
    for q, w, l in samples:
        q = min(max(q, 1e-9), 1 - 1e-9)
        tot += -(w * math.log(q) + l * math.log(1 - q))
        wt += w + l
    return tot / wt if wt else None


def paired_model_vs_market(picks: Sequence[Mapping[str, Any]], *, seed: int = 7) -> dict[str, Any]:
    """Model q vs market q on the identical bets; bootstrap clustered by match date."""
    rows = [p for p in picks if p.get("market_q") is not None]
    by_date: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for p in rows:
        by_date[p["date"]].append(p)

    def diff(sample):
        m = [(p["q"], *_stake(p["state"])) for p in sample]
        k = [(p["market_q"], *_stake(p["state"])) for p in sample]
        lm, lk = _log_loss(m), _log_loss(k)
        return None if lm is None or lk is None else lm - lk

    point = diff(rows)
    rng = random.Random(seed)
    dates = list(by_date)
    draws = []
    for _ in range(BOOTSTRAP_DRAWS if dates else 0):
        sample = [p for d in (rng.choice(dates) for _ in dates) for p in by_date[d]]
        d = diff(sample)
        if d is not None:
            draws.append(d)
    draws.sort()
    lo = draws[int(0.025 * len(draws))] if draws else None
    hi = draws[int(0.975 * len(draws)) - 1] if draws else None
    return {
        "n": len(rows),
        "model_log_loss": _log_loss([(p["q"], *_stake(p["state"])) for p in rows]),
        "market_log_loss": _log_loss([(p["market_q"], *_stake(p["state"])) for p in rows]),
        "log_loss_difference_model_minus_market": point,
        "difference_ci95_cluster_bootstrap_by_date": [lo, hi],
        "interpretation": "negative = model better than market on the same bets",
    }


def run_benchmark(
    *,
    day10_prices: Path,
    research_db: Path,
    selection_seasons: Sequence[str] = rep.DEFAULT_SELECTION_SEASONS,
    holdout_seasons: Sequence[str] = rep.DEFAULT_HOLDOUT_SEASONS,
    rows_by_fixture: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    fixtures: Mapping[str, rep.Fixture] | None = None,
    history: Mapping[str, int] | None = None,
    match_dates: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    fixtures = fixtures if fixtures is not None else rep.load_fixtures(research_db)
    rows_by_fixture = rows_by_fixture if rows_by_fixture is not None else rep.load_day10_rows(day10_prices, markets=("asian_handicap", "corner_total"))
    history = history if history is not None else venue_history(research_db)
    if match_dates is None:
        conn = sqlite3.connect(f"file:{Path(research_db).as_posix()}?mode=ro", uri=True)
        match_dates = {e: d for e, d in conn.execute("SELECT event_id, match_date FROM football_events")}
        conn.close()
    out: dict[str, Any] = {
        "experiment_version": EXPERIMENT_VERSION,
        "preregistered_threshold": PREREGISTERED_THRESHOLD,
        "min_venue_history": MIN_VENUE_HISTORY,
        "market_ah_method": "independent Poisson means solved to de-vigged Pinnacle closing 1X2 (approximation)",
        "periods": {},
    }
    for label, seasons in (("selection", set(selection_seasons)), ("holdout", set(holdout_seasons))):
        lanes = build_lanes(rows_by_fixture, fixtures, history, seasons=seasons, match_dates=match_dates)
        eligible = len(lanes.pop("_ELIGIBLE", []))
        period: dict[str, Any] = {"eligible_fixtures": eligible, "lanes": {}, "by_league": {}}
        for lane, picks in sorted(lanes.items()):
            period["lanes"][lane] = lane_summary(picks, eligible)
        period["paired_model_hc_vs_market"] = paired_model_vs_market(lanes.get("MODEL_HC", []))
        period["market_ah_calibration"] = rep.calibration(
            (p["market_q"], *_stake(p["state"])) for lane in ("MARKET_HC", "FAVOURITE_DNB", "FAVOURITE_WIN") for p in lanes.get(lane, [])
        )
        for comp in sorted({p["competition_id"] for p in lanes.get("MODEL_HC", [])}):
            period["by_league"][comp] = {
                lane: lane_summary([p for p in lanes.get(lane, []) if p["competition_id"] == comp], 0)
                for lane in ("MODEL_HC", "MARKET_HC")
            }
        period["exploratory_thresholds"] = {}
        for t in EXPLORATORY_THRESHOLDS:
            alt = build_lanes(rows_by_fixture, fixtures, history, seasons=seasons, threshold=t, match_dates=match_dates)
            n = len(alt.pop("_ELIGIBLE", []))
            period["exploratory_thresholds"][f"{t:.2f}"] = {
                "MODEL_HC": lane_summary(alt.get("MODEL_HC", []), n),
                "MARKET_HC": lane_summary(alt.get("MARKET_HC", []), n),
                "note": f"exploratory; {len(EXPLORATORY_THRESHOLDS)} extra thresholds tested, not used for any decision",
            }
        out["periods"][label] = period
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day10-prices", type=Path, required=True)
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = run_benchmark(day10_prices=args.day10_prices, research_db=args.research_db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True, default=float), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
