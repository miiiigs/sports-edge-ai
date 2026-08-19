# CODEX MASTER HANDOFF — SPORTS EDGE AI / SPORTS AI BETTING RESEARCH SYSTEM

## Purpose of this document

This is the complete working handoff for Codex.

Treat this file as the **master context and execution brief** for the project. It consolidates:

- the full project background;
- the client relationship and project origin;
- the authoritative Developer Pack direction;
- the change from a conventional software build into a combined software + strategy-research project;
- the MLB 18–4 bettor sample;
- the bookmaker/odds voice-note knowledge;
- the three-zone firewall architecture;
- the complete agent structure;
- data-quality, locking, immutability, and point-in-time rules;
- the formal V1 scope;
- the MLB scope ambiguity;
- research hypotheses;
- implementation requirements;
- testing requirements;
- backtesting requirements;
- coding/architecture instructions;
- project/commercial guardrails;
- open questions;
- the full README;
- the full progress checklist;
- and the exact behavioral instructions Codex should follow while working in the repository.

This document is intentionally redundant. Redundancy is preferred here because losing a critical constraint is more dangerous than repeating it.

---

# CURRENT EXECUTION ADDENDUM — WEDNESDAY, AUGUST 19, 2026

Current active build state:

- football Day 7 through Day 11A remain accepted for the currently loaded scope
- football Day 12 remains honestly `PARTIAL`
- `CURRENT PRODUCT MILESTONE = FOOTBALL DASHBOARD V1.1 CURRENT RUNTIME VALIDATION`
- `TENNIS DEVELOPMENT = PAUSED`
- current suite baseline: `202 passed`

Current operational dashboard status verified locally on Wednesday, August 19, 2026:

- dashboard route booted successfully
- current fixture feed state: `DEGRADED`
- current prediction engine state: `READY`
- current model snapshot state: `READY`
- current market decision state: `NO`
- `CURRENT_FIXTURE_FEED_READY = NO`
- `CURRENT_PREDICTION_PIPELINE_READY = YES`
- `LIVE_PAPER_PIPELINE_READY = NO`

Important product boundary:

- live/current fixture data may update continuously
- the football model shown on live fixtures remains a frozen pre-match prediction
- the dashboard must not reinterpret the current pre-match model as an in-play model
- the dashboard must not fabricate current or upcoming fixtures if the provider returns none

Real provider verification note:

- during the Wednesday, August 19, 2026 verification window, the configured in-scope API-Football
  season identifier for the current football window was correctly validated as `2026`
- the provider authenticated successfully, but the configured plan blocked current-season fixture
  rows for the scoped leagues with:
  - `Free plans do not have access to this season, try from 2022 to 2024.`
- Dashboard V1.1 therefore now reports the fixture feed as degraded rather than treating the Aug
  19-26 empty slate as a clean "no fixtures exist" result

---

# 0. CODEX OPERATING INSTRUCTIONS

Codex should operate as a senior Python/backend/data/ML systems engineer and technical research assistant on this repository.

## 0.1 Before changing code

Always:

1. inspect the repository;
2. inspect the existing Developer Pack skeleton before creating replacements;
3. identify whether a requested feature already exists partially or fully;
4. run the existing test suite before material modifications when practical;
5. understand the database/firewall implications of every data-path change;
6. distinguish:
   - blind sports data;
   - market data;
   - decision-layer data;
7. avoid adding dependencies or architectural complexity without a clear need;
8. preserve reproducibility;
9. preserve historical immutability;
10. preserve point-in-time semantics.

Do not rewrite functioning modules only for stylistic preference.

## 0.2 Source-of-truth precedence

When requirements conflict, use:

1. latest explicit user/client instruction;
2. August 15, 2026 authoritative PDF specification set:
   - `00-developer-brief`
   - the sport-specific build specification
   - `03-thirty-day-plan`;
3. explicit migration decisions interpreting those PDFs;
4. companion Markdown only where it does not conflict with the PDFs;
5. this master handoff / current README / progress checklist / implementation documentation;
6. older Developer Pack / proposal / legacy research materials.

Any material conflict must be documented rather than silently blended.

## 0.3 Do not confuse an agent with an LLM

An agent is a responsibility/module.

Prefer:

- deterministic mathematics for odds conversion/devig;
- statistical models for Elo/ratings;
- simulation for event distributions;
- ML only where it adds measurable value;
- rules engines for explicit policy;
- LLMs only for bounded qualitative extraction/analysis.

Do not create 50 chatbot wrappers.

## 0.4 Never violate the information firewall

Blind Zone A code must never receive or derive information from:

- sportsbook odds;
- opening/current/closing lines;
- implied probabilities;
- betting percentages;
- line movement;
- market consensus;
- betting commentary;
- tipster predictions;
- odds-derived ratings.

A convenience shortcut that violates this is a bug.

## 0.5 Never use future information in historical evaluation

At prediction time T, only use data known at or before T.

Do not:

- backfill current injury status into historical predictions;
- use final lineup data before it was announced;
- use closing odds at pick time;
- use full-season aggregates that contain the event being predicted;
- train on later events and evaluate on earlier events;
- shuffle temporal data randomly if that creates leakage.

## 0.6 Locked history is immutable

Prediction, market, and decision history must remain reproducible and auditable.

Model changes create new versions.

Strategy threshold changes create new versions.

Never mutate old results to make a model appear better.

## 0.7 Do not force a selection

`INSUFFICIENT_DATA`, `PASS`, and `NO QUALIFYING BETS TODAY` are correct results.

The product should prefer abstention over fabricated confidence.

## 0.8 Do not optimize for promotional win rate

Primary quality measures include:

- calibration;
- Brier score;
- log loss;
- CLV;
- uncertainty;
- data quality;
- out-of-sample performance;
- marginal agent contribution.

P/L and win rate are secondary and require sufficient sample size.

## 0.9 For every material implementation task

Codex should ideally leave:

- code;
- tests;
- updated documentation;
- explicit assumptions;
- unresolved blockers;
- progress-checklist updates.

## 0.10 When a fact is unknown

Do not invent it.

Use one of:

- `UNKNOWN`
- `NOT_YET_VERIFIED`
- `INSUFFICIENT_DATA`
- `REQUIRES_CLIENT_CONFIRMATION`
- `UNVERIFIED_CLAIM`

depending on context.

---

# 1. PROJECT ORIGIN AND BACKGROUND

The developer is Joel Miguel Leysa, a Full Stack Developer and AI Automation Engineer based in the Philippines.

Relevant technical strengths include:

- Python;
- automation;
- APIs;
- PostgreSQL;
- full-stack systems;
- AI workflows;
- n8n;
- data processing;
- testing;
- backend architecture;
- systems integration;
- algorithmic/trading-system style reasoning.

The developer is **not claiming to be a professional sports bettor or established sports quant**. Sports-domain research is part of the learning/research work.

The client is Cherona Chadwick.

The project originally appeared as a more conventional Sports Edge AI software platform engagement.

An older proposal described:

- React;
- Spring Boot;
- PostgreSQL;
- Google Gemini;
- third-party sports data;
- secure REST APIs;
- backend authentication;
- workflow/business logic;
- AI;
- scheduling;
- approvals;
- settlements;
- audit logs;
- persistence;
- Docker.

That earlier proposal contained roughly four £2,000 phases for a total of about £8,000.

This older architecture is now partially superseded.

The stronger technical baseline is the 89-page:

**Sports AI Betting Research System — V1 Developer Pack**  
Version 1.0  
Date: August 11, 2026  
Stack: Python 3.11+ / PostgreSQL 15+

The Pack defines a 50-agent, firewall-separated research system for:

- English football tiers 1–4;
- Scottish football tiers 1–4;
- ATP Tour men's singles;
- ATP Challenger men's singles.

The Pack explicitly describes the system as research/decision support, not an automatic wagering executor.

---

# 2. HOW THE CLIENT'S DIRECTION EVOLVED

The formal Pack initially looks like:

> build the football and tennis research architecture with all defined agents over time.

Subsequent client voice notes changed the emphasis.

The current direction is closer to:

> understand how strong bettors and bookmakers appear to think, reconstruct historical decisions, understand pricing and line movement, create explicit hypotheses, test those hypotheses, then build/prioritize the agents and rules that prove useful.

Therefore the project now has two linked components:

## A. Formal product architecture

- firewall;
- locks;
- agents;
- data warehouse;
- feature store;
- market layer;
- decision layer;
- backtesting;
- API/dashboard.

## B. Betting-strategy reverse-engineering research

- historical bettor examples;
- price behavior;
- opening/current/closing lines;
- market movement;
- bettor entry timing;
- sports context;
- bookmaker behavior;
- hypothesis testing.

Codex must preserve separation between these.

The strategy research may determine which agents deserve priority, but it must not contaminate the blind model.

---

# 3. PRIMARY RESEARCH SAMPLE — MLB 18–4 IMAGE

The client provided a Sean Perry Wins promotional image titled approximately:

**MLB PACKAGE RUN 18–4**

Advertised statistics:

- 22 plays;
- 18 winners;
- 4 losses;
- 81.8% win rate.

Recorded picks:

1. Mets vs Phillies U9.5 — win
2. Royals +1.5 — win
3. Tigers -1.5 — win
4. Dodgers vs Yankees U9 — win
5. Brewers -1.5 — win
6. Tigers ML — loss
7. Marlins vs Astros U8 — win
8. Royals vs Tigers U8.5 — win
9. Giants ML — win
10. Dodgers ML — win
11. Brewers -1.5 — win
12. Red Sox ML — win
13. Padres ML — win
14. Red Sox ML — win
15. Dodgers ML — win
16. Brewers ML — win
17. Dodgers ML — loss
18. Padres ML — win
19. Padres +1.5 — loss
20. Tigers +1.5 — win
21. Padres vs Diamondbacks U9.5 — loss
22. Red Sox ML — win

Market mix:

- moneyline: 11 plays, 9–2;
- run line: 6 plays, 5–1;
- totals: 5 plays, 4–1.

Repeated teams:

- Dodgers;
- Brewers;
- Red Sox;
- Padres;
- Tigers;
- Royals.

This sample is not proof of a sustainable strategy.

Critical missing data includes:

- exact dates;
- exact games;
- sportsbook;
- selection posting time;
- entry odds;
- opening odds;
- closing odds;
- full movement path;
- starting pitchers;
- expected/confirmed lineups;
- injuries;
- bullpen state;
- weather;
- park;
- schedule/fatigue;
- public money/ticket data;
- market consensus;
- stake;
- complete unselected historical record.

