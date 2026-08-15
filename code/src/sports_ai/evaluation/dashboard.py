"""Generate a client-facing HTML dashboard for live paper/shadow review."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from ..storage.paper_shadow_ledger import connect as connect_paper_shadow_ledger
from ..storage.paper_shadow_ledger import fetch_ledger_rows
from .reporting import INVALIDATION_MARKERS, explain_assessment, summarize_outputs


def _fmt_pct(value: float | None, *, digits: int = 1, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    sign = "+" if signed else ""
    return f"{value:{sign}.{digits}%}"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def _fmt_odds(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _fmt_pct_whole(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.0%}"


def _fmt_units(value: float | None) -> str:
    if value is None:
        return "Pending"
    return f"{value:+.2f}u"


def _fmt_timestamp(value: str | None) -> str:
    if not value:
        return "N/A"
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")


def _short_hash(value: str | None) -> str:
    if not value:
        return "N/A"
    return value[:12]


def _is_invalidated_row(row: dict[str, Any]) -> bool:
    text = f"{row.get('notes') or ''} {row.get('outcome_notes') or ''}"
    return any(marker in text for marker in INVALIDATION_MARKERS)


def _settlement_units(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    settlement = str(row.get("settlement") or "").upper()
    price = row.get("available_price")
    if not price:
        return None
    if settlement == "WIN":
        return float(price) - 1.0
    if settlement == "LOSS":
        return -1.0
    if settlement in {"VOID", "CANCELLED", "PUSH"}:
        return 0.0
    if settlement == "HALF_WIN":
        return (float(price) - 1.0) / 2.0
    if settlement == "HALF_LOSS":
        return -0.5
    return None


def _read_json(path: Path | None) -> Any | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _watchlist_state_path(ledger_db_path: str | Path) -> Path:
    ledger_path = Path(ledger_db_path)
    return ledger_path.with_name(f"{ledger_path.stem}_watchlist_state.json")


def _find_research_dir(*paths: Path) -> Path | None:
    candidates: list[Path] = []
    for path in paths:
        for base in [path.parent, *path.parents]:
            candidates.append(base / "football_research")
            candidates.append(base / "research" / "football_research")
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _latest_matching_file(directory: Path | None, pattern: str) -> Path | None:
    if directory is None:
        return None
    matches = sorted(directory.glob(pattern))
    return matches[-1] if matches else None


def _load_model_progress(ledger_path: Path, output_path: Path) -> dict[str, Any]:
    research_dir = _find_research_dir(ledger_path, output_path)
    selection = _read_json(_latest_matching_file(research_dir, "track_b_model_selection_*.json")) or {}
    finalization = _read_json(_latest_matching_file(research_dir, "track_b_finalization_summary_v1_vs_v3_*.json")) or {}
    v4_development = _read_json(_latest_matching_file(research_dir, "track_b_development_v4*.json")) or {}
    if not selection and not finalization and not v4_development:
        return {
            "available": False,
            "research_dir": str(research_dir) if research_dir else None,
            "items": [],
            "note": "Model research artifacts are not available beside this dashboard export yet.",
        }

    validation = selection.get("validation", {})
    testing = selection.get("test", {})
    v1_validation = validation.get("poisson_1x2_v1", {})
    v2_validation = validation.get("poisson_1x2_v2", {})
    v1_test = testing.get("poisson_1x2_v1", {})
    v2_test = testing.get("poisson_1x2_v2", {})
    v3_summary = finalization.get("v3", {})
    v1_holdout = finalization.get("v1", {})
    v4_validation = v4_development.get("validation", {}).get("poisson_1x2_v4", {})
    v4_assessment = v4_development.get("promotion_assessment", {}).get("poisson_1x2_v4", {})

    items = [
        {
            "code": "v1",
            "title": "Simple Poisson",
            "status": "Baseline",
            "headline": "Original benchmark used to judge all later football changes.",
            "metrics": [
                ("Validation log loss", _fmt_num(v1_validation.get("model_log_loss"), 5)),
                ("Validation Brier", _fmt_num(v1_validation.get("model_brier"), 5)),
                ("Holdout log loss", _fmt_num(v1_holdout.get("model_log_loss") or v1_test.get("model_log_loss"), 5)),
                ("Holdout Brier", _fmt_num(v1_holdout.get("model_brier") or v1_test.get("model_brier"), 5)),
            ],
        },
        {
            "code": "v2",
            "title": "Recency Poisson",
            "status": "Rejected",
            "headline": "Recency weighting made the probability layer worse than the baseline.",
            "metrics": [
                ("Validation log loss", _fmt_num(v2_validation.get("model_log_loss"), 5)),
                ("Validation Brier", _fmt_num(v2_validation.get("model_brier"), 5)),
                ("Holdout log loss", _fmt_num(v2_test.get("model_log_loss"), 5)),
                ("Holdout Brier", _fmt_num(v2_test.get("model_brier"), 5)),
            ],
        },
        {
            "code": "v3",
            "title": "Opponent-Adjusted Poisson",
            "status": "Promoted",
            "headline": "First version to improve the forecasting layer on validation and holdout.",
            "metrics": [
                ("Holdout log loss", _fmt_num(v3_summary.get("model_log_loss"), 5)),
                ("Holdout Brier", _fmt_num(v3_summary.get("model_brier"), 5)),
                ("Delta vs v1 log loss", _fmt_num(finalization.get("deltas_v3_minus_v1", {}).get("log_loss"), 5)),
                ("Delta vs v1 Brier", _fmt_num(finalization.get("deltas_v3_minus_v1", {}).get("brier"), 5)),
            ],
            "note": f"Status: {finalization.get('classification', 'PROMOTE')}. Holdout status: {finalization.get('holdout_status', 'N/A')}.",
        },
        {
            "code": "v4",
            "title": "Dixon-Coles on v3 Strengths",
            "status": "Under Finalization",
            "headline": "Validation-only improvement exists, but promotion still needs controlled confirmation.",
            "metrics": [
                ("Validation log loss", _fmt_num(v4_validation.get("model_log_loss"), 5)),
                ("Validation Brier", _fmt_num(v4_validation.get("model_brier"), 5)),
                (
                    "Vs v3 log loss",
                    _fmt_num(
                        (v4_validation.get("model_log_loss") or 0.0)
                        - (v4_development.get("validation", {}).get("poisson_1x2_v3", {}).get("model_log_loss") or 0.0),
                        5,
                    )
                    if v4_validation
                    else "N/A",
                ),
                (
                    "Eligible for finalization",
                    "Yes" if v4_assessment.get("eligible_for_finalization") else "No",
                ),
            ],
            "note": "Validation improved slightly; ROI remains secondary and is not used for promotion.",
        },
    ]
    return {
        "available": True,
        "research_dir": str(research_dir) if research_dir else None,
        "items": items,
        "current_live_model": "v3",
        "next_model": "v4",
    }


def _selection_label(value: str | None) -> str:
    labels = {"home": "Home", "draw": "Draw", "away": "Away"}
    if not value:
        return "N/A"
    return labels.get(value.lower(), value.replace("_", " ").title())


def _confidence_label(card: dict[str, Any]) -> str:
    prediction = card.get("prediction") or {}
    data_quality = prediction.get("data_quality")
    uncertainty = prediction.get("uncertainty")
    if card.get("late_market_snapshot"):
        return "Invalid"
    if data_quality is None or uncertainty is None:
        return "Unrated"
    if data_quality >= 0.84 and uncertainty <= 0.97:
        return "High"
    if data_quality >= 0.76 and uncertainty <= 1.0:
        return "Moderate"
    return "Guarded"


def _risk_flags(card: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    prediction = card.get("prediction") or {}
    details = prediction.get("details") or {}
    if card.get("late_market_snapshot"):
        flags.append("Late market snapshot was blocked from pre-match use.")
    if details.get("converged") is False:
        flags.append("Model fit did not fully converge, so confidence is reduced.")
    if card.get("market_price_source_tier") == "SECONDARY_BOOKMAKER_LISTING":
        flags.append("Price came from a secondary bookmaker listing and should be checked against the book before real staking.")
    if not flags:
        flags.append("No critical timing or audit flags.")
    return flags


def _status_class(value: str | None) -> str:
    mapping = {
        "PAPER_SHADOW_CANDIDATE": "good",
        "BET_CANDIDATE": "good",
        "WATCH": "watch",
        "PASS": "muted",
    }
    return mapping.get(value or "", "muted")


def _load_watchlist_payload(ledger_db_path: str | Path) -> dict[str, Any] | None:
    payload = _read_json(_watchlist_state_path(ledger_db_path))
    return payload if isinstance(payload, dict) else None


def _find_live_row_for_card(rows: list[dict[str, Any]], card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not card:
        return None
    event_id = card.get("event_id")
    if not event_id:
        return None
    matching_rows = [row for row in rows if row.get("event_id") == event_id and not _is_invalidated_row(row)]
    if matching_rows:
        return matching_rows[-1]
    matching_rows = [row for row in rows if row.get("event_id") == event_id]
    return matching_rows[-1] if matching_rows else None


def _featured_live_context(watchlist_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not watchlist_payload:
        return {
            "available": False,
            "result": {},
            "cards": [],
            "predicted_cards": [],
            "candidate_cards": [],
            "status_counts": {},
            "featured_card": None,
        }

    result = watchlist_payload.get("result") or {}
    cards = result.get("cards") or []
    candidate_cards = [
        card
        for card in cards
        if card.get("status") in {"PAPER_SHADOW_CANDIDATE", "BET_CANDIDATE"}
    ]
    predicted_cards = [
        card
        for card in cards
        if card.get("prediction") or card.get("late_market_snapshot") or card.get("market_captured_at")
    ]
    status_counts = Counter(card.get("status", "UNKNOWN") for card in cards)
    featured = candidate_cards[0] if candidate_cards else (predicted_cards[0] if predicted_cards else None)
    return {
        "available": True,
        "saved_at": watchlist_payload.get("saved_at"),
        "result": result,
        "cards": cards,
        "predicted_cards": predicted_cards,
        "candidate_cards": candidate_cards,
        "status_counts": status_counts,
        "featured_card": featured,
    }


def _render_status_strip(summary: dict[str, Any], live_context: dict[str, Any]) -> str:
    live_result = live_context.get("result") or {}
    funnel = live_result.get("funnel") or {}
    cards = [
        ("Verified Fixtures", str(funnel.get("fixtures_discovered", 0))),
        ("Blind Predictions", str(funnel.get("blind_prediction_possible", 0))),
        ("Market Snapshots", str(funnel.get("market_prices_available", 0))),
        ("Valid Candidates", str(funnel.get("final_paper_shadow_candidates", 0))),
        ("Late Snapshots Rejected", str(funnel.get("market_snapshots_rejected_late", 0))),
        ("Invalidated Audit Rows", str(summary.get("audit", {}).get("invalidated_rows", 0))),
    ]
    return "\n".join(
        f'<div class="stat-card"><div class="eyebrow">{escape(label)}</div><div class="stat">{escape(value)}</div></div>'
        for label, value in cards
    )


def _render_featured_card(card: dict[str, Any] | None) -> str:
    if not card:
        return """
        <section class="panel">
          <div class="panel-head">
            <div><h2>Today's Live Result</h2></div>
          </div>
          <p class="muted-copy">No live football watchlist state has been saved yet for this export.</p>
        </section>
        """

    candidate = card.get("candidate") or {}
    prediction = card.get("prediction") or {}
    probabilities = prediction.get("probabilities") or {}
    market_report = card.get("market_report") or {}
    devig_probabilities = market_report.get("devig_probabilities") or {}
    available_price = candidate.get("available_price")
    selected = candidate.get("selection")
    fair_probability = candidate.get("fair_probability") or probabilities.get(selected)
    fair_odds = candidate.get("fair_odds")
    market_break_even = (1.0 / available_price) if available_price else None
    devig_market_probability = candidate.get("devig_market_probability") or devig_probabilities.get(selected)
    robust_edge = candidate.get("uncertainty_adjusted_edge")
    recommendation = _selection_label(candidate.get("selection"))
    risk_lines = "".join(f"<li>{escape(flag)}</li>" for flag in _risk_flags(card))
    status = card.get("status") or "PASS"
    return f"""
    <section class="panel hero-panel">
      <div class="panel-head">
        <div>
          <div class="eyebrow">Today's live result</div>
          <h2>{escape(card.get("home_team", "Unknown"))} vs {escape(card.get("away_team", "Unknown"))}</h2>
        </div>
        <span class="badge badge-{_status_class(status)}">{escape(status.replace("_", " "))}</span>
      </div>
      <div class="hero-grid">
        <div class="match-callout">
          <div class="callout-title">Recommendation</div>
          <div class="callout-value">{escape(recommendation)} - PAPER/SHADOW</div>
          <p class="muted-copy">{escape(card.get("reason") or "No qualifying edge yet.")}</p>
        </div>
        <div class="metric-grid">
          <div class="metric"><span>Bookmaker odds</span><strong>{escape(_fmt_odds(available_price))}</strong></div>
          <div class="metric"><span>Fair odds</span><strong>{escape(_fmt_odds(fair_odds))}</strong></div>
          <div class="metric"><span>Model probability</span><strong>{escape(_fmt_pct(fair_probability, digits=1))}</strong></div>
          <div class="metric"><span>Market break-even</span><strong>{escape(_fmt_pct(market_break_even, digits=1))}</strong></div>
          <div class="metric"><span>De-vig market probability</span><strong>{escape(_fmt_pct(devig_market_probability, digits=1))}</strong></div>
          <div class="metric"><span>Edge</span><strong>{escape(_fmt_pct(candidate.get("edge"), digits=2, signed=True))}</strong></div>
          <div class="metric"><span>Robust edge</span><strong>{escape(_fmt_pct(robust_edge, digits=2, signed=True))}</strong></div>
          <div class="metric"><span>Data quality</span><strong>{escape(_fmt_pct_whole(prediction.get("data_quality")))}</strong></div>
          <div class="metric"><span>Confidence</span><strong>{escape(_confidence_label(card))}</strong></div>
          <div class="metric"><span>Kickoff</span><strong>{escape(_fmt_timestamp(card.get("kickoff_at_utc")))}</strong></div>
          <div class="metric"><span>Reason code</span><strong>{escape(card.get("reason_code") or candidate.get("reason_code") or "N/A")}</strong></div>
        </div>
      </div>
      <div class="detail-grid">
        <div class="detail-block">
          <h3>Why it qualified</h3>
          <ul class="detail-list">
            <li>Independent model probability: {escape(_fmt_pct(fair_probability, digits=1))}</li>
            <li>Bookmaker price: {escape(_fmt_odds(available_price))}</li>
            <li>Calculated edge: {escape(_fmt_pct(candidate.get("edge"), digits=2, signed=True))}</li>
            <li>Model vs de-vig market: {escape(_fmt_pct(candidate.get("model_vs_devig_market"), digits=2, signed=True))}</li>
            <li>Expected value: {escape(_fmt_pct(candidate.get("expected_value"), digits=2, signed=True))}</li>
            <li>Bookmaker overround: {escape(_fmt_pct(market_report.get("bookmaker_overround"), digits=1))}</li>
            <li>Price source tier: {escape(card.get("market_price_source_tier") or "N/A")}</li>
          </ul>
        </div>
        <div class="detail-block">
          <h3>Risk flags</h3>
          <ul class="detail-list">
            {risk_lines}
          </ul>
        </div>
      </div>
    </section>
    """


def _render_lifecycle_panel(card: dict[str, Any] | None, ledger_row: dict[str, Any] | None) -> str:
    if not card:
        return """
        <section class="panel">
          <div class="panel-head">
            <div><h2>Candidate Lifecycle</h2></div>
          </div>
          <p class="muted-copy">Once a live candidate exists, this panel will show entry, close, CLV, and settlement from the paper/shadow ledger.</p>
        </section>
        """

    entry_price = (ledger_row or {}).get("available_price")
    closing_price = (ledger_row or {}).get("closing_price")
    clv = (ledger_row or {}).get("clv")
    settlement = (ledger_row or {}).get("settlement")
    unit_result = _settlement_units(ledger_row)
    lifecycle_state = "Waiting for closing-price capture and final result."
    interpretation = "The candidate is live and locked, but the evidence is not graded yet."
    if closing_price is not None and settlement:
        lifecycle_state = "Closing price and result captured."
    elif closing_price is not None:
        lifecycle_state = "Closing price captured; waiting for final result."
    elif settlement:
        lifecycle_state = "Result captured; waiting for close confirmation."

    if clv is None:
        clv_label = "Pending"
    elif clv > 0:
        clv_label = f"Positive ({_fmt_pct(clv, digits=2, signed=True)})"
        interpretation = "The system obtained a better price than the closing market."
    elif clv < 0:
        clv_label = f"Negative ({_fmt_pct(clv, digits=2, signed=True)})"
        interpretation = "The closing market moved against the original entry price."
    else:
        clv_label = "Flat (0.00%)"
        interpretation = "The entry price matched the closing market."

    if settlement == "WIN":
        interpretation += " The paper result won."
    elif settlement == "LOSS":
        interpretation += " The paper result lost."
    elif settlement in {"VOID", "CANCELLED", "PUSH"}:
        interpretation += " The paper result was voided."

    return f"""
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Candidate Lifecycle</h2>
          <p class="muted-copy">This is the full evidence chain the client ultimately cares about: candidate, price capture, closing line, result, and interpretation.</p>
        </div>
      </div>
      <div class="results-grid">
        <div class="stat-card"><div class="eyebrow">Entry Price</div><div class="stat">{escape(_fmt_odds(entry_price))}</div></div>
        <div class="stat-card"><div class="eyebrow">Closing Price</div><div class="stat">{escape(_fmt_odds(closing_price))}</div></div>
        <div class="stat-card"><div class="eyebrow">CLV</div><div class="stat">{escape(clv_label)}</div></div>
        <div class="stat-card"><div class="eyebrow">Result</div><div class="stat">{escape(settlement or 'Pending')}</div></div>
        <div class="stat-card"><div class="eyebrow">Paper Result</div><div class="stat">{escape(_fmt_units(unit_result))}</div></div>
        <div class="stat-card"><div class="eyebrow">Lifecycle Status</div><div class="stat">{escape(lifecycle_state)}</div></div>
      </div>
      <div class="detail-grid">
        <div class="detail-block">
          <h3>Interpretation</h3>
          <p class="muted-copy">{escape(interpretation)}</p>
        </div>
        <div class="detail-block">
          <h3>Operator Reference</h3>
          <ul class="detail-list">
            <li>Decision ID: {escape(str((ledger_row or {}).get('decision_id') or 'Not logged'))}</li>
            <li>Outcome updated: {escape(_fmt_timestamp((ledger_row or {}).get('outcome_updated_at')))}</li>
            <li>Created: {escape(_fmt_timestamp((ledger_row or {}).get('created_at')))}</li>
          </ul>
        </div>
      </div>
    </section>
    """


def _render_audit_trail(card: dict[str, Any] | None) -> str:
    if not card:
        return """
        <section class="panel">
          <div class="panel-head">
            <div><h2>System Audit Trail</h2></div>
          </div>
          <p class="muted-copy">A locked live candidate will show the prediction, market, and decision chain here.</p>
        </section>
        """

    steps = [
        (
            "Sports analysis complete",
            f"Blind probabilities generated before market comparison. Home probability {_fmt_pct((card.get('prediction') or {}).get('probabilities', {}).get('home'), digits=1)}.",
        ),
        (
            "Prediction locked",
            f"Prediction hash {_short_hash(card.get('prediction_lock_hash'))}.",
        ),
        (
            "Market captured",
            f"Bet365 snapshot {_fmt_odds((card.get('candidate') or {}).get('available_price'))} captured {_fmt_timestamp(card.get('market_captured_at'))}.",
        ),
        (
            "Market locked",
            f"Market hash {_short_hash(card.get('market_lock_hash'))}.",
        ),
        (
            "Final decision recorded",
            f"Status {(card.get('status') or 'PASS').replace('_', ' ')} because {card.get('reason', 'no reason recorded')}.",
        ),
    ]
    items = "".join(
        f"""
        <li class="trail-step">
          <div class="trail-bullet"></div>
          <div>
            <strong>{escape(title)}</strong>
            <p>{escape(detail)}</p>
          </div>
        </li>
        """
        for title, detail in steps
    )
    return f"""
    <section class="panel">
      <div class="panel-head">
        <div><h2>System Audit Trail</h2></div>
      </div>
      <ul class="trail">
        {items}
      </ul>
    </section>
    """


def _render_watchlist_table(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return '<p class="muted-copy">No analyzed matches are available yet.</p>'

    rows: list[str] = []
    for card in cards:
        prediction = card.get("prediction") or {}
        candidate = card.get("candidate") or {}
        probabilities = prediction.get("probabilities") or {}
        market_report = card.get("market_report") or {}
        risk = "; ".join(_risk_flags(card))
        best_edge = (
            f"{_selection_label(candidate.get('selection'))} {_fmt_pct(candidate.get('edge'), digits=2, signed=True)} / robust {_fmt_pct(candidate.get('uncertainty_adjusted_edge'), digits=2, signed=True)}"
            if candidate
            else "N/A"
        )
        rows.append(
            f"""
            <tr>
              <td>
                <strong>{escape(card.get("home_team", "Unknown"))} vs {escape(card.get("away_team", "Unknown"))}</strong>
                <div class="row-sub">{escape(card.get("competition_name") or card.get("competition_id") or "Unknown competition")}</div>
              </td>
              <td><span class="badge badge-{_status_class(card.get("status"))}">{escape((card.get("status") or "PASS").replace("_", " "))}</span></td>
              <td>{escape(card.get("reason") or "N/A")}</td>
              <td>{escape(_fmt_pct(probabilities.get("home"), digits=1))} / {escape(_fmt_pct(probabilities.get("draw"), digits=1))} / {escape(_fmt_pct(probabilities.get("away"), digits=1))}</td>
              <td>{escape(best_edge)}</td>
              <td>{escape(card.get("reason_code") or "N/A")}</td>
              <td>{escape(_fmt_pct(market_report.get("bookmaker_overround"), digits=1))}</td>
              <td>{escape(_fmt_pct_whole(prediction.get("data_quality")))}</td>
              <td>{escape(_fmt_timestamp(card.get("market_captured_at")))}</td>
              <td>{escape(risk)}</td>
            </tr>
            """
        )
    return f"""
    <table class="data-table">
      <thead>
        <tr>
          <th>Match</th>
          <th>Status</th>
          <th>Reason</th>
          <th>Model H / D / A</th>
          <th>Best Edge</th>
          <th>Reason Code</th>
          <th>Overround</th>
          <th>Data Quality</th>
          <th>Market Time</th>
          <th>Remarks</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def _render_results_cards(summary: dict[str, Any], live_context: dict[str, Any]) -> str:
    returns = summary["returns"]
    quality = summary["quality"]
    cards = [
        ("Paper/Shadow Candidates", str(summary["funnel"]["PAPER_SHADOW_CANDIDATE"])),
        ("Settled Candidate Bets", str(returns["n"])),
        ("Average Model Edge", _fmt_pct(quality.get("avg_edge"), digits=2, signed=True)),
        ("Average CLV", _fmt_pct(summary["clv"]["mean"], digits=2, signed=True)),
        ("Win Rate", _fmt_pct(returns.get("win_rate"), digits=1)),
        ("Current Live Model", str((live_context.get("result") or {}).get("model_name") or "N/A")),
    ]
    return "\n".join(
        f'<div class="stat-card"><div class="eyebrow">{escape(label)}</div><div class="stat">{escape(value)}</div></div>'
        for label, value in cards
    )


