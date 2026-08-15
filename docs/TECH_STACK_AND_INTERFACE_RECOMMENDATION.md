# Tech Stack And Interface Recommendation

Last updated: Thursday, August 13, 2026

## Recommended Core Stack

### Best fit for this project now

- Python 3.11+
- PostgreSQL 15+
- FastAPI for the internal API layer
- Pydantic for contracts and validation
- `psycopg` for database access
- `pytest` for tests
- `pandas` for early research/data wrangling
- optional `polars` later if data volume or speed becomes a problem
- optional `statsmodels` for statistical baselines and interpretable research tests
- optional `XGBoost` / `LightGBM` later if tree models earn their place in backtests

## Why This Is The Best Fit

- it matches the Developer Pack baseline;
- Python is best for the data, research, modeling, and pipeline parts;
- PostgreSQL is the right place for auditable history, locking, and role separation;
- FastAPI is a strong fit for internal services and a future dashboard backend;
- this stack keeps the firewall and immutable-history requirements easier to enforce.

## Recommended Job / Pipeline Style

### Best now

- CLI-driven pipelines;
- scheduled runs via cron/task scheduler;
- database-backed ledgers;
- simple background job flow before introducing heavier orchestration.

### Do not overbuild yet

Avoid starting with:

- microservices everywhere;
- Kubernetes;
- a complex event-bus architecture;
- a heavy ML platform before the core loop works.
- `Redis` before live-state needs are real;
- `TimescaleDB` / `ClickHouse` before odds-tick volume actually demands them;
- compiled low-latency layers before there is a real in-play or speed-critical requirement.

## Recommended Interface Strategy

### Best immediate interface

An internal research and review dashboard, not a public polished product.

The first interface should help us:

- inspect upcoming events;
- see data completeness;
- compare fair price vs market price;
- view candidate decisions;
- review historical decisions and CLV;
- track passes/watches/candidates;
- inspect blockers and missing data.

### Best long-term interface stack

- Next.js / React frontend
- FastAPI backend
- PostgreSQL database

Why:

- clean separation between interface and engine;
- easier future growth into an operator dashboard;
- good fit for decision history, filters, audit views, and research summaries.

## What Can Be Done With An Interface First

### Phase 1 - Internal only

- daily event list
- data-quality status
- research notes per event
- fair-vs-market comparison
- pass/watch/candidate output
- immutable decision history
- closing-line review

### Phase 2 - Operator workflow

- run-trigger controls
- source-health view
- event rerun controls
- policy/version display
- export to CSV/report

### Phase 3 - Client-facing or group-support tooling

- summary cards for trial outputs
- simple posting/export views
- weekly performance summaries
- CLV and accuracy history

## What We Should Not Build First

- a polished public betting app;
- customer billing or account management;
- automatic betting execution;
- a visually heavy marketing site.

## Interface Recommendation In One Line

Build the engine and the audit trail first, then a small internal dashboard around it.

## Streamlit vs Next.js

### Streamlit

Good for:

- very fast internal research prototypes.

Not best for:

- long-term role separation;
- richer operator workflows;
- a serious audit-focused product.

### Next.js

Better long-term choice for:

- a maintainable internal dashboard;
- operator workflows;
- future expansion.

## Final Recommendation

If we need speed:

- use documents, CSVs, and API outputs immediately;
- keep interface light.
- keep the first research loop boring and measurable.

If we need the best durable product path:

- Python + PostgreSQL + FastAPI + Next.js is the best overall stack for this project.

If future market-data volume becomes large:

- consider `TimescaleDB` or `ClickHouse` for odds-tick storage;
- consider `Redis` for live in-memory state;
- consider compiled speed layers only if live/in-play workflows ever become a real requirement.