Codex should help create a normalized reconstruction dataset and never infer unknown historical facts from the promotional image alone.

---

# 4. VOICE-NOTE KNOWLEDGE — ODDS / BOOKMAKER STRATEGY

A later voice-note transcript describes a claimed bookmaker-discrepancy strategy.

Treat all of this as **source claims and research hypotheses**, not verified market theory.

## 4.1 Patriots vs Seahawks example described in the voice note

The speaker claims:

- Patriots were around +4.5 on the spread;
- the game "should" have been around +10.5;
- approximately six points of discrepancy;
- about 86% of money was allegedly on Patriots +4.5;
- the sportsbook allegedly did not move the line;
- this was interpreted as the sportsbook being comfortable taking Patriots action;
- Seahawks allegedly won by 16.

The implied claimed signal:

> strong public money + line resistance + large internal/expected line discrepancy may indicate the popular side is incorrectly priced.

This is **not automatically true** and must be tested.

## 4.2 Moneyline example

The speaker gives an example similar to:

- available price: -150;
- claimed normal/expected price: -250;
- a 100-cent pricing discrepancy;
- analysts look for such discrepancies.

This motivates the concept:

`independent fair price vs available market price`

but does not validate the claimed expected price.

## 4.3 Early line movement

The speaker claims some MLB selections may be obtained around prices such as -120 and then move after high-stake customers/Sean place bets.

This creates a testable requirement to reconstruct:

- opener;
- pick-time price;
- timestamp;
- immediate post-pick price;
- later market price;
- close;
- cross-book movement.

## 4.4 Unverified commercial/performance claims

The voice note also contains claims about:

- 80%+ MLB performance;
- $100k–$500k personal wagers;
- multi-million-dollar profits;
- 6–1 weeks;
- 8–2/9–1 package expectations;
- package pricing and profitability.

These claims must never become model truth without independent records.

Tag them:

`UNVERIFIED_CLAIM`.

---

# 5. RESEARCH QUESTIONS CREATED BY THE VOICE NOTE

Codex should support testing, not storytelling.

Examples:

1. Does large fair-line vs market-line discrepancy predict positive CLV?
2. Does high public money with limited line movement provide incremental information?
3. Does reverse line movement add signal after controlling for news?
4. Are certain books systematically stale relative to market leaders?
5. Do selections move favorably after the bettor posts?
6. Does the bettor consistently beat the closing line?
7. Does the bettor choose ML vs run line based on projected margin?
8. Are totals selected under repeatable pitcher/weather/bullpen conditions?
9. Are certain odds ranges preferred?
10. Do repeated teams reflect team identity or underlying features?
11. Are winning examples cherry-picked from a larger unknown population?

A valid result can be:

- supported;
- rejected;
- inconclusive;
- impossible to test with available data.

---

# 6. CLIENT PRODUCT EXPECTATIONS BEYOND WINNER MARKETS

The client explicitly does not want the system limited to simple winner predictions.

Potential football markets mentioned/described:

- 1X2;
- draw;
- totals;
- BTTS;
- handicaps;
- eventually corners;
- eventually cards.

Potential tennis markets:

- winner;
- straight sets / 2–0;
- game handicap;
- set handicap;
- totals.

Formal Developer Pack V1 supports:

Football:

- 1X2;
- DNB;
- Asian Handicap;
- O/U 2.5;
- BTTS.

Tennis:

- match winner;
- game handicap;
- set handicap;
- match-games O/U.

Corners/cards/player props remain formal scope expansion unless explicitly reprioritized.

---

# 7. COMMERCIAL / WORKING ARRANGEMENT CONTEXT

Prior discussions documented:

- approximately 8 hours/day expected;
- likely ~40 hours/week;
- monthly compensation agreed in discussion: $850/month;
- payment described as biweekly;
- signup/start fee: $250;
- a future raise after launch/revenue was mentioned but not quantified.

Do not assume the future raise exists contractually.

Items that require actual confirmation/evidence before marking complete:

- $250 receipt;
- exact first payment date;
- exact biweekly amount;
- payment method;
- normal workdays;
- scope boundaries;
- change-request process;
- termination/change expectations.

This project is being treated as officially started as of August 13, 2026 in the working chat context.

Codex should not mix commercial negotiation into technical code unless maintaining a project-management document, but it should preserve scope and requirement-change records.

---

# 8. DOCUMENTS / ARTIFACTS THAT BELONG TO THE PROJECT

Primary provided/reference artifacts:

1. `Sports-AI-Developer-Pack-V1(2).pdf`
   - 89-page Developer Pack
   - version 1.0
   - date 2026-08-11
   - Python 3.11+ / PostgreSQL 15+
   - architecture, agents, DB, API, roadmap, data sources, tests, code skeleton

2. MLB Sean Perry Wins 18–4 screenshot
   - 22 picks
   - promotional historical sample

3. `Pasted text(20260812-210337).txt`
   - voice-note transcript
   - bookmaker discrepancy / line movement / sales discussion

4. Older master handoff markdown
   - background;
   - client changes;
   - employment/payment;
   - scope;
   - technical interpretations.

5. `SPORTS_EDGE_AI_MASTER_README.md`
   - current working technical/project README

6. `SPORTS_EDGE_AI_PROGRESS_CHECKLIST.md`
   - current operational checklist

This master handoff embeds items 5 and 6 below.

---

# 9. EXACT HIGH-LEVEL SYSTEM FLOW

```text
RAW DATA SOURCES
    ↓
RAW IMMUTABLE WAREHOUSE
    ↓
POINT-IN-TIME FEATURE STORE
    ↓
ZONE A — BLIND SPORTS ANALYSIS
    ↓
SPORT FAIR-PRICE ENGINE
    ↓
PREDICTION LOCK
    ↓
ZONE B — MARKET INTELLIGENCE
    ↓
MARKET LOCK
    ↓
ZONE C — DECISION / STRATEGY / RISK
    ↓
BET_CANDIDATE / WATCH / PASS
    ↓
IMMUTABLE DECISION LEDGER
    ↓
RESULT INGESTION
    ↓
CLV / CALIBRATION / LOG LOSS / BRIER / AGENT PERFORMANCE
    ↓
VERSIONED RETRAINING / POLICY REVIEW
```

---

# 10. CODING RULES FOR THIS REPOSITORY

## 10.1 Python

Target:

- Python 3.11+.

Prefer:

- typed interfaces;
- small testable functions;
- pure deterministic functions for calculations;
- explicit schemas/models;
- dependency injection around external providers;
- UTC timestamps internally where appropriate;
- explicit `as_of`;
- structured logging.

Avoid:

- hidden global state;
- notebook-only production logic;
- silent exception swallowing;
- dynamically changing strategy thresholds;
- provider-specific logic leaking everywhere.

## 10.2 PostgreSQL

Preserve:

- schema separation;
- role separation;
- grants;
- append-only history;
- immutability triggers;
- indexes on identifiers/time;
- model/strategy version links;
- `as_of` semantics.

## 10.3 Data ingestion

Every raw record should preserve enough provenance to answer:

- source;
- retrieved time;
- source event ID;
- original timestamp;
- normalization version;
- whether it contains market data;
- correction/version status where applicable.

## 10.4 External providers

Wrap providers behind adapters.

Do not make core model code depend directly on one API's raw response schema.

## 10.5 Research code

Notebooks/scripts may be used to explore.

Validated logic must graduate into:

- versioned source modules;
- tests;
- configuration;
- experiment registry.

## 10.6 LLM use

If an LLM analyzes contextual sports text:

- Zone A input must be scrubbed;
- no web/odds tools in blind agents;
- output must be structured;
- narrative cannot directly override numeric probability;
- source evidence and uncertainty should be preserved.

---

# 11. EXPECTED CODEX WORKFLOW ON EACH TASK

For each task:

1. state/inspect current implementation;
2. identify affected zone(s);
3. identify data inputs and forbidden inputs;
4. implement smallest coherent change;
5. add/update tests;
6. run tests;
7. report failures honestly;
8. update docs/progress if the work materially changes status;
9. note assumptions;
10. note blockers;
11. avoid claiming completion without evidence.

When modifying data structures, consider backward compatibility and migration impact.

When adding an agent, use the agent Definition of Done from the README.

When adding a strategy hypothesis, version it separately from code.

---

# 12. DO NOT DO THESE THINGS

Codex must not:

- build all 50 agents just to satisfy the number 50;
- equate an agent with an LLM;
- put sportsbook odds into blind agents;
- use closing lines as blind features;
- rewrite past prediction records;
- optimize against the same 22 MLB picks used to derive the hypothesis;
- report promotional win rate as proof;
- silently add MLB as formal V1;
- silently add corners/cards as formal V1;
- build a high-polish dashboard before the research loop works;
- automatically place bets unless explicitly approved much later;
- claim that sportsbook line behavior proves an outcome;
- invent unavailable public-money data;
- fabricate reconstructed historical dates;
- assume a source license is valid without verification;
- add a new model because it sounds sophisticated without measuring marginal value.

---

# 13. IMMEDIATE CODEX STARTING PLAN

If Codex is being placed directly into the codebase now, proceed in this sequence unless repository evidence forces a different order:

### Stage 1 — Repository reconnaissance

- list project tree;
- identify Pack-provided files;
- identify existing README/tests/config;
- identify database files;
- identify code skeleton vs custom code;
- identify environment/dependency manager.

### Stage 2 — Baseline execution

- install dependencies;
- run tests;
- run demo/CLI if available;
- capture exact failures;
- do not fix unrelated issues before documenting them.

### Stage 3 — Architecture verification

Trace:

- agent base contract;
- firewall;
- contamination scanner;
- locking;
- data quality;
- Elo;
- simulation;
- devig;
- metrics;
- daily orchestration.

Produce an `IMPLEMENTED_PARTIAL_MISSING.md` or equivalent if useful.

### Stage 4 — Database

- provision/apply PostgreSQL schema;
- verify roles/grants;
- prove blind role cannot select market schema;
- prove locked ledgers cannot be mutated;
- prove `as_of` query behavior.

### Stage 5 — Minimum honest loop

Focus on ATP slice first unless client explicitly changes implementation priority.

### Stage 6 — Parallel strategy-research dataset

Create a structured MLB 22-pick research dataset with missing fields represented explicitly, then begin event/date reconstruction.

Do not allow MLB research to compromise the blind-data architecture.

---

# 14. HANDOFF STATUS

Current repo-backed status as of Tuesday, August 18, 2026:

Known/complete:

- project background captured;
- Developer Pack present;
- MLB image present;
- voice-note transcript present;
- architectural intent interpreted;
- README created;
- progress checklist created;
- repository is runnable locally;
- root `pytest -q` now passes at `187 passed in 29.79s`;
- the currently loaded football migration slice is closed through:
  - Day 7 GAP statistics and lambda fitting;
  - Day 8 expected-goals conversion and corner-distribution evidence;
  - Day 9 Dixon-Coles scoreline layer;
  - Day 10 fair-price derivation;
-  - Day 11A manager core;
- the current repo shape is:
  - `58` Python modules under `code/src/sports_ai`;
  - `28` test files under `code/tests`;
  - `177` explicit `test_...` functions;
- the Day 10 football layer now derives:
  - `1X2`;
  - `BTTS`;
  - goals totals;
  - Asian handicap;
  - corner totals;
- Day 10 uses:
  - a strict sanitized pre-match pricing input;
  - the frozen Day 9 scoreline matrix as the single canonical goals object;
  - the frozen Day 8 corner object as the single canonical corners object;
  - settlement-aware fair-price arithmetic for push and quarter-line markets;
- the current repo-backed football archive artifacts include:
  - `9664` Day 10 sanitized pricing inputs;
  - `473536` Day 10 market-price rows;
  - `28992` Day 11A decision rows from the real historical/demo manager pass;
  - deterministic object lineage and acceptance audits for Day 7 through Day 11A.
- Day 10 now also has product-facing outputs under:
  - `research/product_outputs/football/day10/`;
  - `research/product_outputs/football/latest/`;
- Day 11A now also has manager-facing outputs under:
  - `research/product_outputs/football/day11/`;
  - `research/product_outputs/football/latest/`;
- the real Day 11A manager run currently reports:
  - `10299` `PRELIMINARY_CANDIDATE`;
  - `18693` `NO_BET`;
  - `DAY11_MANAGER_CORE_READY = YES`;
  - `LIVE_PAPER_PIPELINE_READY = NO`;
- the two pre-Day-11A handoff housekeeping fixes are now resolved:
  - the master handoff authority wording matches the August 15, 2026 PDF-first rule;
  - `pytest.ini` is physically included in generated handoff bundles;
- every completed milestone must now produce:
  - machine-readable artifacts;
  - reproducibility/version manifests;
  - a human-readable product/output report;
  - a compact `current/latest` artifact;
- the locked build order after Day 12 is now:
  - resolve the truthful xG/data-path blocker for the competitions where the football spec requires it;
  - then freeze the football logical pipeline;
  - then tennis Days 13 through 19;
  - then shared Days 20 through 23+.
- the actual Day 12 implementation run is now complete as an honest `PARTIAL`:
  - baseline before coding: `187 passed in 40.06s`;
  - full suite after implementation: `195 passed in 33.22s`;
  - real Day 12 input candidates: `10299`;
  - final Day 12 dispositions: `4317` `EXPERIMENTAL_CANDIDATE`, `5982` `BLOCKED_MISSING_XG_DATA`;
  - `DAY12 STATUS = PARTIAL`;
  - `FOOTBALL_LOGICAL_PIPELINE_READY = NO`;
  - `LIVE_PAPER_PIPELINE_READY = NO`;
  - `TENNIS AUTHORIZED = NO`.
- Day 11A has already ended with a manager-facing deliverable that can be opened directly and that shows preliminary candidate and no-bet outputs from supplied market snapshots.
- Day 11A does not claim live-paper readiness because live/current dependencies are still unresolved and explicitly tracked as such.
- the next run should be treated as the Day 12 implementation build, not as another Day 11A planning or handoff-preparation pass.
- the expected Day 12 deliverables now explicitly include:
  - `research/product_outputs/football/day12/FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md`;
  - `research/product_outputs/football/day12/football_day12_candidates.csv`;
  - `research/product_outputs/football/day12/football_day12_candidates.jsonl`;
  - `research/product_outputs/football/day12/football_day12_product_manifest.json`;
  - `research/spec_migration/FOOTBALL_DAY12_ACCEPTANCE_AUDIT_2026-08-19.md`.

Still unresolved / not yet proven:

- PostgreSQL production architecture is configured and verified end to end;
- football and tennis are split into separate repos/runtimes/databases;
- the full football-spec historical archive is loaded;
- a resolved live sharp-reference and odds-snapshot stack is connected;
- the 22 MLB dates are reconstructed;
- bookmaker hypotheses work out of sample;
- Day 12 safeguards / veto foundation is fully complete;
- point-in-time-valid xG coverage for the Day 12 safeguard is confirmed for the current repo;
- football logical pipeline is frozen and ready to authorize tennis;
- Day 11 has a shared later-layer semantics for any generic `model_probability` field beyond the current settlement-aware manager arithmetic;
- current 2026-27 football paper predictions have an as-of-current forward-production fit for Day 8 and Day 9 parameters;
- any statistical edge exists.

Codex should treat all unchecked work as genuinely pending, and should use the newer repo-backed
status documents and acceptance audits as the operational truth:

- `docs/PROJECT_STATUS.md`
- `docs/BLOCKERS.md`
- `docs/DECISION_LOG.md`
- `docs/EXPERIMENT_LOG.md`
- `docs/REQUIREMENTS_TRACEABILITY.md`
- `research/spec_migration/FOOTBALL_DAY7_ACCEPTANCE_AUDIT_2026-08-17.md`
- `research/spec_migration/FOOTBALL_DAY8_ACCEPTANCE_AUDIT_2026-08-17.md`
- `research/spec_migration/FOOTBALL_DAY9_ACCEPTANCE_AUDIT_2026-08-17.md`
- `research/spec_migration/FOOTBALL_DAY10_ACCEPTANCE_AUDIT_2026-08-18.md`
- `research/spec_migration/FOOTBALL_DAY11A_ACCEPTANCE_AUDIT_2026-08-19.md`
- `research/spec_migration/FULL_SPEC_ALIGNMENT_AUDIT_2026-08-17.md`

---

# 15. MASTER README — EMBEDDED COPY

The following is the current master README. Preserve it as working context.

---

# Sports Edge AI / Sports AI Betting Research System
## Master Project README — Official Working Context

**Project status:** Official development/research start  
**Working date:** 2026-08-13  
**Developer:** Joel Miguel Leysa  
**Client:** Cherona Chadwick  
**Primary technical baseline:** *Sports AI Betting Research System — V1 Developer Pack*, Version 1.0, dated 2026-08-11  
**Primary stack:** Python 3.11+ / PostgreSQL 15+  
**Current project mode:** Architecture implementation + strategy reverse-engineering + data/backtesting research

---

# 1. Executive Summary

This project is an evidence-driven sports betting research and pricing system.

The goal is **not** to build a generic AI that simply predicts who will win. The actual engineering objective is to construct a point-in-time, auditable system that:

1. independently evaluates sporting events without sportsbook contamination;
2. estimates fair probabilities and fair prices;
3. separately analyzes sportsbook odds, consensus, movement, and market behavior;
4. locks the independent sports prediction before market information is revealed;
5. locks the market analysis separately;
6. compares the independent price against the market only after both are frozen;
7. evaluates discrepancies, expected value, uncertainty, data quality, and risk;
8. studies historical successful betting examples to form explicit hypotheses;
9. backtests those hypotheses honestly on unseen historical data;
10. outputs a controlled decision such as:
   - `BET_CANDIDATE`
   - `WATCH`
   - `PASS`
   - `NO QUALIFYING BETS TODAY`;
11. explains the evidence behind each decision;
12. continuously evaluates calibration, closing-line value, model error, agent contribution, and live paper-trading performance.

The project's core north-star question is:

> Can we build a point-in-time, independently priced sports model plus a separate market-intelligence layer that identifies repeatable pricing discrepancies, and can we demonstrate that the signal survives unseen historical and live data?

---

# 2. Source Hierarchy and Project Authority

Several project descriptions have existed. They are not equally authoritative.

## 2.1 Current authority order

When requirements conflict, use the following precedence:

1. **Most recent explicit client instruction**
2. **Sports AI Betting Research System — V1 Developer Pack**
3. **Current documented architecture/research decisions**
4. **Earlier proposal/planning documents**

Do not silently rewrite the Developer Pack when the client changes direction. Instead:

- record the new requirement;
- classify it as a clarification, modification, or scope expansion;
- document the effect on architecture/data/timeline;
- preserve the original baseline for auditability.

## 2.2 Earlier proposal — partially superseded

An earlier Vectra Growth Studio proposal described:

- React frontend
- Spring Boot backend
- PostgreSQL
- Google Gemini
- third-party sports data
- REST APIs
- authentication
- workflow/business logic
- AI
- scheduling
- approvals
- settlements
- audit logs
- Docker

That earlier proposal used a roughly £8,000 phased structure.

The August 11, 2026 Developer Pack is now the stronger technical reference and changes the backend direction to **Python 3.11+ / PostgreSQL 15+**.

Do not build new core backend architecture around Spring Boot unless the client explicitly reintroduces that requirement.

---

# 3. Current Product Interpretation

The current strongest interpretation of Cherona's desired system is:

> Build a system that understands the underlying sporting event, understands how bookmakers price markets, understands how and why prices move, studies historical successful betting decisions, converts those observations into testable strategy hypotheses, and identifies which specific market appears materially mispriced.

This means the project is no longer only:

> "Build all 50 agents from the pack."

Instead, there are two connected workstreams.

## Workstream A — Formal Developer Pack Architecture

The formal football/tennis research platform:

- three intelligence zones;
- 50-agent architectural library;
- information firewall;
- PostgreSQL schema separation;
- prediction locks;
- market locks;
- structured outputs;
- simulations;
- fair-pricing engines;
- market intelligence;
- discrepancy/EV/risk/gatekeeping;
- backtesting;
- dashboard/API;
- evaluation and retraining.

## Workstream B — Strategy Reverse Engineering

Research based on:

- successful bettor promotional/history examples;
- the 22-play MLB run;
- voice notes describing bookmaker/odds reasoning;
- historical event conditions;
- historical prices;
- opening/current/closing movement;
- public-money or market-flow information where reliable;
- sports fundamentals;
- market structure.

The objective of Workstream B is not to copy a bettor's narrative.

The objective is to formulate hypotheses, encode them, and test whether they add predictive or pricing value.

The two workstreams eventually converge:

**Developer Pack architecture provides the controls and infrastructure.  
Strategy research influences which modules and rules deserve priority.**

---

# 4. Non-Negotiable Engineering Principles

These rules are load-bearing.

## 4.1 Absolute market firewall

Zone A sports-analysis components must never receive:

- current sportsbook odds;
- opening odds;
- closing odds;
- sportsbook prices;
- implied market probabilities;
- betting percentages;
- money/ticket splits;
- line movement;
- market consensus;
- bookmaker commentary;
- betting articles/tipster analysis discussing prices;
- other published predictions that incorporate betting markets;
- ratings derived from sportsbook markets.

Enforce this mechanically, not socially.

Required controls include:

- PostgreSQL schemas/roles;
- database grants;
- application allow-lists;
- recursive contamination scanning;
- input schema validation;
- restricted tools/retrieval;
- prompt restrictions if LLMs are used;
- workflow ordering;
- automated firewall tests.

## 4.2 Prediction lock before market reveal

The independent fair-price result must be stored and frozen before the market layer can reveal sportsbook information.

The lock should include:

- `event_id`
- sport
- model version
- timestamp
- canonical payload hash
- prior lock hash if using a hash chain

After locking, historical prediction content must be immutable.

## 4.3 Market lock

Market analysis is separately frozen before final decision logic.

Raw odds may continue to be recorded afterward for:

- closing-line tracking;
- movement history;
- settlement/evaluation.

But the pre-decision market interpretation used for that decision must remain immutable.

## 4.4 Point-in-time correctness

Every historical feature must answer:

> What was actually known at time T?

Use `as_of` timestamps throughout.

Never accidentally apply:

- final lineups to earlier predictions;
- current injury data to historical dates;
- future ratings;
- season aggregates containing the predicted event;
- closing odds to entry-time decisions;
- corrected post-event statistics to pre-event models.

Backtest and live systems should use the **same payload builder** wherever possible.

## 4.5 `INSUFFICIENT_DATA` is a valid result

Never force an agent to fabricate certainty.

An agent can return:

- `OK`
- `INSUFFICIENT_DATA`

Low-quality data can force:

- agent abstention;
- wider uncertainty;
- `PASS`.

## 4.6 `PASS` is a valid product outcome

The system is not required to generate a bet every day.

`NO QUALIFYING BETS TODAY` is correct behavior when no event passes the required thresholds.

## 4.7 Price matters more than pick identity

The same event can be attractive at one price and poor at another.

The system must think in terms of:

- probability;
- fair odds;
- break-even probability;
- minimum acceptable price;
- expected value;
- uncertainty.

Not simply:

> "Team X will win."

## 4.8 No equal agent voting

Never use:

> 12 agents agree vs 8 agents disagree.

Agents often encode overlapping information.

Use:

- calibrated model stacking;
- regularized logistic regression;
- gradient boosting where justified;
- versioned hand-set cold-start weights;
- correlation analysis;
- agent reliability tracking.

Correlated evidence must not be double-counted.

## 4.9 Statistical validation cannot be rushed

Implementation speed and validation speed are different variables.

It may be possible to produce working software quickly.

It is not possible to prove durable edge from a very small sample merely by coding faster.

---

# 5. Three-Zone Architecture

## Zone A — Blind Sports Intelligence

Purpose:

- model the sporting event independently;
- estimate probabilities/fair prices;
- evaluate uncertainty;
- remain completely blind to markets.

Typical outputs:

- primary score/probability;
- confidence;
- data quality;
- side advantage;
- key reasons;
- risk flags;
- uncertainty;
- sport-specific details.

Zone A ends at the sport's fair-price maker and **prediction lock**.

## Zone B — Market Intelligence

Runs only after Zone A is locked.

Purpose:

- collect odds;
- devig prices;
- estimate market consensus;
- characterize line movement;
- identify bookmaker disagreement;
- analyze opening/current/best prices;
- where reliable, inspect flow/ticket/money information;
- distinguish leader/follower books if data supports it.

Zone B must initially be blind to the internal fair price so its market description is independent.

Zone B ends at the **market lock**.

## Zone C — Decision and Risk

Only runs once both locks exist.

Can see:

- Zone A fair probabilities;
- Zone A uncertainty/data quality;
- Zone B odds;
- Zone B movement;
- historical agent reliability;
- strategy rules;
- portfolio/risk state.

Responsibilities:

- M06 discrepancy;
- M07 EV/value;
- M08 adversarial skeptic;
- M09 exposure/risk;
- M10 final gate.

---

# 6. Full Agent Architecture

An "agent" is a software responsibility, not automatically an LLM.

An agent may be:

- deterministic mathematics;
- statistics;
- simulation;
- machine learning;
- rules engine;
- data pipeline;
- structured qualitative model;
- LLM-backed contextual analysis where appropriate.

Every production agent should define:

- identifier;
- purpose;
- zone;
- sport;
- allowed inputs;
- forbidden inputs;
- update frequency;
- method;
- structured outputs;
- confidence;
- data quality;
- uncertainty;
- failure conditions;
- unit tests;
- firewall tests;
- backtest report;
- model/version metadata.

## 6.1 Football agents F01–F20

- F01 Team Strength
- F02 xG
- F03 Shot Quality
- F04 Attack
- F05 Defence
- F06 Possession & Progression
- F07 Pressing & Transition
- F08 Set Piece
- F09 Goalkeeper
- F10 Player Availability
- F11 Starting XI
- F12 Fatigue & Schedule
- F13 Home/Away
- F14 Tactical Matchup
- F15 Manager
- F16 Form Quality
- F17 Weather & Pitch
- F18 Scoreline Model
- F19 Football Simulation
- F20 Football Fair-Odds Maker

F19 should simulate market-relevant outcomes, potentially including:

- 1X2;
- O/U;
- BTTS;
- DNB;
- Asian handicap.

F20 combines valid Zone A outputs into a calibrated, independent fair-price surface and is frozen before sportsbook data is revealed.

## 6.2 Tennis agents T01–T20

- T01 Player Strength
- T02 Surface Elo
- T03 Serve
- T04 Return
- T05 Hold/Break
- T06 Recent Form
- T07 Opponent Quality
- T08 Fatigue
- T09 Physical Condition
- T10 Travel & Rest
- T11 H2H
- T12 Playing Style
- T13 Handedness
- T14 Court Condition
- T15 Weather
- T16 Pressure / Tiebreak
- T17 Challenger Specialist
- T18 ATP Main Tour Specialist
- T19 Tennis Match Simulation
- T20 Tennis Fair-Odds Maker

## 6.3 Shared market/risk agents M01–M10

- M01 Odds Collection
- M02 Consensus & Devig
- M03 Line Movement
- M04 Bookmaker Leader / Follower
- M05 Market Flow
- M06 Discrepancy Detector
- M07 Value / EV
- M08 Adversarial Skeptic
- M09 Risk & Portfolio
- M10 Master Gatekeeper

---

# 7. Formal V1 Scope

## Football

Competitions:

- Premier League
- Championship
- League One
- League Two
- Scottish Premiership
- Scottish Championship
- Scottish League One
- Scottish League Two

Core markets:

- 1X2
- DNB
- Asian Handicap
- O/U 2.5
- BTTS

## Tennis

Competitions:

- ATP Tour
- ATP Challenger
- men's singles

Markets:

- match winner
- game handicap
- set handicap
- match-games totals

## Explicit V1 exclusions until proven

Do not automatically expand to:

- WTA
- ITF
- doubles
- international football
- European club competitions
- corners
- cards
- player props
- automated bet placement
- aggressive stake sizing
- additional sports

unless explicitly approved as scope changes.

---

# 8. MLB Research and Scope Status

The Sean Perry Wins MLB 18–4 sample introduces a separate scope question.

The Developer Pack does **not** formally include MLB.

Therefore MLB currently has two possible roles:

### Role A — Strategy research only

Use MLB examples to understand:

- pricing discrepancy;
- market movement;
- selection construction;
- ML vs run-line choice;
- totals selection;
- entry timing;
- bookmaker behavior.

No MLB production agents required yet.

### Role B — Production sport vertical

If Cherona explicitly wants production MLB support, this is a scope expansion requiring MLB-specific:

- data ingestion;
- entities;
- models;
- feature store;
- simulations;
- agents;
- backtests;
- market definitions;
- acceptance gates.

Potential MLB modules would include:

- starting pitcher;
- pitcher handedness/splits;
- bullpen;
- team offense;
- platoon matchup;
- projected lineup;
- injuries;
- park factors;
- weather;
- travel/rest;
- recent schedule;
- expected-runs model;
- simulation;
- fair odds;
- market movement;
- discrepancy;
- EV;
- risk.

**Do not silently assume MLB is production V1.**

---

# 9. Sean Perry MLB 18–4 Research Sample

Promotional image summary:

- 22 plays
- 18 wins
- 4 losses
- advertised 81.8% win rate

Recorded plays:

1. Mets vs Phillies U9.5 — win
2. Royals +1.5 — win
3. Tigers -1.5 — win
4. Dodgers vs Yankees U9 — win
5. Brewers -1.5 — win
6. Tigers ML — loss
7. Marlins vs Astros U8 — win
8. Royals vs Tigers U8.5 — win
9. Giants ML — win
10. Dodgers ML — win
11. Brewers -1.5 — win
12. Red Sox ML — win
13. Padres ML — win
14. Red Sox ML — win
15. Dodgers ML — win
16. Brewers ML — win
17. Dodgers ML — loss
18. Padres ML — win
19. Padres +1.5 — loss
20. Tigers +1.5 — win
21. Padres vs Diamondbacks U9.5 — loss
22. Red Sox ML — win

Market aggregation:

- Moneyline: 11 plays, 9–2
- Run line: 6 plays, 5–1
- Totals: 5 plays, 4–1

Repeated teams:

- Dodgers
- Brewers
- Red Sox
- Padres
- Tigers
- Royals

This sample is **not sufficient to infer a strategy** because the image does not provide:

- exact event dates;
- pick release timestamps;
- sportsbook;
- entry price;
- opening price;
- closing price;
- full price path;
- starting pitchers;
- lineup status;
- injury state;
- bullpen state;
- weather;
- venue/park conditions;
- consensus market;
- public money;
- ticket share;
- stake;
- full context available at the time.

The job is to reconstruct this context where possible.

---

# 10. Voice-Note Strategy Knowledge

The additional voice note describes a claimed strategy centered on market-price discrepancy and sportsbook behavior.

Important: this is **strategy-source material**, not verified fact.

## 10.1 Patriots/Seahawks example

The speaker describes:

- Patriots available around +4.5;
- an asserted "true" or expected line around +10.5;
- roughly six points of discrepancy;
- claimed 86% of money on the Patriots;
- little/no corresponding line movement;
- interpretation that the sportsbook was comfortable holding Patriots liability;
- Seahawks ultimately winning by 16.