def _render_assessment_lines(lines: list[str]) -> str:
    return "".join(f"<li>{escape(line)}</li>" for line in lines)


def _render_ledger_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted-copy">No paper/shadow ledger entries have been recorded yet.</p>'

    body_parts: list[str] = []
    for row in rows:
        invalidated = _is_invalidated_row(row)
        body_parts.append(
            f"""
            <tr>
              <td>{escape(_fmt_timestamp(row.get("created_at")))}</td>
              <td>{escape(row.get("event_id") or "N/A")}</td>
              <td><span class="badge badge-{_status_class(row.get("decision"))}">{escape((row.get("decision") or "UNKNOWN").replace("_", " "))}</span></td>
              <td>{escape(_selection_label(row.get("selection")))}</td>
              <td>{escape(_fmt_odds(row.get("available_price")))}</td>
              <td>{escape(_fmt_odds(row.get("closing_price")))}</td>
              <td>{escape(_fmt_pct(row.get("edge"), digits=2, signed=True))}</td>
              <td>{escape(row.get("settlement") or row.get("result_status") or "N/A")}</td>
              <td>{escape("Invalidated" if invalidated else (row.get("outcome_notes") or row.get("notes") or "Active / pending"))}</td>
            </tr>
            """
        )
    return f"""
    <table class="data-table">
      <thead>
        <tr>
          <th>Created</th>
          <th>Event</th>
          <th>Decision</th>
          <th>Selection</th>
          <th>Entry</th>
          <th>Close</th>
          <th>Edge</th>
          <th>Settlement</th>
          <th>Remarks</th>
        </tr>
      </thead>
      <tbody>
        {''.join(body_parts)}
      </tbody>
    </table>
    """


