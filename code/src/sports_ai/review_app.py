"""Persistent local review app for the paper/shadow ledger.

This server is intentionally lightweight and standard-library only. It gives
the current project phase a persistent interactive surface where an operator
or client can:

1. run a demo scenario into the ledger
2. append a closing price / settlement to an existing decision
3. export the latest static HTML dashboard
4. inspect the current summary, remarks, and decision table
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

from .evaluation.dashboard import write_dashboard_html
from .evaluation.reporting import explain_assessment, summarize_outputs
from .experiments.football_1x2_close_value import (
    HistoricalLaneConfig,
    run_historical_football_1x2_close_value,
)
from .pipeline.football_watchlist import FootballWatchlistConfig, generate_football_watchlist
from .pipeline.daily import build_demo_result, log_demo_result
from .storage.paper_shadow_ledger import (
    OutcomeUpdate,
    append_outcome_update,
    connect as connect_paper_shadow_ledger,
    fetch_ledger_rows,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def watchlist_state_path(ledger_db_path: str | Path) -> Path:
    ledger_path = Path(ledger_db_path)
    return ledger_path.with_name(f"{ledger_path.stem}_watchlist_state.json")


def load_watchlist_state(ledger_db_path: str | Path) -> dict[str, Any] | None:
    path = watchlist_state_path(ledger_db_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_watchlist_state(ledger_db_path: str | Path, payload: dict[str, Any]) -> Path:
    path = watchlist_state_path(ledger_db_path)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_review_bundle(ledger_db_path: str | Path) -> dict[str, Any]:
    """Load merged rows plus summary and remarks for the current ledger."""

    with connect_paper_shadow_ledger(ledger_db_path) as conn:
        rows = fetch_ledger_rows(conn)
    summary = summarize_outputs(rows)
    assessment = explain_assessment(summary)
    return {
        "rows": rows,
        "summary": summary,
        "assessment": assessment,
        "watchlist_state": load_watchlist_state(ledger_db_path),
    }


def run_demo_action(
    ledger_db_path: str | Path,
    *,
    closing_price: float | None = None,
    settlement: str | None = None,
) -> dict[str, Any]:
    """Record one synthetic run into the persistent ledger."""

    return log_demo_result(
        build_demo_result(),
        ledger_db_path,
        closing_price=closing_price,
        settlement=settlement,
    )


def append_grade_action(
    ledger_db_path: str | Path,
    *,
    decision_id: str,
    closing_price: float | None = None,
    settlement: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Append a later close or settlement update to an existing decision."""

    result_status = "SETTLED" if settlement else "PENDING"
    with connect_paper_shadow_ledger(ledger_db_path) as conn:
        result = append_outcome_update(
            conn,
            OutcomeUpdate(
                decision_id=decision_id,
                result_status=result_status,
                closing_price=closing_price,
                settlement=settlement,
                notes=notes,
            ),
        )
    return result