The speaker interprets line resistance as evidence of bookmaker conviction.

That interpretation must be tested. It cannot be directly encoded as truth.

## 10.2 General moneyline discrepancy example

The speaker also describes a situation such as:

- available price around -150;
- claimed expected sportsbook price around -250;
- discrepancy interpreted as intentional or exploitable pricing behavior.

Again, this is a hypothesis source.

## 10.3 Claimed early-price / sharp-movement behavior

The voice note suggests some MLB selections may initially be available at relatively moderate prices and then move after high-stake bettors act.

This implies important research variables:

- opening price;
- time of pick;
- price at pick release;
- price after release;
- closing price;
- movement velocity;
- market depth/limits where available;
- book identity.

## 10.4 Claims that must remain unverified until evidence is obtained

Examples include:

- sustained 80%+ MLB accuracy;
- very large personal bet sizes;
- recurring 6–1, 8–2, or 9–1 package performance;
- specific profit claims;
- causal assertions that a no-move line means the sportsbook "knows" one side will lose.

These should be tagged internally as:

`UNVERIFIED_CLAIM`

They may motivate research, but should not be used as training truth.

---

# 11. Strategy Hypotheses to Test

The reverse-engineering process should translate narrative into falsifiable variables.

## H1 — Independent price discrepancy

Measure:

`independent fair probability - market implied probability`

or, for spreads/totals:

`model fair line - market line`

Test whether larger discrepancies predict:

- better CLV;
- better calibrated outcomes;
- positive out-of-sample EV.

## H2 — Line resistance

Potential pattern:

- high public/money concentration on one side;
- little line movement;
- or movement opposite the popular side.

Test whether such behavior adds information after controlling for:

- opener;
- sharp-book movement;
- market-wide consensus;
- time to event;
- liquidity;
- injuries/lineup news;
- known information shocks.

## H3 — Cross-book disagreement

Potentially identify:

- stale books;
- recreational books;
- leader/follower behavior;
- off-market prices.

## H4 — Early-price capture

Test whether selections systematically achieve favorable:

- entry-vs-close movement;
- positive CLV;
- directional market confirmation after release.

## H5 — Market choice

Test whether the bettor appears to choose:

- ML vs run line according to projected margin;
- underdog +1.5 under particular distributional profiles;
- totals when run/goal expectation is more mispriced than side markets.

## H6 — Price-band preference

Test whether picks cluster in particular odds bands.

Do not optimize price-band thresholds on the 22 picks and then report success on the same sample.

## H7 — Sports/context confirmation

Test whether market signals only work when combined with:

- pitcher/lineup edges;
- team strength;
- fatigue;
- weather;
- venue;
- tactical matchup;
- injury information.

---

# 12. Historical Reconstruction Data Model

A normalized reconstruction row may include:

```text
bet_id
event_id
game_date
sport
league
team_a
team_b
market_type
selection
line
entry_odds
entry_timestamp
opening_odds
opening_timestamp
closing_odds
closing_timestamp
result
stake
starting_pitcher_a
starting_pitcher_b
lineup_state
injuries
form_a
form_b
strength_a
strength_b
bullpen_metrics
weather
venue
referee
market_consensus
ticket_pct
money_pct
line_movement
movement_velocity
bookmaker
book_class
independent_probability
market_probability
fair_line
market_line
edge
uncertainty
data_quality
strategy_version
source_quality
reconstruction_confidence
```

Not all fields apply to every sport.

Historical rows must distinguish:

- known;
- reconstructed;
- inferred;
- unavailable.

Do not silently fill missing values.

---

# 13. Strategy Rule Versioning

Never silently change strategy criteria.

Example:

```text
RULE_VERSION = v0.1
SPORT = MLB
MARKET = TOTAL

REQUIRE:
    data_quality >= 80
    starting_pitcher_data_complete = true
    market_consensus_available = true

EDGE:
    fair_probability - market_probability >= 0.04

RISK:
    uncertainty <= configured_threshold

OUTPUT:
    CANDIDATE / WATCH / PASS
```

If thresholds change:

- preserve v0.1;
- create v0.2;
- record reason;
- rerun walk-forward evaluation;
- compare objectively.

---

# 14. Metrics

## Primary model-quality metrics

Use:

- calibration curve;
- calibration slope;
- Brier score;
- log loss;
- uncertainty coverage;
- data quality;
- out-of-sample performance;
- CLV / closing-line proxy;
- agent marginal value.

## Secondary operational/business metrics

May include:

- hit rate;
- P/L;
- ROI;
- number of candidates;
- pass rate;
- watch rate;
- average edge;
- market movement after decision.

Do not use short-term P/L or win percentage as the only acceptance standard.

---

# 15. Data Quality

Suggested interpretation from the Pack:

- 95–100: excellent
- 85–94: strong
- 70–84: acceptable
- 50–69: weak
- below 50: automatic PASS

Competition-specific thresholds may differ.

Examples:

- ATP Tour minimum around 75 initially;
- ATP Challenger minimum around 85;
- football starting thresholds around 70, then tuned by backtest.

Potential deductions:

- missing lineup;
- missing injury information;
- small sample;
- new manager;
- promoted team;
- returning player;
- sparse Challenger data;
- missing market history;
- uncertain player/entity identity.

---

# 16. Data Sources — Baseline Direction

Licensing, pricing, availability, and terms must be verified before production use.

## Football

Potential sources named in the Pack include:

- football-data.co.uk
- FBref
- Understat
- API-Football
- Sportmonks
- StatsBomb
- Stats Perform / Opta
- official club/league sources
- BBC/team-news sources

Important:

Any feed/file containing odds data is treated as market-zone material.

Odds columns must be separated before sports data enters Zone A.

Lower-league data quality may be inadequate for some advanced agents. Returning `INSUFFICIENT_DATA` or making some competitions no-bet zones is acceptable.

## Tennis

Potential research/operations sources include:

- Jeff Sackmann tennis datasets;
- tennis-data.co.uk;
- official ATP/Challenger sources;
- paid sports APIs;
- Tennis Abstract where appropriate.

Historical odds must remain isolated from Zone A.

## Odds / Market

Requirements eventually include:

- opening;
- current;
- best price;
- book identity;
- timestamp;
- historical movement;
- closing;
- consensus;
- devig;
- potentially exchange pricing;
- potentially money/ticket information where data quality is known.

---

# 17. Existing Python Skeleton

The Developer Pack already includes useful scaffolding.

Existing concepts/components include:

- standard agent contract;
- firewall;
- contamination scanner;
- lock/hash logic;
- data-quality module;
- tennis Elo;
- tennis simulation;
- devig;
- pipeline/orchestration skeleton;
- evaluation metrics;
- CLV;
- tests.

Still requiring implementation/integration:

- real data ingestion;
- PostgreSQL wiring;
- remaining agents;
- meta-model;
- production orchestration;
- historical backtesting;
- live/paper workflow;
- API;
- dashboard;
- monitoring;
- deployment hardening.

Do not rebuild already-working components merely to rewrite them.

First inspect and test them.

---

# 18. Recommended Build Order

## Immediate engineering sequence

1. inspect repository/package contents;
2. create Python 3.11+ environment;
3. install dependencies;
4. run complete test suite;
5. run synthetic/demo pipeline;
6. trace agent contract;
7. trace firewall;
8. deliberately test contaminated payload rejection;
9. trace prediction lock;
10. trace market lock;
11. inspect tennis Elo;
12. inspect simulation;
13. inspect devig;
14. inspect metrics;
15. inspect orchestration;
16. document implemented vs missing components;
17. configure PostgreSQL;
18. apply provided schema;
19. configure separate roles/DSNs;
20. verify blind role cannot query `market.*`;
21. verify ledger immutability;
22. verify point-in-time query semantics;
23. build the first real end-to-end slice.

---

# 19. Minimum Honest Loop

Do **not** build all 50 agents immediately.

The first formal slice should remain small enough to debug.

Developer Pack baseline for ATP:

### Blind / Tennis

- T01 Elo / player strength
- T02 surface Elo
- T03 serve
- T04 return
- T05 hold/break
- T19 simulation
- T20 fair odds

### Market / Decision

- M01 odds collection
- M02 consensus/devig
- M06 discrepancy
- M07 value/EV
- simple M10 gatekeeper

Purpose:

> prove the architecture end-to-end before expanding agent count.

---

# 20. Formal Developer Pack Roadmap

## Phase 0 — Foundations

Target scope:

- repository;
- environments;
- CI;
- PostgreSQL schema;
- DB roles/grants;
- immutability;
- entities/IDs;
- seed data;
- historical foundations;
- `as_of` correctness.

Acceptance:

- schema active;
- immutability tests pass;
- historical data queryable;
- blind role cannot read market schema.

## Phase 1 — Minimum Honest Loop

ATP Tour first.

Deliver:

- firewall payload builder;
- structured validation;
- limited tennis agents;
- T20;
- prediction lock;
- M01;
- M02;
- market lock;
- M06;
- M07;
- simple M10;
- CLI daily run.

Acceptance:

- complete daily run;
- prediction locked before market reveal;
- decisions generated with price information;
- funnel logged;
- zero firewall violations.

## Phase 2 — Backtest Slice

Requirements:

- same live/backtest payload builder;
- walk-forward evaluation;
- calibration;
- log loss;
- Brier;
- CLV proxy;
- learned initial meta-model;
- leakage review.

Baseline walk-forward described in the Pack:

- train: 2021–2023
- validation: 2024
- test: 2025

An example acceptance metric is ATP match-winner calibration slope roughly 0.9–1.1 plus documented CLV proxy and leakage sign-off.

## Phase 3 — Full Tennis + Decision Stack

Add:

- T06–T18;
- Challenger;
- M03/M04;
- M08;
- M09;
- full M10;
- dashboard v1;
- continuous paper trading.

Each new agent must have:

- contract;
- tests;
- `INSUFFICIENT_DATA` path;
- firewall test;
- backtest evidence.

## Phase 4 — Football

Start with ENG1/ENG2 data spine.

Core first:

- F01
- F02
- F16
- F18
- F19
- F20

Backtest core before adding F03–F17.

Enable lower leagues last.

Accept the possibility that some lower-data competitions remain no-bet.

## Phase 5 — Evaluation Hardening

- accumulate live CLV;
- per-agent performance jobs;
- monthly reports;
- funnel monitoring;
- failover tests;
- hash-chain audit;
- paper trading;
- acceptance-gate hardening.

## Phase 6 — Expansion Only After Proof

Potentially:

- corners;
- cards;
- props;
- WTA/ITF;
- additional football;
- stake sizing;
- automated bet placement where lawful.