def _render_model_progress(progress: dict[str, Any]) -> str:
    if not progress.get("available"):
        return f"""
        <section class="panel">
          <div class="panel-head">
            <div><h2>Model Progress</h2></div>
          </div>
          <p class="muted-copy">{escape(progress.get("note") or "No model research artifacts found.")}</p>
        </section>
        """

    cards = []
    for item in progress["items"]:
        metrics = "".join(
            f"<li><span>{escape(label)}</span><strong>{escape(value)}</strong></li>"
            for label, value in item["metrics"]
        )
        badge_status = (
            "PAPER_SHADOW_CANDIDATE"
            if item["status"] == "Promoted"
            else "WATCH"
            if item["status"] == "Under Finalization"
            else "PASS"
        )
        cards.append(
            f"""
            <article class="model-card">
              <div class="panel-head">
                <div>
                  <div class="eyebrow">{escape(item['code'])}</div>
                  <h3>{escape(item['title'])}</h3>
                </div>
                <span class="badge badge-{_status_class(badge_status)}">{escape(item['status'])}</span>
              </div>
              <p class="muted-copy">{escape(item['headline'])}</p>
              <ul class="metric-list">
                {metrics}
              </ul>
              <p class="row-sub">{escape(item.get('note') or '')}</p>
            </article>
            """
        )
    return f"""
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Track B Model Progress</h2>
          <p class="muted-copy">Research structure is now clean: v1 baseline, v2 rejected, v3 promoted, v4 still separate.</p>
        </div>
      </div>
      <div class="model-grid">
        {''.join(cards)}
      </div>
    </section>
    """