def export_dashboard_action(
    ledger_db_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Write the latest static client-friendly dashboard."""

    return write_dashboard_html(ledger_db_path, output_path)


def run_historical_football_lane_action(
    *,
    research_db_path: str | Path,
    ledger_db_path: str | Path,
    season_code: str,
    competition_ids: Sequence[str],
    retail_bookmaker: str,
    min_ev: float,
    min_edge: float,
    allowed_selections: Sequence[str],
    max_fair_odds: float | None,
    max_available_odds: float | None,
) -> dict[str, Any]:
    """Run the first real football lane into the persistent ledger."""

    return run_historical_football_1x2_close_value(
        HistoricalLaneConfig(
            research_db_path=research_db_path,
            ledger_db_path=ledger_db_path,
            competition_ids=tuple(competition_ids),
            season_code=season_code,
            retail_bookmaker=retail_bookmaker,
            min_ev=min_ev,
            min_edge=min_edge,
            allowed_selections=tuple(allowed_selections),
            max_fair_odds=max_fair_odds,
            max_available_odds=max_available_odds,
        )
    )


def run_live_football_watchlist_action(
    *,
    research_db_path: str | Path,
    ledger_db_path: str | Path,
    season_code: str,
    division_codes: Sequence[str],
    target_date: str,
    bookmaker: str,
    model_name: str,
    min_team_matches: int,
    prior_weight: float,
    strength_regularization: float,
    edge_threshold: float,
    allowed_selections: Sequence[str],
    max_fair_odds: float | None,
    fixture_source: str,
    football_data_token: str | None = None,
    api_football_token: str | None = None,
    api_football_league_map_path: str | Path | None = None,
    manual_fixtures_path: str | Path | None = None,
    manual_market_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the live football paper/shadow watchlist and persist the latest diagnostics."""

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db_path,
            season_code=season_code,
            division_codes=tuple(division_codes),
            target_date=target_date,
            bookmaker=bookmaker,
            model_name=model_name,
            min_team_matches=min_team_matches,
            prior_weight=prior_weight,
            strength_regularization=strength_regularization,
            edge_threshold=edge_threshold,
            allowed_selections=tuple(allowed_selections),
            max_fair_odds=max_fair_odds,
            ledger_db_path=ledger_db_path,
            fixture_source=fixture_source,
            football_data_token=football_data_token,
            api_football_token=api_football_token,
            api_football_league_map_path=api_football_league_map_path,
            manual_fixtures_path=manual_fixtures_path,
            manual_market_path=manual_market_path,
        )
    )
    state_payload = {
        "saved_at": _utc_now(),
        "result": result,
    }
    state_path = save_watchlist_state(ledger_db_path, state_payload)
    return {
        **result,
        "watchlist_state_path": str(state_path),
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2%}"


