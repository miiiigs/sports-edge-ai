"""The 13-step daily pipeline skeleton with a runnable demo."""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents.market.devig import consensus, devig_shin
from ..agents.tennis.elo import EloBook, PlayerStrengthAgent
from ..agents.tennis.simulation import simulate_match
from ..evaluation.dashboard import write_dashboard_html
from ..config import settings
from ..evaluation.reporting import explain_assessment, summarize_outputs
from ..firewall import scan_for_market_contamination
from ..locking import canonical_hash, make_lock_row
from ..storage.paper_shadow_ledger import (
    OutcomeUpdate,
    PaperDecision,
    append_outcome_update,
    connect as connect_paper_shadow_ledger,
    fetch_ledger_rows,
    record_decision,
)


def step_gatekeeper(
    competition: str,
    model_p: float,
    p_lower: float,
    best_odds: float,
    consensus_p: float,
    data_quality: int,
    skeptic_verdict: str,
    risk_decision: str,
    market_signal: str,
) -> dict:
    """M10 decision policy - the V1 default rule set."""

    break_even = 1.0 / best_odds
    raw_ev = model_p * best_odds - 1.0

    # Uncertainty-adjusted: shrink model probability 40% toward consensus.
    adj_p = 0.6 * model_p + 0.4 * consensus_p
    adj_ev = adj_p * best_odds - 1.0

    min_ev = settings.min_adjusted_ev.get(competition, 0.05)
    dq_floor = settings.min_data_quality.get(competition, 75)

    if data_quality < 50 or data_quality < dq_floor:
        decision, reason = "PASS", f"data quality {data_quality} below floor {dq_floor}"
    elif skeptic_verdict == "VETO":
        decision, reason = "PASS", "skeptic veto"
    elif risk_decision == "REJECT":
        decision, reason = "PASS", "risk rejection"
    elif market_signal == "strongly_opposing":
        decision, reason = "WATCH", "market moving strongly against model"
    elif adj_ev >= min_ev and p_lower > break_even:
        decision, reason = (
            "BET_CANDIDATE",
            (
                f"adjusted EV {adj_ev:+.1%} >= {min_ev:.1%} and interval lower "
                f"bound {p_lower:.1%} clears break-even {break_even:.1%}"
            ),
        )
    elif raw_ev > 0:
        decision, reason = "WATCH", "positive raw EV but not robust to uncertainty"
    else:
        decision, reason = "PASS", "no edge"

    card = {
        "decision": decision,
        "reason": reason,
        "model": {
            "fair_probability": model_p,
            "fair_odds": round(1 / model_p, 2),
            "p_lower": p_lower,
        },
        "market_view": {
            "consensus_probability": consensus_p,
            "best_price": best_odds,
            "break_even_at_best": break_even,
            "market_signal": market_signal,
        },
        "value": {"raw_ev": raw_ev, "adjusted_ev": adj_ev},
        "prices": {
            "fair": round(1 / model_p, 2),
            "acceptable_at": round((1 / adj_p) * (1 + min_ev), 2),
            "current_best": best_odds,
        },
    }

    for word in settings.forbidden_decision_words:
        assert word not in str(card).lower(), f"forbidden word: {word}"

    return card