Not before the evidence gates pass.

---

# 21. Accelerated Working Timeline vs Statistical Reality

The client prefers faster visible implementation.

A practical accelerated engineering target may be:

### First focused hours

- environment runs;
- tests pass/fail documented;
- demo traced;
- architecture mapped;
- gaps identified.

### 1–2 working days

- basic functional core loop.

### 3–5 days

- connected MVP if data/API access cooperates.

### 5–10 days

- first meaningful backtesting/agent-selection evidence.

### Weeks/months

- robust validation;
- broad football/tennis;
- live paper-trading evidence;
- hardened operational system.

This accelerated schedule is **not** a substitute for the formal acceptance gates.

---

# 22. Backtesting Protocol

## Mandatory rules

- point-in-time data only;
- no random leakage-prone train/test shuffle for temporal sports models;
- use walk-forward splits;
- lock thresholds/model versions;
- never use closing info as an entry-time feature;
- evaluate on unseen samples;
- retain failed experiments;
- preserve experiment metadata;
- compare against baselines;
- compute confidence intervals where practical.

## Recommended experiment record

```text
experiment_id
created_at
sport
competition
market
feature_version
agent_versions
strategy_version
train_window
validation_window
test_window
data_source_versions
sample_count
candidate_count
brier
log_loss
calibration_slope
clv
roi
hit_rate
max_drawdown
notes
approved
```

---

# 23. Anti-Overfitting Rules for the 22 MLB Picks

Do not:

- invent a rule simply because it explains winners;
- optimize thresholds on those 22 events and call it proof;
- use result-dependent features;
- use closing lines as if known at pick time;
- assume repeated team identity is causal;
- assume the promotional run is complete/unselected;
- assume claimed record quality without independent verification.

Correct workflow:

1. reconstruct;
2. document missing data;
3. create hypotheses;
4. test on a broader historical population;
5. lock rules;
6. test unseen periods;
7. compare to market baseline;
8. retain or reject.

---

# 24. Market-Behavior Research Requirements

The voice note makes this a major priority.

The platform should eventually support analysis of:

- opening line;
- current line;
- best line;
- closing line;
- price path;
- timestamped snapshots;
- spread/total movement;
- cross-book disagreement;
- consensus;
- devig;
- sharp vs recreational book movement if classification is defensible;
- exchanges;
- public ticket percentage where available;
- public money percentage where available;
- line resistance;
- reverse movement;
- market reaction to injuries/lineups;
- movement immediately after a pick is posted.

Important:

"No movement despite heavy public money" is a feature candidate — not automatic proof of sportsbook knowledge.

---

# 25. Dashboard Direction

Do not prioritize visual polish before the research loop works.

A useful decision card/dashboard eventually displays:

- event;
- sport;
- competition;
- market;
- blind probability;
- fair odds;
- fair line;
- sportsbook price;
- implied probability;
- devigged probability;
- market consensus;
- opening/current/closing;
- movement;
- edge;
- EV;
- uncertainty;
- data quality;
- risk flags;
- relevant agent evidence;
- lock status;
- strategy/model version;
- candidate/watch/pass;
- explanation;
- later result;
- CLV;
- evaluation history.

---

# 26. Internal API Direction

The Developer Pack includes internal API concepts around:

- events/runs;
- predictions/locks;
- market views;
- decisions;
- dashboard;
- evaluation;
- monitoring;
- administration.

API design should preserve:

- zone security;
- immutable histories;
- clear event/version identifiers;
- point-in-time semantics;
- read-only access to locked records;
- auditable decision lineage.

---

# 27. Suggested Repository Structure

A practical repository could look like:

```text
sports-edge-ai/
├── README.md
├── PROGRESS.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml
├── docs/
│   ├── architecture/
│   ├── decisions/
│   ├── research/
│   ├── experiments/
│   ├── data-sources/
│   └── runbooks/
├── manifest/
├── schemas/
├── sql/
├── src/
│   └── sports_ai/
│       ├── agents/
│       │   ├── football/
│       │   ├── tennis/
│       │   ├── market/
│       │   └── mlb_research/
│       ├── data/
│       ├── features/
│       ├── firewall/
│       ├── locking/
│       ├── evaluation/
│       ├── backtesting/
│       ├── pipeline/
│       ├── api/
│       └── monitoring/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── firewall/
│   ├── leakage/
│   └── backtest/
├── notebooks/
│   └── research_only/
└── scripts/
```

Research notebooks must not become uncontrolled production logic.

Anything validated in notebooks should be migrated into versioned/tested modules.

---

# 28. Engineering Definition of Done for an Agent

An agent is not "done" because it returns output.

Before non-zero production influence, require:

- [ ] documented purpose
- [ ] zone defined
- [ ] input contract
- [ ] allowed fields
- [ ] forbidden fields
- [ ] output schema
- [ ] deterministic reproducibility where applicable
- [ ] data-quality behavior
- [ ] uncertainty behavior
- [ ] `INSUFFICIENT_DATA` path
- [ ] unit tests
- [ ] bad-input tests
- [ ] firewall contamination test
- [ ] historical backtest
- [ ] marginal-value comparison
- [ ] version number
- [ ] logging/monitoring
- [ ] approval for non-zero meta-model weight

A useless agent should be removed or weighted to approximately zero rather than retained simply because the blueprint says "50."

---

# 29. Failure Modes to Actively Defend Against

## Data leakage

Symptoms:

- spectacular backtest;
- weak live results.

Possible causes:

- future stats;
- closing prices;
- post-event corrections;
- current injury status in historical data;
- odds-derived features entering Zone A.

## Firewall erosion

Temporary shortcuts are dangerous.

Never allow:

> "just pass this odds field for debugging."

## Overfitting decision policy

Avoid tuning until historical profit looks attractive.

## Trusting P/L over model quality

Short periods can be noisy.

Track CLV/calibration/error metrics.

## Thin-data hallucination

Return insufficient data.

## Equal-vote ensembles

Correlated agents double-count evidence.

## Narrative overrides

A persuasive LLM explanation must never override the numeric decision engine.

## Silent drift

Monitor data providers, missing fields, entity matching, API changes, league/season changes.

## Mutable history

Locked predictions/market reports/decisions must remain immutable.

## Scope creep before proof

Do not simultaneously add every sport, market, prop, and UI feature.

---

# 30. Responsible Product Boundaries

The formal Developer Pack describes a research system, not an automatic wagering executor.

The system must not communicate certainty through labels such as:

- GUARANTEED
- CERTAIN
- LOCK

Use controlled uncertainty and evidence.

Any future automated bet execution or staking logic would require:

- explicit scope approval;
- jurisdiction/legal review;
- risk controls;
- independent operational safeguards.

---

# 31. Client/Employment Working Context

Documented arrangement from prior discussion:

- client requested approximately 8 hours/day;
- likely 40 hours/week;
- monthly compensation discussed/accepted: **$850/month**;
- payment described as biweekly;
- signup/start payment discussed: **$250**;
- possible future increase after launch/revenue was mentioned but **not quantified**.

Do not treat an unspecified future increase as guaranteed compensation.

Items that should remain documented:

- official start date;
- signup payment status;
- first biweekly payment date;
- exact biweekly amount;
- payment method;
- expected days/hours;
- defined scope;
- termination/change expectations.

As of this README, the project is being treated as **officially started on 2026-08-13 based on the current conversation**, but payment-receipt details should only be marked complete when actually confirmed.

---

# 32. Communication Rules with Cherona

When writing client-facing messages:

- conversational;
- concise;
- professional;
- confident;
- technically informed;
- not corporate-heavy;
- not defensive;
- not overly apologetic.

When scope changes:

- acknowledge the goal;
- explain technical impact plainly;
- identify whether it changes data, agents, timeline, or validation;
- avoid emotional language.

Do not overpromise statistical results.

Good framing:

> Implementation can move quickly. Validation still requires enough historical/live evidence to determine which signals actually contribute predictive value.

---

# 33. Decisions That Need Explicit Clarification

Track these as open issues.

## Scope

- Is MLB only reverse-engineering research or a production sport?
- Does the client still accept tennis-first implementation from the Developer Pack?
- Are corners/cards now required in V1 or later?
- Are bookmaker-flow/ticket datasets actually available?
- Which sportsbook(s) must be supported first?
- Which jurisdiction/operator environment matters?

## Data

- What sports API access already exists?
- What historical odds source will be used?
- Can we obtain timestamped intraday line movement, not merely open/close?
- Is money/ticket data available and licensed?
- Which live weather/injury/lineup sources will be used?

## Product

- The active milestone deliverable format is now a human-readable Markdown report plus CSV, JSONL, and manifest artifacts, with a compact `current/latest` copy under `research/product_outputs/`.
- Is there an existing repository?
- Is deployment expected immediately?
- Is user authentication required in early MVP?
- Who consumes the output?

## Commercial

- signup fee status;
- first payment date;
- payment method;
- exact scope under current compensation;
- change-request process.

---

# 34. Immediate Official-Start Plan

At official start, do the following in order.

## Track A — Developer Pack / Code

1. obtain/inspect the actual code skeleton;
2. create environment;
3. run tests;
4. run demo;
5. map repository;
6. verify firewall;
7. verify locks;
8. inspect DB DDL;
9. configure PostgreSQL;
10. run security/immutability tests;
11. document technical gaps.

## Track B — Strategy Research

1. normalize the 22 MLB plays;
2. identify likely dates;
3. identify actual games;
4. reconstruct sportsbook/market context;
5. reconstruct pre-pick sports context;
6. assign source/reconstruction confidence;
7. build feature matrix;
8. formulate hypotheses from the voice note;
9. avoid peeking at outcome-derived features;
10. define broad historical comparison cohorts;
11. backtest;
12. retain/reject each hypothesis.

## Track C — Documentation

Create/maintain:

- README;
- progress checklist;
- decision log;
- scope-change log;
- data-source registry;
- experiment registry;
- model/strategy version log;
- client requirement notes.

---

# 35. First Concrete Deliverables

The first meaningful outputs should be:

## Deliverable 1 — Architecture/Gaps Report

- what the Pack already provides;
- what runs;
- what fails;
- what is missing;
- immediate dependencies.

## Deliverable 2 — Strategy Map

- claimed strategy mechanisms;
- variables needed;
- testable hypotheses;
- evidence requirements;
- unsupported claims separated.

## Deliverable 3 — MLB Reconstruction Dataset v0

At least the 22 picks normalized into a stable schema with missing fields explicit.

## Deliverable 4 — Data Source Plan