def _fmt_num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def render_review_app_html(
    *,
    ledger_db_path: str | Path,
    export_html_path: str | Path,
    research_db_path: str | Path,
    bundle: dict[str, Any],
    message: str = "",
    level: str = "info",
) -> str:
    """Render the persistent review app HTML."""

    rows = bundle["rows"]
    summary = bundle["summary"]
    assessment = bundle["assessment"]
    watchlist_state = bundle.get("watchlist_state")
    funnel = summary["funnel"]
    clv = summary["clv"]
    returns = summary["returns"]
    prediction = summary["prediction_metrics"]
    live_candidates = funnel["BET_CANDIDATE"] + funnel["PAPER_SHADOW_CANDIDATE"]

    banner = ""
    if message:
        banner = (
            f'<div class="banner {escape(level)}">'
            f"{escape(message)}"
            "</div>"
        )

    cards = [
        ("Total Rows", str(summary["total_rows"])),
        ("Live Candidates", str(live_candidates)),
        ("Research Signals", str(funnel["RESEARCH_SIGNAL"])),
        ("Watches", str(funnel["WATCH"])),
        ("Passes", str(funnel["PASS"])),
        ("Avg CLV", _fmt_pct(clv["mean"])),
        ("CLV Sample", str(clv["n"])),
        ("ROI / Bet", _fmt_num(returns["roi_per_bet"])),
        ("Brier", _fmt_num(prediction["brier"], 4)),
    ]
    if watchlist_state:
        latest_watchlist = watchlist_state.get("result") or {}
        latest_funnel = latest_watchlist.get("funnel") or {}
        cards.extend(
            [
                ("Last Watchlist Fixtures", str(latest_funnel.get("fixtures_discovered", 0))),
                ("Last Watchlist Candidates", str(latest_funnel.get("final_paper_shadow_candidates", 0))),
            ]
        )
    cards_html = "\n".join(
        f'<div class="card"><div class="label">{escape(label)}</div><div class="value">{escape(value)}</div></div>'
        for label, value in cards
    )
    assessment_html = "\n".join(f"<li>{escape(line)}</li>" for line in assessment)

    if watchlist_state:
        latest_watchlist = watchlist_state.get("result") or {}
        latest_funnel = latest_watchlist.get("funnel") or {}
        provider_lines = []
        for entry in latest_watchlist.get("provider_diagnostics") or []:
            provider_lines.append(
                "<li>"
                f"{escape(entry.get('provider', 'unknown'))} / {escape(entry.get('division_code', ''))}: "
                f"{escape(entry.get('status', 'UNKNOWN'))}"
                + (
                    f" ({escape(entry.get('failure_reason', ''))})"
                    if entry.get("failure_reason")
                    else ""
                )
                + "</li>"
            )
        provider_html = "".join(provider_lines) or "<li>No provider diagnostics recorded yet.</li>"
        watchlist_panel = f"""
        <div class="panel">
          <h2>Last Live Watchlist Run</h2>
          <div class="meta">
            <div><strong>Saved:</strong> {escape(str(watchlist_state.get("saved_at") or "N/A"))}</div>
            <div><strong>Date:</strong> {escape(str(latest_watchlist.get("target_date") or "N/A"))}</div>
            <div><strong>Model:</strong> {escape(str(latest_watchlist.get("model_name") or "N/A"))}</div>
            <div><strong>Fixtures discovered:</strong> {escape(str(latest_funnel.get("fixtures_discovered", 0)))}</div>
            <div><strong>Paper/shadow candidates:</strong> {escape(str(latest_funnel.get("final_paper_shadow_candidates", 0)))}</div>
            <div><strong>Provider failures:</strong> {escape(str(latest_funnel.get("provider_failures", 0)))}</div>
            <div><strong>State file:</strong> {escape(str(watchlist_state_path(ledger_db_path)))}</div>
          </div>
          <ul class="assessment">
            {provider_html}
          </ul>
        </div>
        """
    else:
        watchlist_panel = """
        <div class="panel">
          <h2>Last Live Watchlist Run</h2>
          <p class="inline-note">
            No live watchlist run has been recorded yet. Use the live action below to create a persistent
            paper/shadow run state with provider diagnostics and candidate counts.
          </p>
        </div>
        """

    table_headers = [
        "decision_id",
        "created_at",
        "event_id",
        "competition",
        "market",
        "selection",
        "available_price",
        "closing_price",
        "fair_probability",
        "edge",
        "decision",
        "result_status",
        "settlement",
        "clv",
    ]
    head_html = "".join(f"<th>{escape(header)}</th>" for header in table_headers)
    row_parts: list[str] = []
    for row in rows:
        cells: list[str] = []
        for header in table_headers:
            value = row.get(header)
            if isinstance(value, float) and header in {"fair_probability", "edge", "clv"}:
                display = _fmt_pct(value)
            elif isinstance(value, float):
                display = _fmt_num(value)
            elif value is None:
                display = "N/A"
            else:
                display = str(value)
            cells.append(f"<td>{escape(display)}</td>")
        row_parts.append(f"<tr>{''.join(cells)}</tr>")
    rows_html = "".join(row_parts) or '<tr><td colspan="14">No rows yet. Run a demo or append a result to begin.</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sports AI Review App</title>
  <style>
    :root {{
      --bg: #f5efe2;
      --panel: #fffaf2;
      --ink: #171412;
      --muted: #695d52;
      --accent: #8b5e34;
      --accent-soft: #f4ddbc;
      --border: #d8c7af;
      --good: #166534;
      --bad: #991b1b;
      --info: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Georgia, serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, #f8dd99 0%, transparent 25%),
        radial-gradient(circle at top left, #e9f4d0 0%, transparent 18%),
        linear-gradient(180deg, #f8f3e9 0%, var(--bg) 100%);
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px 18px 40px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2.15rem;
      letter-spacing: 0.02em;
    }}
    .sub {{
      color: var(--muted);
      max-width: 880px;
      line-height: 1.55;
      margin-bottom: 18px;
    }}
    .banner {{
      padding: 12px 14px;
      border-radius: 14px;
      margin-bottom: 18px;
      border: 1px solid var(--border);
      background: #eef4ff;
    }}
    .banner.success {{ background: #eaf8ec; border-color: #b7e0be; }}
    .banner.error {{ background: #fdecec; border-color: #efb8b8; }}
    .banner.info {{ background: #eef4ff; border-color: #b8cdf5; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .card, .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 10px 28px rgba(82, 60, 35, 0.08);
    }}
    .card {{
      padding: 16px;
    }}
    .label {{
      color: var(--muted);
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 6px;
    }}
    .value {{
      font-size: 1.55rem;
      font-weight: 700;
    }}
    .columns {{
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .panel {{
      padding: 18px;
    }}
    .panel h2 {{
      margin-top: 0;
      font-size: 1.18rem;
    }}
    .assessment {{
      margin: 0;
      padding-left: 20px;
      line-height: 1.55;
    }}
    .actions {{
      display: grid;
      gap: 14px;
    }}
    form {{
      display: grid;
      gap: 10px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }}
    label {{
      display: grid;
      gap: 4px;
      font-size: 0.93rem;
      color: var(--muted);
    }}
    input, select, textarea, button {{
      font: inherit;
    }}
    input, select, textarea {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: #fffdf9;
      color: var(--ink);
    }}
    textarea {{
      min-height: 88px;
      resize: vertical;
    }}
    button {{
      padding: 11px 14px;
      border-radius: 999px;
      border: none;
      background: linear-gradient(135deg, #8b5e34 0%, #b67c40 100%);
      color: white;
      cursor: pointer;
      font-weight: 700;
    }}
    button.secondary {{
      background: linear-gradient(135deg, #2d6a4f 0%, #40916c 100%);
    }}
    button.ghost {{
      background: linear-gradient(135deg, #465f87 0%, #5778ab 100%);
    }}
    .inline-note {{
      color: var(--muted);
      line-height: 1.45;
      margin-top: -2px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.92rem;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      text-align: left;
      padding: 10px 8px;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #fdf8ef;
    }}
    .meta {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      margin-bottom: 14px;
    }}
    .legend {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .legend div {{
      border-radius: 12px;
      padding: 12px;
      background: var(--accent-soft);
      border: 1px solid #e2c594;
    }}
    @media (max-width: 960px) {{
      .columns {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 720px) {{
      .wrap {{
        padding: 18px 12px 28px;
      }}
      h1 {{
        font-size: 1.8rem;
      }}
      table {{
        font-size: 0.84rem;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Sports AI Review App</h1>
    <p class="sub">
      This is the persistent review surface for the paper/shadow ledger. Use it to run a test
      scenario, attach a close or settlement later, and keep a current view of whether the lane
      looks promising, weak, or still too early to judge.
    </p>
    {banner}

    <section class="grid">
      {cards_html}
    </section>

    <section class="columns">
      <div class="panel">
        <h2>Assessment</h2>
        <ul class="assessment">
          {assessment_html}
        </ul>
        <div class="legend">
          <div><strong>Good:</strong> CLV stays positive over a meaningful sample, and the probabilities are not obviously broken.</div>
          <div><strong>Weak:</strong> CLV is flat or negative, even if a few short-term results look nice.</div>
          <div><strong>Too Early:</strong> the sample is small, so the right move is keep logging and avoid overconfidence.</div>
        </div>
      </div>

        <div class="actions">
        <div class="panel">
          <h2>Run Live Football Watchlist</h2>
          <p class="inline-note">
            This runs the same-day paper/shadow watchlist with blind prediction first, market snapshot second,
            and stores the latest operational diagnostics. The default model is the promoted `v3` benchmark.
          </p>
          <form method="post" action="/actions/run-live-watchlist">
            <div class="field-grid">
              <label>Season
                <input name="season_code" value="2627" required>
              </label>
              <label>Target Date
                <input name="target_date" value="2026-08-15" required>
              </label>
              <label>Divisions
                <input name="division_codes" value="E0,E1,E2,E3,SP1" required>
              </label>
              <label>Bookmaker
                <input name="bookmaker" value="Bet365" required>
              </label>
              <label>Model
                <select name="model_name">
                  <option value="poisson_1x2_v3">poisson_1x2_v3</option>
                  <option value="poisson_1x2_v1">poisson_1x2_v1</option>
                </select>
              </label>
              <label>Fixture Source
                <select name="fixture_source">
                  <option value="auto">auto</option>
                  <option value="manual">manual</option>
                  <option value="csv">csv</option>
                  <option value="football-data-org">football-data-org</option>
                  <option value="api-football">api-football</option>
                </select>
              </label>
              <label>Min Team Matches
                <input name="min_team_matches" type="number" min="1" value="6" required>
              </label>
              <label>Prior Weight
                <input name="prior_weight" type="number" step="0.1" min="0" value="5.0" required>
              </label>
              <label>Strength Regularization
                <input name="strength_regularization" type="number" step="0.01" min="0.01" value="0.03" required>
              </label>
              <label>Edge Threshold
                <input name="edge_threshold" type="number" step="0.005" min="0" value="0.03" required>
              </label>
              <label>Selections
                <input name="allowed_selections" value="home,draw,away" required>
              </label>
              <label>Max Fair Odds
                <input name="max_fair_odds" type="number" step="0.1" min="1.01" value="2.8">
              </label>
            </div>
            <label>Manual Fixtures Path
              <input name="manual_fixtures_path" placeholder="C:\\dev\\sports-edge-ai\\research\\manual_inputs\\football_fixtures_template.json">
            </label>
            <label>Manual Market Path
              <input name="manual_market_path" placeholder="C:\\dev\\sports-edge-ai\\research\\manual_inputs\\football_market_template.json">
            </label>
            <div class="meta">
              <div><strong>Research DB:</strong> {escape(str(research_db_path))}</div>
              <div><strong>Ledger DB:</strong> {escape(str(ledger_db_path))}</div>
            </div>
            <button type="submit">Run Live Paper/Shadow Watchlist</button>
          </form>
        </div>

        {watchlist_panel}

        <div class="panel">
          <h2>Run Historical Football Lane</h2>
          <p class="inline-note">
            This is the first real football validation slice. It compares a retail close against
            Pinnacle close for stored historical leagues, then logs only positive-EV research signals.
            Because the source is close-only, this lane is useful for historical discrepancy and
            outcome review, but it is not full CLV proof yet.
          </p>
          <form method="post" action="/actions/run-football-lane">
            <div class="field-grid">
              <label>Season
                <input name="season_code" value="2526" required>
              </label>
              <label>Competitions
                <input name="competition_ids" value="ENG1,ESP1" required>
              </label>
              <label>Retail Book
                <input name="retail_bookmaker" value="Bet365" required>
              </label>
              <label>Min EV
                <input name="min_ev" type="number" step="0.005" min="0" value="0.02" required>
              </label>
              <label>Min Edge
                <input name="min_edge" type="number" step="0.005" min="0" value="0.00" required>
              </label>
              <label>Selections
                <input name="allowed_selections" value="home,draw,away" required>
              </label>
              <label>Max Fair Odds
                <input name="max_fair_odds" type="number" step="0.1" min="1.01" placeholder="optional">
              </label>
              <label>Max Retail Odds
                <input name="max_available_odds" type="number" step="0.1" min="1.01" placeholder="optional">
              </label>
            </div>
            <div class="meta">
              <div><strong>Research DB:</strong> {escape(str(research_db_path))}</div>
            </div>
            <button type="submit">Run Historical Football Test</button>
          </form>
        </div>

        <div class="panel">
          <h2>Run Test Scenario</h2>
          <p class="inline-note">
            This records one synthetic run into the persistent ledger so you can see how the
            evaluation flow behaves end to end.
          </p>
          <form method="post" action="/actions/run-demo">
            <div class="field-grid">
              <label>Closing Price
                <input name="closing_price" type="number" step="0.01" min="1.01" placeholder="1.66">
              </label>
              <label>Settlement
                <select name="settlement">
                  <option value="">Pending only</option>
                  <option value="WIN">WIN</option>
                  <option value="LOSS">LOSS</option>
                  <option value="VOID">VOID</option>
                  <option value="CANCELLED">CANCELLED</option>
                </select>
              </label>
            </div>
            <button type="submit">Run Synthetic Test</button>
          </form>
        </div>

        <div class="panel">
          <h2>Append Result / Grade Decision</h2>
          <p class="inline-note">
            Use this when a real or paper decision gets a closing price later, or when the final
            result settles. This does not rewrite the original row; it appends a new update.
          </p>
          <form method="post" action="/actions/append-grade">
            <label>Decision ID
              <input name="decision_id" required placeholder="dec_...">
            </label>
            <div class="field-grid">
              <label>Closing Price
                <input name="closing_price" type="number" step="0.01" min="1.01" placeholder="1.91">
              </label>
              <label>Settlement
                <select name="settlement">
                  <option value="">No settlement yet</option>
                  <option value="WIN">WIN</option>
                  <option value="LOSS">LOSS</option>
                  <option value="VOID">VOID</option>
                  <option value="CANCELLED">CANCELLED</option>
                </select>
              </label>
            </div>
            <label>Notes
              <textarea name="notes" placeholder="Optional grading note"></textarea>
            </label>
            <button type="submit" class="secondary">Append Update</button>
          </form>
        </div>

        <div class="panel">
          <h2>Export Client Dashboard</h2>
          <div class="meta">
            <div><strong>Ledger DB:</strong> {escape(str(ledger_db_path))}</div>
            <div><strong>Static report:</strong> {escape(str(export_html_path))}</div>
          </div>
          <form method="post" action="/actions/export-dashboard">
            <button type="submit" class="ghost">Rebuild Static Dashboard</button>
          </form>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2>Decision Ledger</h2>
      <table>
        <thead>
          <tr>{head_html}</tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>"""


def _float_or_none(raw_value: str | None) -> float | None:
    if raw_value is None or raw_value == "":
        return None
    return float(raw_value)


def _text_or_none(raw_value: str | None) -> str | None:
    if raw_value is None or raw_value == "":
        return None
    return raw_value


class ReviewAppHandler(BaseHTTPRequestHandler):
    """HTTP handler configured by `main` with ledger paths."""

    ledger_db_path: Path
    export_html_path: Path
    research_db_path: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            params = parse_qs(parsed.query)
            message = params.get("message", [""])[0]
            level = params.get("level", ["info"])[0]
            self._send_home(message=message, level=level)
            return
        if parsed.path == "/api/rows":
            bundle = load_review_bundle(self.ledger_db_path)
            self._send_json(bundle)
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            form = self._read_form()
            if parsed.path == "/actions/run-demo":
                closing_price = _float_or_none(form.get("closing_price"))
                settlement = _text_or_none(form.get("settlement"))
                result = run_demo_action(
                    self.ledger_db_path,
                    closing_price=closing_price,
                    settlement=settlement,
                )
                self._redirect(
                    "/",
                    message=f"Recorded synthetic run {result['decision_id']}.",
                    level="success",
                )
                return
            if parsed.path == "/actions/run-football-lane":
                competition_ids = [
                    value.strip()
                    for value in (form.get("competition_ids") or "").split(",")
                    if value.strip()
                ]
                if not competition_ids:
                    raise ValueError("At least one competition id is required")
                season_code = (form.get("season_code") or "").strip()
                if not season_code:
                    raise ValueError("season_code is required")
                allowed_selections = [
                    value.strip()
                    for value in (form.get("allowed_selections") or "").split(",")
                    if value.strip()
                ]
                if not allowed_selections:
                    raise ValueError("At least one selection is required")
                result = run_historical_football_lane_action(
                    research_db_path=self.research_db_path,
                    ledger_db_path=self.ledger_db_path,
                    season_code=season_code,
                    competition_ids=competition_ids,
                    retail_bookmaker=(form.get("retail_bookmaker") or "Bet365").strip() or "Bet365",
                    min_ev=float(form.get("min_ev") or "0.02"),
                    min_edge=float(form.get("min_edge") or "0.0"),
                    allowed_selections=allowed_selections,
                    max_fair_odds=_float_or_none(form.get("max_fair_odds")),
                    max_available_odds=_float_or_none(form.get("max_available_odds")),
                )
                self._redirect(
                    "/",
                    message=(
                        f"Historical football lane logged {result['candidate_count']} research signals "
                        f"from {result['matched_events']} matched events."
                    ),
                    level="success",
                )
                return
            if parsed.path == "/actions/run-live-watchlist":
                division_codes = [
                    value.strip()
                    for value in (form.get("division_codes") or "").split(",")
                    if value.strip()
                ]
                if not division_codes:
                    raise ValueError("At least one division code is required")
                allowed_selections = [
                    value.strip()
                    for value in (form.get("allowed_selections") or "").split(",")
                    if value.strip()
                ]
                if not allowed_selections:
                    raise ValueError("At least one selection is required")
                season_code = (form.get("season_code") or "").strip()
                target_date = (form.get("target_date") or "").strip()
                if not season_code or not target_date:
                    raise ValueError("season_code and target_date are required")
                result = run_live_football_watchlist_action(
                    research_db_path=self.research_db_path,
                    ledger_db_path=self.ledger_db_path,
                    season_code=season_code,
                    division_codes=division_codes,
                    target_date=target_date,
                    bookmaker=(form.get("bookmaker") or "Bet365").strip() or "Bet365",
                    model_name=(form.get("model_name") or "poisson_1x2_v3").strip() or "poisson_1x2_v3",
                    min_team_matches=int(form.get("min_team_matches") or "6"),
                    prior_weight=float(form.get("prior_weight") or "5.0"),
                    strength_regularization=float(form.get("strength_regularization") or "0.03"),
                    edge_threshold=float(form.get("edge_threshold") or "0.03"),
                    allowed_selections=allowed_selections,
                    max_fair_odds=_float_or_none(form.get("max_fair_odds")),
                    fixture_source=(form.get("fixture_source") or "auto").strip() or "auto",
                    manual_fixtures_path=_text_or_none(form.get("manual_fixtures_path")),
                    manual_market_path=_text_or_none(form.get("manual_market_path")),
                )
                self._redirect(
                    "/",
                    message=(
                        f"Live watchlist found {result['funnel']['fixtures_discovered']} fixtures and "
                        f"{result['funnel']['final_paper_shadow_candidates']} paper/shadow candidates."
                    ),
                    level="success" if result["funnel"]["fixtures_discovered"] > 0 else "error",
                )
                return
            if parsed.path == "/actions/append-grade":
                decision_id = (form.get("decision_id") or "").strip()
                if not decision_id:
                    raise ValueError("decision_id is required")
                closing_price = _float_or_none(form.get("closing_price"))
                settlement = _text_or_none(form.get("settlement"))
                notes = _text_or_none(form.get("notes"))
                result = append_grade_action(
                    self.ledger_db_path,
                    decision_id=decision_id,
                    closing_price=closing_price,
                    settlement=settlement,
                    notes=notes,
                )
                self._redirect(
                    "/",
                    message=f"Appended update to {result['decision_id']}.",
                    level="success",
                )
                return
            if parsed.path == "/actions/export-dashboard":
                result = export_dashboard_action(self.ledger_db_path, self.export_html_path)
                self._redirect(
                    "/",
                    message=f"Exported static dashboard to {result['output_path']}.",
                    level="success",
                )
                return
            self.send_error(404, "Not found")
        except Exception as exc:  # pragma: no cover - exercised via helper tests
            self._redirect("/", message=f"{type(exc).__name__}: {exc}", level="error")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        """Keep console output compact for the local operator."""

        return

    def _read_form(self) -> dict[str, str]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        parsed = parse_qs(raw_body)
        return {key: values[-1] for key, values in parsed.items()}

    def _send_home(self, *, message: str, level: str) -> None:
        bundle = load_review_bundle(self.ledger_db_path)
        html = render_review_app_html(
            ledger_db_path=self.ledger_db_path,
            export_html_path=self.export_html_path,
            research_db_path=self.research_db_path,
            bundle=bundle,
            message=message,
            level=level,
        )
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, path: str, *, message: str, level: str) -> None:
        qs = urlencode({"message": message, "level": level})
        location = f"{path}?{qs}"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent local Sports AI review app")
    parser.add_argument("--db", required=True, help="SQLite paper/shadow ledger path")
    parser.add_argument("--research-db", required=True, help="SQLite football research store path")
    parser.add_argument(
        "--export-html",
        help="static dashboard output path; defaults next to the DB",
    )
    parser.add_argument("--host", default="127.0.0.1", help="host to bind")
    parser.add_argument("--port", type=int, default=8765, help="port to bind")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    ledger_db_path = Path(args.db)
    research_db_path = Path(args.research_db)
    export_html_path = (
        Path(args.export_html)
        if args.export_html
        else ledger_db_path.with_name("paper_shadow_dashboard.html")
    )

    handler_cls = type(
        "ConfiguredReviewAppHandler",
        (ReviewAppHandler,),
        {
            "ledger_db_path": ledger_db_path,
            "export_html_path": export_html_path,
            "research_db_path": research_db_path,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"Sports AI review app running at http://{args.host}:{args.port}")
    print(f"Ledger DB: {ledger_db_path}")
    print(f"Research DB: {research_db_path}")
    print(f"Static dashboard export: {export_html_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
