# sports-ai - V1 code skeleton

Runnable skeleton implementing the load-bearing pieces of the architecture:
the firewall, the lock hash chain, the standard agent contract, devig math,
a point-level tennis simulator, and evaluation metrics. The developer fills in
data ingestion, the remaining agents, and the meta-model.

## Layout

```text
src/sports_ai/
    config.py                     settings (DB DSNs per role, thresholds)
    models/output.py              AgentOutput (pydantic) - the standard contract
    firewall.py                   allow-list payload builder + deep odds-contamination scan
    locking.py                    prediction/market lock hash chain
    data_quality.py               data-quality scoring with per-competition floors
    agents/base.py                BaseAgent + registry (loads manifest/agents_manifest.json)
    agents/tennis/elo.py          T01/T02-style Elo (reference implementation)
    agents/tennis/simulation.py   T19 point-level Markov simulator (working)
    agents/market/devig.py        M02 devig: proportional, power, Shin (working)
    pipeline/daily.py             the 13-step orchestration skeleton
    evaluation/metrics.py         log loss, Brier, calibration slope (working)
    evaluation/clv.py             CLV computation (working)
    evaluation/reporting.py       output summaries + plain-language assessment
    data_sources/football_data_co_uk.py
                                  historical football CSV profiler for source validation
    data_sources/football_history.py
                                  historical football intake bundle with sports/market separation
    storage/football_research_store.py
                                  local SQLite research store for football intake bundles
    storage/paper_shadow_ledger.py
                                  append-only paper/shadow decision ledger
tests/                            firewall, devig, simulation, metrics tests
```

## Quickstart

```bash
cd code
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
# all tests should pass
python -m sports_ai.pipeline.daily --demo
# runs the pipeline on a synthetic event
python -m sports_ai.data_sources.football_data_co_uk --season 2526 --division E0
# profiles one football-data.co.uk CSV for intake validation
python -m sports_ai.data_sources.football_history --season 2526 --division E0 --preview-limit 1
# builds a historical football intake preview with separate sports and market rows
python -m sports_ai.storage.football_research_store --season 2526 --division E0 --db-path ..\research\football_research\store\football_research.sqlite
# stores a historical football bundle in the local research DB
python -m sports_ai.pipeline.daily --demo --ledger-db ..\research\paper_shadow_demo.sqlite --closing-price 1.66 --settlement WIN --dashboard-html ..\research\paper_shadow_dashboard.html
# runs the synthetic pipeline, records the decision, grades it, and writes a review dashboard
python -m sports_ai.review_app --db ..\research\paper_shadow_demo.sqlite --research-db ..\research\football_research\store\football_research.sqlite --export-html ..\research\paper_shadow_dashboard.html
# starts the persistent local review app for running tests, grading decisions, and rebuilding the client dashboard
```

## New evaluation pieces

- `storage.paper_shadow_ledger` records pre-event decisions and later close/settlement updates without rewriting the original row.
- `evaluation.reporting` turns ledger rows into:
  - funnel counts (`PASS` / `WATCH` / `BET_CANDIDATE`)
  - CLV summary
  - basic return summary
  - log loss / Brier / calibration checks
  - plain-language assessment text for review notes or dashboards
- `evaluation.dashboard` writes a local HTML report so the current lane can be reviewed visually.
- `review_app` starts a persistent local interface backed by the same SQLite ledger.
  It lets you run a test scenario, append close/settlement updates, run the first historical football lane, and rebuild the client dashboard from the browser.

## How to read the output

- `CLV` is the first number to trust.
  Positive CLV over a real sample is good. Flat or negative CLV is bad even if a few bets win.
- `Brier` and `log loss` judge probability quality.
  Lower is better.
- `Calibration slope` shows whether probabilities are too aggressive or too conservative.
  Close to `1.0` is healthier.
- `ROI` and win rate are secondary.
  They matter, but they should not overrule weak CLV.

## Wiring the database

Apply `../sql/schema.sql` to Postgres 15+, create login users inheriting the
three roles, and set `SPORTS_AI_DSN_BLIND`, `SPORTS_AI_DSN_MARKET`,
`SPORTS_AI_DSN_DECISION`, `SPORTS_AI_DSN_ORCH`. Each zone connects with its
own role - this is part of the firewall; do not share one superuser DSN.

## Rules encoded here that must never be weakened

1. `firewall.build_payload` is the ONLY way agent inputs are constructed in production AND in backtests.
2. Zone A agents run before `lock_predictions`; the DB trigger rejects late writes.
3. `AgentOutput.status = INSUFFICIENT_DATA` is a normal, expected output.
4. Decisions never contain `GUARANTEED` / `LOCK` / `CERTAIN`.