For:

- sports fundamentals;
- odds;
- line movement;
- weather;
- injuries/lineups;
- historical results;
- settlement;
- market consensus/flow.

## Deliverable 5 — Minimum Honest Loop

A small functioning end-to-end pipeline with locking.

## Deliverable 6 — Backtest Harness

Same payload semantics as live.

## Deliverable 7 — Initial Research Report

What strategy hypotheses:

- appear promising;
- fail;
- cannot yet be tested;
- need better data.

---

# 36. Daily Work Log Template

Use this each workday.

```markdown
# Daily Log — YYYY-MM-DD

## Objectives
- ...

## Completed
- ...

## Evidence / outputs
- test result:
- commit:
- dataset:
- report:

## Issues
- ...

## Decisions
- ...

## Client inputs / requirement changes
- ...

## Scope impact
- none / minor / material

## Next
- ...

## Blockers requiring client response
- ...
```

---

# 37. Decision Log Template

```markdown
## ADR-XXX — Decision title

Date:
Status: proposed / accepted / superseded

Context:
...

Decision:
...

Reason:
...

Alternatives:
...

Consequences:
...

Affected components:
...

Requires client approval:
yes/no
```

---

# 38. Research Hypothesis Template

```markdown
## HYP-XXX — Hypothesis

Source:
- voice note / bettor sample / model analysis / market research

Claim:
...

Variables required:
...

Pre-event cutoff:
...

Cohort:
...

Control/baseline:
...

Metric:
...

Acceptance threshold:
...

Train period:
...

Validation period:
...

Test period:
...

Result:
pending / supported / rejected / inconclusive

Notes:
...
```

---

# 39. Data-Source Registry Template

```text
source_id
provider
dataset/feed
sport
coverage
historical/live
zone
contains_odds
license
cost
rate_limit
latency
update_frequency
point_in_time_quality
known_gaps
fallback_source
approved
```

Any source containing odds should be classified as Zone B unless safely separated before entry into blind features.

---

# 40. Model / Strategy Version Rules

Every meaningful model or decision-policy change creates a new version.

Do not edit history in place.

Version:

- agent algorithms;
- feature definitions;
- meta-model weights;
- calibration;
- risk thresholds;
- minimum edge;
- data-quality thresholds;
- strategy rules.

Every decision should be reproducible from:

- event snapshot;
- feature version;
- model versions;
- strategy version;
- data timestamps;
- market snapshot;
- code revision.

---

# 41. Testing Strategy

At minimum:

## Unit tests

- Elo;
- devig;
- simulations;
- probability normalization;
- odds conversion;
- EV;
- data-quality logic.

## Firewall tests

- direct odds key;
- nested odds key;
- price language in text;
- market-derived rating;
- hidden nested contamination.

Expected behavior:

> hard reject and log violation.

## Lock tests

- prediction immutable after lock;
- market report immutable after lock;
- hash verifies;
- insert/update/delete restrictions.

## Point-in-time tests

- future row not returned;
- pre-event cutoff enforced;
- backtest/live payload parity.

## Integration tests

- complete daily event flow;
- failure/retry;
- re-run after lineup update;
- provider outage;
- `INSUFFICIENT_DATA`;
- no qualifying candidates.

---

# 42. Current Definition of Success

Success is not:

- a beautiful dashboard;
- 50 agent names;
- a promotional 80% win rate;
- a backtest optimized until profitable.

Success is:

1. a reproducible data pipeline;
2. strict information separation;
3. honest point-in-time backtesting;
4. calibrated fair probabilities;
5. stable market comparisons;
6. explicit uncertainty;
7. measurable CLV/model quality;
8. agents that prove marginal value;
9. auditable decisions;
10. a system willing to abstain;
11. evidence that survives unseen/live data.

---

# 43. Current Project North Star

Keep this statement at the top of technical decision-making:

> Build the smallest auditable system capable of independently pricing an event, separately understanding the market, identifying a statistically defensible discrepancy, and proving that the discrepancy is repeatable without leakage or hindsight.

Everything else is secondary until that loop works.



---

# 16. MASTER PROGRESS CHECKLIST — EMBEDDED COPY

The following is the current operational progress checklist. Update status only when supported by actual evidence.

---

# Sports Edge AI — Master Progress Checklist

**Official working start:** 2026-08-13  
**Status legend:**  
- [x] completed / known
- [ ] not completed or not yet verified
- [~] actively in progress
- [?] requires clarification

---

# A. Project / Administrative

- [x] Project context assembled
- [x] Developer Pack received
- [x] MLB 18–4 screenshot received
- [x] Additional strategy voice-note transcript received
- [x] Official project start stated in current conversation
- [ ] Confirm $250 signup/start payment received
- [ ] Confirm exact first biweekly payment date
- [ ] Confirm exact biweekly payment amount
- [ ] Confirm payment method
- [ ] Confirm normal workdays
- [ ] Confirm expected 8-hour-day arrangement
- [ ] Confirm scope-change approval process
- [ ] Confirm termination/change expectations
- [ ] Create written client requirements log
- [ ] Create scope-change log

# B. Scope Clarification

- [?] MLB research-only vs production vertical
- [?] Tennis-first formal MVP vs another sport first
- [?] Are corners/cards formally V1 or post-V1?
- [?] Which sportsbook(s) are first-class targets?
- [?] Is exchange/Betfair data available/desired?
- [?] Is public money/ticket data available?
- [?] Which jurisdiction/operator constraints matter?
- [x] Milestone product-output standard defined: Markdown report plus CSV/JSONL/manifest and a compact `current/latest` copy

# C. Source / Requirements Review

- [x] Developer Pack high-level architecture understood
- [x] Three-zone architecture understood
- [x] Firewall requirement understood
- [x] Prediction lock requirement understood
- [x] Market lock requirement understood
- [x] `INSUFFICIENT_DATA` requirement understood
- [x] No-equal-voting principle understood
- [x] Price-vs-pick principle understood
- [x] MLB bettor sample normalized manually at list level
- [x] Voice-note core claims interpreted as hypotheses, not facts
- [ ] Read/annotate full build roadmap against actual repository
- [ ] Read/annotate full database schema against actual environment
- [ ] Read/annotate all data-source guidance against current providers
- [ ] Review all acceptance gates before live-money use
- [ ] Create formal requirements matrix

# D. Repository / Environment

- [ ] Obtain/locate actual runnable code skeleton
- [ ] Record repository commit/hash at official start
- [ ] Verify Python version
- [ ] Create virtual environment
- [ ] Install dependencies
- [ ] Run `pytest`
- [ ] Record all failing tests
- [ ] Run synthetic/demo flow
- [ ] Create `.env.example`
- [ ] Verify secrets are not committed
- [ ] Configure lint/type/test tooling
- [ ] Configure CI
- [ ] Document local run commands

# E. Existing Skeleton Audit

- [ ] Inspect standard agent contract
- [ ] Inspect firewall implementation
- [ ] Inspect contamination scanner
- [ ] Inspect lock/hash chain
- [ ] Inspect data-quality module
- [ ] Inspect tennis Elo
- [ ] Inspect tennis simulation
- [ ] Inspect devig logic
- [ ] Inspect CLV logic
- [ ] Inspect Brier/log-loss/calibration implementation
- [ ] Inspect pipeline/daily orchestration
- [ ] Inspect tests
- [ ] Produce "implemented / partial / missing" matrix

# F. PostgreSQL / Security Foundations

- [ ] Provision PostgreSQL 15+
- [ ] Apply schema
- [ ] Verify migrations
- [ ] Create Zone A/blind role
- [ ] Create market role
- [ ] Create decision/application role(s)
- [ ] Confirm Zone A has zero SELECT on `market.*`
- [ ] Test direct market query as Zone A and confirm failure
- [ ] Verify append-only prediction ledger
- [ ] Verify append-only market analysis
- [ ] Verify odds history immutability as designed
- [ ] Verify prediction lock DB trigger
- [ ] Verify market lock DB trigger
- [ ] Verify hash-chain integrity
- [ ] Verify `as_of` indexing/query semantics
- [ ] Add automated DB security tests

# G. Firewall

- [ ] Enumerate forbidden market keys
- [ ] Enumerate allowed Zone A payload fields
- [ ] Test nested odds contamination
- [ ] Test free-text odds contamination
- [ ] Test implied-probability contamination
- [ ] Test market-derived rating contamination
- [ ] Log firewall incidents
- [ ] Block web/odds tools for blind LLM agents
- [ ] Add scrubber for news/injury text where necessary
- [ ] Verify backtest and live use same payload builder

# H. Entity / ID Model

- [ ] Define canonical event IDs
- [ ] Define team/player IDs
- [ ] Define competition IDs
- [ ] Define bookmaker IDs
- [ ] Define venue IDs
- [ ] Define market IDs
- [ ] Create alias/entity resolution rules
- [ ] Handle renamed teams/players
- [ ] Handle postponed/rescheduled events
- [ ] Handle withdrawals/retirements
- [ ] Create entity-quality flags

# I. Historical Data Foundations

## Tennis
- [ ] Load historical ATP results
- [ ] Load historical Challenger results
- [ ] Load serve/return stats
- [ ] Load rankings only as allowed features
- [ ] Build surface mapping
- [ ] Build player identity resolution
- [ ] Separate any odds columns into Zone B

## Football
- [ ] Load historical 8-league results
- [ ] Load team/competition mappings
- [ ] Load xG where available
- [ ] Load lineups/injuries where available
- [ ] Load venues
- [ ] Separate odds columns into Zone B

## Market
- [ ] Select historical odds source
- [ ] Load opening odds
- [ ] Load closing odds
- [ ] Load timestamped movement if available
- [ ] Load book identifiers
- [ ] Build devig-ready snapshots
- [ ] Build market consensus snapshots

# J. Data-Source Due Diligence

- [ ] Create source registry
- [ ] Verify licensing for every source
- [ ] Verify commercial-use restrictions
- [ ] Verify current API pricing
- [ ] Verify rate limits
- [ ] Verify historical depth
- [ ] Verify lower-league football coverage
- [ ] Verify Challenger depth
- [ ] Verify timestamp resolution for odds
- [ ] Verify opening/closing definition
- [ ] Verify money/ticket data provenance if used
- [ ] Create fallback provider plan

# K. Minimum Honest Loop — Tennis

## Zone A
- [ ] T01 Player Strength / Elo
- [ ] T02 Surface Elo
- [ ] T03 Serve
- [ ] T04 Return
- [ ] T05 Hold/Break
- [ ] T19 Simulation
- [ ] T20 Fair-Odds Maker