def _render_process_panel() -> str:
    return """
    <section class="panel">
      <div class="panel-head">
        <div><h2>How the system thinks</h2></div>
      </div>
      <div class="flow-grid">
        <article class="flow-card">
          <div class="eyebrow">Sports analysis</div>
          <h3>Independent fair price</h3>
          <p>Team strength, match history, and score modelling produce a blind fair probability before bookmaker prices are considered.</p>
          <div class="lock-chip">Prediction lock</div>
        </article>
        <article class="flow-card">
          <div class="eyebrow">Bookmaker analysis</div>
          <h3>Market snapshot</h3>
          <p>Current 1X2 prices are captured, timestamped, and checked against kickoff timing so invalid late rows cannot leak in.</p>
          <div class="lock-chip">Market lock</div>
        </article>
        <article class="flow-card">
          <div class="eyebrow">Final decision</div>
          <h3>Pass / Watch / Candidate</h3>
          <p>The system compares fair price against market price, reviews edge and data quality, then records a final auditable status.</p>
          <div class="lock-chip">Decision recorded</div>
        </article>
      </div>
    </section>
    """


def _render_interpretation_panel(summary: dict[str, Any]) -> str:
    clv_mean = summary["clv"]["mean"]
    current_read = (
        _fmt_pct(clv_mean, digits=2, signed=True)
        if clv_mean is not None
        else "No closing-line sample yet. Use today primarily to confirm process discipline and logging quality."
    )
    return f"""
    <section class="panel">
      <div class="panel-head">
        <div><h2>How to judge results</h2></div>
      </div>
      <div class="detail-grid">
        <div class="detail-block">
          <h3>Good</h3>
          <ul class="detail-list">
            <li>Average CLV is positive across a growing real sample.</li>
            <li>Calibration and Brier stay stable instead of collapsing by league or outcome type.</li>
            <li>The system keeps rejecting bad or late data instead of forcing picks.</li>
          </ul>
        </div>
        <div class="detail-block">
          <h3>Too early or weak</h3>
          <ul class="detail-list">
            <li>Few or no settled observations means today is still evidence gathering.</li>
            <li>Negative CLV over time is a stronger warning than a short win streak.</li>
            <li>One live candidate proves the platform works operationally, not that the edge is proven.</li>
          </ul>
        </div>
      </div>
      <p class="muted-copy">Current CLV read: {escape(current_read)}</p>
    </section>
    """


