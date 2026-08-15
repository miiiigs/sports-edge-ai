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
- [x] Create written client requirements log
- [x] Create scope-change log

# B. Scope Clarification

- [?] MLB research-only vs production vertical
- [?] Tennis-first formal MVP vs another sport first
- [?] Are corners/cards formally V1 or post-V1?
- [?] Which sportsbook(s) are first-class targets?
- [?] Is exchange/Betfair data available/desired?
- [?] Is public money/ticket data available?
- [?] Which jurisdiction/operator constraints matter?
- [?] First user-facing deliverable: CLI, report, API, or dashboard?
- [x] Football immediate parallel research confirmed

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
- [x] Read/annotate meeting_2 football-source and pricing guidance
- [x] Read/annotate meeting_3 narrow-validation and CLV-first guidance
- [ ] Review all acceptance gates before live-money use
- [x] Create formal requirements matrix

# D. Repository / Environment

- [x] Obtain/locate actual runnable code skeleton
- [x] Assess Appendix C recoverability of Pack code skeleton
- [ ] Record repository commit/hash at official start
- [x] Verify Python version
- [ ] Create virtual environment
- [x] Install dependencies
- [x] Run `pytest`
- [x] Record all failing tests
- [x] Run synthetic/demo flow
- [ ] Create `.env.example`
- [ ] Verify secrets are not committed
- [ ] Configure lint/type/test tooling
- [ ] Configure CI
- [x] Document local run commands

# E. Existing Skeleton Audit

- [x] Inspect standard agent contract
- [x] Inspect firewall implementation
- [x] Inspect contamination scanner
- [x] Inspect lock/hash chain
- [x] Inspect data-quality module
- [x] Inspect tennis Elo
- [x] Inspect tennis simulation
- [x] Inspect devig logic
- [x] Inspect CLV logic
- [x] Inspect Brier/log-loss/calibration implementation
- [x] Inspect pipeline/daily orchestration
- [x] Inspect tests
- [x] Produce "implemented / partial / missing" matrix

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
- [~] Load historical 8-league results
- [ ] Load team/competition mappings
- [ ] Load xG where available
- [ ] Load lineups/injuries where available
- [ ] Load venues
- [~] Separate odds columns into Zone B
- [x] Identify first historical football source candidate
- [x] Identify first live/current football API candidate
- [x] Validate initial `football-data.co.uk` historical field coverage for `E0/E1/E2/E3/SC0`
- [x] Validate `football-data.co.uk` La Liga coverage for `SP1`
- [~] Validate current-season football CSV reliability league by league
- [x] Export first reusable football historical artifact for `ENG1 2526`
- [x] Create first local football research store and ingest `ENG1 2526`
- [x] Ingest `ESP1 2526` into the local football research store

## Market
- [ ] Select historical odds source
- [ ] Load opening odds
- [ ] Load closing odds
- [ ] Load timestamped movement if available
- [ ] Load book identifiers
- [ ] Build devig-ready snapshots
- [ ] Build market consensus snapshots

# J. Data-Source Due Diligence

- [x] Create source registry
- [ ] Verify licensing for every source
- [ ] Verify commercial-use restrictions
- [x] Verify current API pricing
- [ ] Verify rate limits
- [ ] Verify historical depth
- [~] Verify lower-league football coverage
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
- [x] Create structured CSV/database table
- [x] Assign `bet_id`
- [x] Assign reconstruction confidence

## Identify Events
- [~] Find exact Mets–Phillies U9.5 event
- [ ] Find exact Royals +1.5 event
- [ ] Find exact Tigers -1.5 event
- [~] Find exact Dodgers–Yankees U9 event
- [ ] Find exact Brewers -1.5 event
- [ ] Find exact Tigers ML event
- [~] Find exact Marlins–Astros U8 event
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

- [x] Create football research workspace
- [x] ENG1 data spine
- [x] ESP1 data spine
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

# S1. Narrow Market Validation

- [~] Define first narrow football market experiment
- [x] Decide first sharp-reference book
- [ ] Decide first retail comparison set
- [x] Define experiment logging fields
- [~] Define entry-to-close grading rules
- [ ] Start first manual/structured validation run

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
- [x] Data-source registry
- [ ] Backtest methodology
- [ ] Leakage checklist
- [ ] Deployment runbook
- [ ] Incident runbook
- [x] Client requirement log
- [x] Scope-change log
- [ ] Weekly progress report template

# V. Current First-Day Priority Order

1. [x] Obtain and run actual developer-pack code
2. [x] Run tests and demo
3. [x] Map implemented vs missing components
4. [ ] Configure PostgreSQL/security roles
5. [ ] Prove firewall and lock behavior
6. [x] Create structured 22-pick reconstruction dataset
7. [ ] Begin exact-date identification for MLB events
8. [x] Formalize voice-note hypotheses
9. [x] Create data-source registry
10. [~] Decide first real API/provider connections
11. [~] Start football historical intake foundation
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