## Lock
- [ ] Standard output validation
- [ ] Prediction payload canonicalization
- [ ] Prediction lock
- [ ] Hash chain
- [ ] Immutable storage

## Zone B
- [ ] M01 Odds Collection
- [ ] M02 Devig / Consensus
- [ ] Market report
- [ ] Market lock

## Zone C
- [ ] M06 Discrepancy
- [ ] M07 Value / EV
- [ ] Simple M10 Gatekeeper

## End-to-End
- [ ] CLI daily run
- [ ] Zero market fields visible to Zone A
- [ ] Prediction timestamp earlier than market reveal
- [ ] Decision output includes price ladder
- [ ] Funnel counts logged
- [ ] `NO QUALIFYING BETS TODAY` path tested

# L. Tennis Backtesting

- [ ] Create single backtest/live payload builder
- [ ] Train window configured
- [ ] Validation window configured
- [ ] Test window configured
- [ ] Walk-forward framework
- [ ] Brier score
- [ ] Log loss
- [ ] Calibration curve
- [ ] Calibration slope
- [ ] CLV proxy
- [ ] Baseline model comparison
- [ ] Agent ablation
- [ ] Learned meta-model v1
- [ ] Per-tour calibration
- [ ] Leakage checklist
- [ ] Reproducible experiment artifacts

# M. Extended Tennis

- [ ] T06 Recent Form
- [ ] T07 Opponent Quality
- [ ] T08 Fatigue
- [ ] T09 Physical Condition
- [ ] T10 Travel & Rest
- [ ] T11 H2H
- [ ] T12 Playing Style
- [ ] T13 Handedness
- [ ] T14 Court Condition
- [ ] T15 Weather
- [ ] T16 Pressure / Tiebreak
- [ ] T17 Challenger Specialist
- [ ] T18 ATP Main Tour Specialist
- [ ] Challenger DQ threshold
- [ ] Each agent has `INSUFFICIENT_DATA`
- [ ] Each agent has firewall test
- [ ] Each agent has backtest/ablation
- [ ] Each nonzero weight justified

# N. Full Market / Decision Layer

- [ ] M03 Line Movement
- [ ] M04 Bookmaker Leader / Follower
- [ ] M05 Market Flow
- [ ] M08 Adversarial Skeptic
- [ ] M09 Risk / Portfolio
- [ ] Full M10 Gatekeeper
- [ ] Versioned decision policies
- [ ] Conservative uncertainty discount
- [ ] Minimum acceptable price logic
- [ ] Exposure controls
- [ ] Risk flags
- [ ] Explanation output

# O. MLB 22-Pick Reverse Engineering

## Normalize
- [x] Capture all 22 listed plays
- [x] Classify ML / run line / totals
- [x] Record win/loss shown in screenshot
- [ ] Create structured CSV/database table
- [ ] Assign `bet_id`
- [ ] Assign reconstruction confidence

## Identify Events
- [ ] Find exact Mets–Phillies U9.5 event
- [ ] Find exact Royals +1.5 event
- [ ] Find exact Tigers -1.5 event
- [ ] Find exact Dodgers–Yankees U9 event
- [ ] Find exact Brewers -1.5 event
- [ ] Find exact Tigers ML event
- [ ] Find exact Marlins–Astros U8 event
- [ ] Find exact Royals–Tigers U8.5 event
- [ ] Find exact Giants ML event
- [ ] Find exact Dodgers ML event
- [ ] Continue until all 22 dates are resolved

## Reconstruct Sports Context
- [ ] Starting pitchers
- [ ] Projected/actual lineups
- [ ] Injuries
- [ ] Bullpen availability
- [ ] Recent workload
- [ ] Team offense
- [ ] Platoon splits
- [ ] Park factors
- [ ] Weather
- [ ] Schedule/rest
- [ ] Any major pre-game information event

## Reconstruct Market Context
- [ ] Sportsbook
- [ ] Opening line/odds
- [ ] Pick-time line/odds
- [ ] Pick timestamp
- [ ] Post-pick movement
- [ ] Closing line/odds
- [ ] Consensus
- [ ] Sharp/recreational book divergence if defensible
- [ ] Public ticket share if available
- [ ] Public money share if available

## Analyze
- [ ] Entry-vs-close CLV
- [ ] Price-band distribution
- [ ] Favorite/underdog pattern
- [ ] ML-vs-run-line choice
- [ ] Totals pattern
- [ ] Repeated-team explanation
- [ ] Market-movement confirmation
- [ ] Compare winners AND losses
- [ ] Compare against matched control games
- [ ] Form hypotheses
- [ ] Lock hypothesis v0.1
- [ ] Test on broader unseen MLB cohort

# P. Voice-Note Hypothesis Research

- [x] Record Patriots +4.5 vs asserted +10.5 discrepancy claim
- [x] Record claimed 86% money/no-move interpretation
- [x] Record -150 vs asserted -250 discrepancy example
- [x] Record claimed early-price / later-line-movement behavior
- [x] Tag performance/stake/profit claims as unverified
- [ ] Identify exact Patriots/Seahawks game referenced
- [ ] Verify historical spread path
- [ ] Verify result
- [ ] Determine whether public-money data is independently available
- [ ] Test line-resistance signal historically
- [ ] Test reverse-line-movement signal historically
- [ ] Test cross-book disagreement
- [ ] Test stale-price capture
- [ ] Test entry-vs-close movement
- [ ] Control for injury/lineup news
- [ ] Control for market liquidity/time-to-game
- [ ] Reject or retain each signal based on unseen data

# Q. Football Core

- [ ] ENG1 data spine
- [ ] ENG2 data spine
- [ ] F01 Team Strength
- [ ] F02 xG
- [ ] F16 Form Quality
- [ ] F18 Scoreline Model
- [ ] F19 Simulation
- [ ] F20 Fair Odds
- [ ] Core football backtest
- [ ] Add situational agents only after core result
- [ ] ENG3/ENG4 later
- [ ] Scotland later
- [ ] Per-league thresholds
- [ ] Lineup-triggered re-runs

# R. Dashboard / API

- [ ] Do not start polish before core research loop
- [ ] Event/run API
- [ ] Prediction/lock read API
- [ ] Market read API
- [ ] Decision API
- [ ] Evaluation API
- [ ] Monitoring API
- [ ] Admin API
- [ ] Decision-card UI
- [ ] Funnel UI
- [ ] Lock status
- [ ] Fair vs market price
- [ ] Edge/EV
- [ ] Data quality
- [ ] Uncertainty
- [ ] Risk flags
- [ ] Agent evidence
- [ ] Historical result/CLV
- [ ] `NO QUALIFYING BETS TODAY` UI

# S. Paper Trading / Evaluation Hardening

- [ ] Start paper trading once minimum loop is stable
- [ ] Do not stop paper-trading history
- [ ] Record every candidate
- [ ] Record every pass/watch
- [ ] Record available price at decision
- [ ] Record best available price
- [ ] Record close
- [ ] Record settlement
- [ ] Compute CLV
- [ ] Compute rolling calibration
- [ ] Compute rolling Brier
- [ ] Compute rolling log loss
- [ ] Agent performance nightly
- [ ] Monthly agent report
- [ ] Funnel anomaly alerts
- [ ] Provider outage drills
- [ ] Hash-chain audit job
- [ ] Required live-sample acceptance gates

# T. Model Governance

- [ ] Version feature sets
- [ ] Version agents
- [ ] Version calibration
- [ ] Version meta-model
- [ ] Version decision policy
- [ ] Version strategy hypotheses
- [ ] No in-place historical edits
- [ ] Store code revision with run
- [ ] Store data snapshot/version where possible
- [ ] Experiment registry
- [ ] Decision log
- [ ] Data drift log

# U. Documentation

- [x] Master README created
- [x] Master progress checklist created
- [ ] Architecture diagram
- [ ] Data dictionary
- [ ] Entity ID conventions
- [ ] Agent contract docs
- [ ] Data-source registry
- [ ] Backtest methodology
- [ ] Leakage checklist
- [ ] Deployment runbook
- [ ] Incident runbook
- [ ] Client requirement log
- [ ] Scope-change log
- [ ] Weekly progress report template

# V. Current First-Day Priority Order

1. [ ] Obtain and run actual developer-pack code
2. [ ] Run tests and demo
3. [ ] Map implemented vs missing components
4. [ ] Configure PostgreSQL/security roles
5. [ ] Prove firewall and lock behavior
6. [ ] Create structured 22-pick reconstruction dataset
7. [ ] Begin exact-date identification for MLB events
8. [ ] Formalize voice-note hypotheses
9. [ ] Create data-source registry
10. [ ] Decide first real API/provider connections
11. [ ] Start minimum honest loop integration
12. [ ] Update this checklist with evidence, not estimates

# W. Definition of "Complete"

The project is NOT complete because:

- all agents have code;
- a dashboard exists;
- a short winning run appears;
- a backtest looks profitable.

A production-research milestone is complete only when:

- [ ] point-in-time data is correct
- [ ] firewall tests pass
- [ ] locks are immutable
- [ ] strategy/model versions are reproducible
- [ ] required data quality is met
- [ ] calibration is acceptable
- [ ] log loss/Brier are evaluated
- [ ] CLV evidence is documented
- [ ] unseen-data evaluation is complete
- [ ] paper-trading evidence is sufficient
- [ ] agent marginal value is measured
- [ ] operational monitoring works
- [ ] scope/risks are documented


---

# 17. FINAL INSTRUCTION TO CODEX

This is an evolving project.

Do not optimize for looking busy, agent count, or attractive backtests.

Optimize for:

- correct information boundaries;
- reproducibility;
- honest historical reconstruction;
- auditable decisions;
- point-in-time correctness;
- reliable data;
- calibrated probabilities;
- measurable market discrepancies;
- robust out-of-sample evidence;
- maintainable code.

When the client requests something new:

1. identify whether it is already in scope;
2. identify whether it changes the formal V1;
3. identify required data;
4. identify architecture/security impact;
5. identify validation requirements;
6. record the change;
7. implement only after understanding those effects.

The engineering north star remains:

> Build the smallest auditable system capable of independently pricing an event, separately understanding the market, identifying a statistically defensible discrepancy, and proving that the discrepancy is repeatable without leakage or hindsight.

If a result cannot be established from data, say so.

If a hypothesis fails, reject it.

If an agent adds no marginal value, do not keep it merely because the original blueprint listed it.

If data is inadequate, abstain.

If a model is changed, version it.

If a prediction is locked, never rewrite it.

If a shortcut compromises the firewall, do not take the shortcut.