def build_dashboard_html(
    rows: list[dict[str, Any]],
    *,
    watchlist_payload: dict[str, Any] | None = None,
    model_progress: dict[str, Any] | None = None,
) -> str:
    """Build a standalone client-friendly HTML report from ledger rows plus live context."""

    summary = summarize_outputs(rows)
    assessment = explain_assessment(summary)
    live_context = _featured_live_context(watchlist_payload)
    featured_card = live_context.get("featured_card")
    featured_row = _find_live_row_for_card(rows, featured_card)
    predicted_cards = live_context.get("predicted_cards", [])
    model_progress = model_progress or {"available": False, "items": [], "note": "No model artifacts found."}
    generated_at = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    live_result = live_context.get("result") or {}
    live_subtitle = (
        f"Live model: {live_result.get('model_name', 'N/A')} | "
        f"Run date: {live_result.get('target_date', 'N/A')} | "
        f"Saved: {_fmt_timestamp(live_context.get('saved_at'))}"
        if live_context.get("available")
        else "No live watchlist state is attached to this export yet."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sports Edge AI - Live Research Dashboard</title>
  <style>
    :root {{
      --ink: #171c1f;
      --muted: #5f6b71;
      --paper: #f8f5ee;
      --panel: rgba(255, 252, 246, 0.92);
      --line: #d6cec1;
      --accent: #0d5c63;
      --accent-soft: #dff0ef;
      --olive: #4d6b50;
      --sand: #c89d66;
      --rose: #9b4f3f;
      --shadow: 0 18px 40px rgba(32, 27, 18, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(200, 157, 102, 0.24), transparent 28%),
        radial-gradient(circle at top right, rgba(13, 92, 99, 0.18), transparent 24%),
        linear-gradient(180deg, #f2ede3 0%, var(--paper) 100%);
      font-family: "Trebuchet MS", "Gill Sans", "Segoe UI", sans-serif;
    }}
    h1, h2, h3 {{
      font-family: "Palatino Linotype", "Book Antiqua", Georgia, serif;
      margin: 0;
      letter-spacing: 0.01em;
    }}
    p {{
      margin: 0;
    }}
    .shell {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 28px 18px 42px;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(13, 92, 99, 0.96), rgba(23, 28, 31, 0.94));
      color: #f9f7f2;
      border-radius: 28px;
      padding: 28px;
      box-shadow: var(--shadow);
      overflow: hidden;
      position: relative;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -10% -40% auto;
      width: 340px;
      height: 340px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,255,255,0.14), transparent 65%);
      pointer-events: none;
    }}
    .hero-top {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .hero-copy {{
      max-width: 820px;
    }}
    .hero-copy h1 {{
      font-size: clamp(2rem, 4vw, 3.2rem);
      margin-bottom: 10px;
    }}
    .hero-copy p {{
      color: rgba(249, 247, 242, 0.84);
      line-height: 1.55;
      max-width: 760px;
    }}
    .hero-meta {{
      display: grid;
      gap: 8px;
      min-width: 240px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.12);
      backdrop-filter: blur(10px);
    }}
    .hero-meta strong {{
      font-size: 0.95rem;
    }}
    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 0.75rem;
      color: var(--muted);
    }}
    .hero .eyebrow {{
      color: rgba(249, 247, 242, 0.76);
    }}
    .tabs {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 18px 0 22px;
    }}
    .tab-button {{
      border: 1px solid var(--line);
      background: rgba(255, 252, 246, 0.8);
      color: var(--ink);
      padding: 10px 16px;
      border-radius: 999px;
      cursor: pointer;
      font-weight: 700;
      transition: transform 120ms ease, background 120ms ease, border-color 120ms ease;
    }}
    .tab-button:hover {{
      transform: translateY(-1px);
      border-color: var(--accent);
    }}
    .tab-button.active {{
      background: var(--accent);
      color: #f7f4ef;
      border-color: var(--accent);
    }}
    .tab-panel {{
      display: none;
      animation: fadeIn 180ms ease;
    }}
    .tab-panel.active {{
      display: block;
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .status-grid, .results-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .stat-card, .panel, .model-card, .flow-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }}
    .stat-card {{
      padding: 16px 18px;
    }}
    .stat {{
      margin-top: 8px;
      font-size: 1.8rem;
      font-weight: 800;
      color: var(--accent);
    }}
    .panel {{
      padding: 22px;
      margin-bottom: 18px;
    }}
    .panel-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}
    .muted-copy {{
      color: var(--muted);
      line-height: 1.55;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: 1.2fr 1.8fr;
      gap: 18px;
      align-items: stretch;
    }}
    .match-callout {{
      border-radius: 20px;
      padding: 18px;
      background: linear-gradient(180deg, rgba(13, 92, 99, 0.08), rgba(13, 92, 99, 0.02));
      border: 1px solid rgba(13, 92, 99, 0.16);
    }}
    .callout-title {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .callout-value {{
      font-size: 1.5rem;
      font-weight: 800;
      margin-bottom: 10px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      padding: 14px;
      border-radius: 18px;
      border: 1px solid rgba(13, 92, 99, 0.12);
      background: rgba(255, 255, 255, 0.55);
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 0.83rem;
      margin-bottom: 6px;
    }}
    .metric strong {{
      font-size: 1.1rem;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 18px;
    }}
    .detail-block {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.55);
      border: 1px solid rgba(13, 92, 99, 0.10);
    }}
    .detail-block h3 {{
      margin-bottom: 12px;
      font-size: 1.05rem;
    }}
    .detail-list, .assessment-list, .trail, .metric-list {{
      margin: 0;
      padding-left: 18px;
    }}
    .detail-list li, .assessment-list li {{
      margin-bottom: 8px;
      line-height: 1.45;
    }}
    .trail {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 14px;
    }}
    .trail-step {{
      display: grid;
      grid-template-columns: 18px 1fr;
      gap: 14px;
      align-items: flex-start;
    }}
    .trail-step p {{
      margin-top: 4px;
      color: var(--muted);
      line-height: 1.45;
    }}
    .trail-bullet {{
      width: 14px;
      height: 14px;
      border-radius: 50%;
      margin-top: 6px;
      background: linear-gradient(180deg, var(--sand), var(--accent));
      box-shadow: 0 0 0 5px rgba(13, 92, 99, 0.08);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-weight: 800;
      white-space: nowrap;
    }}
    .badge-good {{
      background: rgba(77, 107, 80, 0.12);
      color: var(--olive);
      border: 1px solid rgba(77, 107, 80, 0.22);
    }}
    .badge-watch {{
      background: rgba(200, 157, 102, 0.16);
      color: #875b24;
      border: 1px solid rgba(200, 157, 102, 0.26);
    }}
    .badge-muted {{
      background: rgba(95, 107, 113, 0.12);
      color: var(--muted);
      border: 1px solid rgba(95, 107, 113, 0.18);
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}
    .data-table th, .data-table td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid rgba(214, 206, 193, 0.75);
      vertical-align: top;
    }}
    .data-table th {{
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      background: rgba(255, 255, 255, 0.65);
      position: sticky;
      top: 0;
    }}
    .row-sub {{
      color: var(--muted);
      font-size: 0.82rem;
      margin-top: 4px;
      line-height: 1.35;
    }}
    .model-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
    }}
    .model-card {{
      padding: 18px;
    }}
    .metric-list {{
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }}
    .metric-list li {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      line-height: 1.4;
    }}
    .flow-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .flow-card {{
      padding: 18px;
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.72), rgba(255,255,255,0.9)),
        linear-gradient(135deg, rgba(200, 157, 102, 0.08), rgba(13, 92, 99, 0.08));
    }}
    .flow-card h3 {{
      margin: 8px 0 10px;
    }}
    .flow-card p {{
      color: var(--muted);
      line-height: 1.55;
    }}
    .lock-chip {{
      margin-top: 14px;
      display: inline-flex;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(13, 92, 99, 0.12);
      color: var(--accent);
      font-size: 0.78rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .footer-note {{
      margin-top: 16px;
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.94rem;
    }}
    @media (max-width: 980px) {{
      .hero-grid, .detail-grid, .flow-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 720px) {{
      .shell {{
        padding: 16px 12px 28px;
      }}
      .hero {{
        padding: 20px;
        border-radius: 22px;
      }}
      .panel, .model-card {{
        padding: 18px;
      }}
      .metric-grid {{
        grid-template-columns: 1fr;
      }}
      .data-table {{
        font-size: 0.86rem;
      }}
      .data-table th, .data-table td {{
        padding: 10px 8px;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-top">
        <div class="hero-copy">
          <div class="eyebrow">Sports Edge AI</div>
          <h1>Live Research Dashboard</h1>
          <p>
            This client view shows what the system is doing today, why a match qualified or failed,
            how the lock-and-audit chain works, and where the football model stands historically.
            The goal is disciplined evidence gathering first, not forced picks.
          </p>
        </div>
        <div class="hero-meta">
          <strong>Generated {escape(generated_at)}</strong>
          <span>{escape(live_subtitle)}</span>
          <span>Client reading rule: look at edge and process today, then CLV and calibration as the sample grows.</span>
        </div>
      </div>
    </section>

    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab-button active" data-tab="today">Today</button>
      <button class="tab-button" data-tab="candidates">Candidates</button>
      <button class="tab-button" data-tab="results">Results</button>
      <button class="tab-button" data-tab="models">Models</button>
      <button class="tab-button" data-tab="process">How It Works</button>
    </nav>

    <section id="today" class="tab-panel active">
      <div class="status-grid">
        {_render_status_strip(summary, live_context)}
      </div>
      {_render_featured_card(featured_card)}
      {_render_lifecycle_panel(featured_card, featured_row)}
      {_render_audit_trail(featured_card)}
    </section>

    <section id="candidates" class="tab-panel">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Pass | Watch | Candidate</h2>
            <p class="muted-copy">These are the matches that reached the analysis stage today. The system is allowed to return zero valid candidates if the gates are not met.</p>
          </div>
        </div>
        {_render_watchlist_table(predicted_cards)}
      </section>
    </section>

    <section id="results" class="tab-panel">
      <div class="results-grid">
        {_render_results_cards(summary, live_context)}
      </div>
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Performance &amp; Remarks</h2>
            <p class="muted-copy">CLV matters more than short-term wins. ROI stays secondary until the live sample is large enough.</p>
          </div>
        </div>
        <ul class="assessment-list">
          {_render_assessment_lines(assessment)}
        </ul>
      </section>
      {_render_interpretation_panel(summary)}
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Decision Ledger</h2>
            <p class="muted-copy">Superseded or invalidated rows remain auditable, but they are excluded from valid-candidate performance counts.</p>
          </div>
        </div>
        {_render_ledger_table(rows)}
      </section>
    </section>

    <section id="models" class="tab-panel">
      {_render_model_progress(model_progress)}
      <p class="footer-note">
        Current interpretation: probability estimation is improving from v1 to v3, while the betting-policy layer is still not proven profitable.
        That is why live paper/shadow runs and historical model validation are kept separate.
      </p>
    </section>

    <section id="process" class="tab-panel">
      {_render_process_panel()}
      <section class="panel">
        <div class="panel-head">
          <div><h2>What the client should expect next</h2></div>
        </div>
        <div class="detail-grid">
          <div class="detail-block">
            <h3>Immediate live work</h3>
            <ul class="detail-list">
              <li>Capture closing prices and results for the active paper/shadow candidates.</li>
              <li>Track CLV and settlement without changing already-locked decisions.</li>
              <li>Keep using promoted v3 for live paper/shadow until v4 is formally finalized.</li>
            </ul>
          </div>
          <div class="detail-block">
            <h3>Next platform improvements</h3>
            <ul class="detail-list">
              <li>Finalize v4 separately and promote it only if the holdout confirmation survives.</li>
              <li>Add richer market intelligence only after timestamped odds history is reliable.</li>
              <li>Expand the client surface from a review dashboard into a fuller persistent platform view.</li>
            </ul>
          </div>
        </div>
      </section>
    </section>
  </div>
  <script>
    const buttons = document.querySelectorAll('.tab-button');
    const panels = document.querySelectorAll('.tab-panel');
    buttons.forEach((button) => {{
      button.addEventListener('click', () => {{
        const target = button.getAttribute('data-tab');
        buttons.forEach((item) => item.classList.toggle('active', item === button));
        panels.forEach((panel) => panel.classList.toggle('active', panel.id === target));
      }});
    }});
  </script>
</body>
</html>"""


def write_dashboard_html(
    ledger_db_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Read the ledger, build the dashboard, and write it to disk."""

    with connect_paper_shadow_ledger(ledger_db_path) as conn:
        rows = fetch_ledger_rows(conn)
    target = Path(output_path)
    watchlist_payload = _load_watchlist_payload(ledger_db_path)
    model_progress = _load_model_progress(Path(ledger_db_path), target)
    html = build_dashboard_html(
        rows,
        watchlist_payload=watchlist_payload,
        model_progress=model_progress,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return {
        "output_path": str(target),
        "row_count": len(rows),
        "summary": summarize_outputs(rows),
    }