def build_demo_result() -> dict[str, Any]:
    """Run the synthetic demo and return the full result payload."""

    run_id = "run_demo_1"
    event = {"event_id": "evt_demo_ATP_A_B", "competition": "ATP"}

    # ---- ZONE A (blind) -------------------------------------------------
    book = EloBook()
    rng = random.Random(1)
    for _ in range(400):
        # Synthetic history: A is the stronger player.
        book.update("A", "B", rng.random() < 0.62)
        book.update("A", "C", rng.random() < 0.58)
        book.update("B", "C", rng.random() < 0.50)

    t01 = PlayerStrengthAgent(book)
    payload = {"event_id": event["event_id"], "player_a": {"player_id": "A"}, "player_b": {"player_id": "B"}}

    scan_for_market_contamination(payload)
    out01 = t01.run(payload)

    sim = simulate_match(pa_serve=0.66, pb_serve=0.62)

    model_p = 0.5 * out01.primary_score + 0.5 * sim["p_win_a"]
    p_lower = model_p - 0.05
    fair = {
        "market": "ML",
        "selection": "A",
        "probability": model_p,
        "fair_odds": round(1 / model_p, 2),
    }

    lock = make_lock_row(
        event["event_id"],
        run_id,
        [out01.model_dump(mode="json"), sim, fair],
        prev_hash=None,
    )

    # ---- ZONE B (market) -----------------------------------------------
    books = {
        "sharp_book": [1.68, 2.30],
        "soft_book": [1.62, 2.20],
        "exchange": [1.70, 2.36],
    }
    tiers = {"sharp_book": "sharp", "soft_book": "soft", "exchange": "exchange"}
    cons = consensus(books, tiers, devig_shin)
    best = max(book_odds[0] for book_odds in books.values())

    mlock = make_lock_row(
        event["event_id"],
        run_id,
        [{"consensus": cons, "best": best}],
        prev_hash=lock["payload_hash"],
    )

    # ---- ZONE C (decision) ---------------------------------------------
    card = step_gatekeeper(
        "ATP",
        model_p,
        p_lower,
        best,
        cons[0],
        data_quality=88,
        skeptic_verdict="NO_CRITICAL_OBJECTION",
        risk_decision="APPROVED",
        market_signal="neutral",
    )

    return {
        "run_id": run_id,
        "event": event,
        "agent_output": out01,
        "simulation": sim,
        "fair": fair,
        "prediction_lock": lock,
        "books": books,
        "consensus": cons,
        "best_price": best,
        "market_lock": mlock,
        "card": card,
        "input_hash": canonical_hash([lock, mlock, card]),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def print_demo_steps(result: dict[str, Any]) -> None:
    """Render the demo steps in the original terminal-friendly format."""

    out01 = result["agent_output"]
    sim = result["simulation"]
    fair = result["fair"]
    cons = result["consensus"]
    card = result["card"]

    print("STEP 1-2    fixtures + validation: 1 synthetic ATP event")
    print(f"STEP 3       T01 blind: p(A)={out01.primary_score:.3f} status={out01.status.value}")
    print(f"STEP 4       T19 simulation: p(A)={sim['p_win_a']:.3f}")
    print(f"STEP 5       T20 fair price: {fair}")
    print(f"STEP 6       PREDICTION LOCK {result['prediction_lock']['payload_hash'][:16]} ({result['timestamp']})")
    print(f"STEP 7       M01-M05: consensus p(A)={cons[0]:.3f}, best price {result['best_price']}")
    print(f"STEP 8       MARKET LOCK {result['market_lock']['payload_hash'][:16]}")
    print(
        f"STEP 9-10    M06/M07: model {fair['probability']:.1%} vs consensus {cons[0]:.1%} "
        f"({(fair['probability'] - cons[0]) * 100:+.1f} pts)"
    )
    print("STEP 11-12   skeptic: no veto; risk: approved")
    print(f"STEP 13      GATEKEEPER: {card['decision']} - {card['reason']}")
    print(f"              input_hash={result['input_hash'][:16]}")


def log_demo_result(
    result: dict[str, Any],
    ledger_db_path: str | Path,
    *,
    closing_price: float | None = None,
    settlement: str | None = None,
) -> dict[str, Any]:
    """Record the demo result in the local paper/shadow ledger."""

    event = result["event"]
    fair = result["fair"]
    card = result["card"]
    competition = event["competition"]
    market_probability = result["consensus"][0]
    edge = fair["probability"] - market_probability

    paper_decision = PaperDecision(
        run_id=result["run_id"],
        event_id=event["event_id"],
        sport="tennis",
        competition=competition,
        market=fair["market"],
        selection=fair["selection"],
        available_price=result["best_price"],
        bookmaker="exchange",
        fair_probability=fair["probability"],
        fair_odds=fair["fair_odds"],
        market_probability=market_probability,
        edge=edge,
        data_quality=88,
        uncertainty=0.05,
        decision=card["decision"],
        decision_reason=card["reason"],
        model_version=result["agent_output"].model_version,
        strategy_version="demo_strategy_v1",
        policy_version="gatekeeper_v1",
        notes=f"prediction_lock={result['prediction_lock']['payload_hash'][:16]} market_lock={result['market_lock']['payload_hash'][:16]}",
    )

    ledger_path = Path(ledger_db_path)
    with connect_paper_shadow_ledger(ledger_path) as conn:
        decision_id = record_decision(conn, paper_decision)
        if closing_price is not None:
            append_outcome_update(
                conn,
                OutcomeUpdate(
                    decision_id=decision_id,
                    result_status="PENDING" if settlement is None else "SETTLED",
                    closing_price=closing_price,
                    notes="captured closing price",
                ),
            )
        if settlement is not None:
            append_outcome_update(
                conn,
                OutcomeUpdate(
                    decision_id=decision_id,
                    result_status="SETTLED",
                    settlement=settlement,
                    notes="captured settlement",
                ),
            )
        rows = fetch_ledger_rows(conn)

    summary = summarize_outputs(rows)
    explanation = explain_assessment(summary)
    return {
        "decision_id": decision_id,
        "ledger_db_path": str(ledger_path),
        "rows": rows,
        "summary": summary,
        "explanation": explanation,
    }


def run_demo() -> None:
    """Run the original demo without persistence."""

    result = build_demo_result()
    print_demo_steps(result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sports AI daily pipeline demo")
    parser.add_argument("--demo", action="store_true", help="run the synthetic daily pipeline demo")
    parser.add_argument("--ledger-db", help="optional SQLite paper/shadow ledger path")
    parser.add_argument("--closing-price", type=float, help="optional closing price for grading")
    parser.add_argument("--settlement", help="optional settlement like WIN or LOSS")
    parser.add_argument("--dashboard-html", help="optional HTML dashboard output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.demo and not args.dashboard_html:
        parser.print_help()
        return 1

    if args.demo:
        result = build_demo_result()
        print_demo_steps(result)

        if args.ledger_db:
            logged = log_demo_result(
                result,
                args.ledger_db,
                closing_price=args.closing_price,
                settlement=args.settlement,
            )
            print()
            print(f"LEDGER      recorded decision_id={logged['decision_id']} into {logged['ledger_db_path']}")
            print(f"SUMMARY     candidates={logged['summary']['funnel']['BET_CANDIDATE']} total_rows={logged['summary']['total_rows']}")
            for line in logged["explanation"]:
                print(f"ASSESS      {line}")

    if args.dashboard_html:
        if not args.ledger_db:
            parser.error("--dashboard-html requires --ledger-db")
        dashboard = write_dashboard_html(args.ledger_db, args.dashboard_html)
        print(f"DASHBOARD   wrote {dashboard['row_count']} rows to {dashboard['output_path']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
