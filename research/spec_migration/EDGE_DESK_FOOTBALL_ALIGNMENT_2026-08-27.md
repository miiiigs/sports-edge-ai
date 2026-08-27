# Football Edge Desk Alignment

Date: `2026-08-27`

## Purpose

This document records the alignment between the original Football implementation in this repository and the newer Football Edge Desk architecture direction after Route A direct classification was implemented and validated.

It preserves requirement history instead of rewriting earlier specs.

## Original Football Architecture

The original Football production path in this repository still operates around the established Route B composite stack:

- GAP baseline state
- Day 8 expected-goals conversion
- Day 9 Dixon-Coles scoreline distribution
- Day 10 fair-price derivation

Manager logic is not an agent.

Day 12 safeguards are not agents.

Legacy Poisson remains historical research only and is not promoted into Agent 3.

## Edge Desk Predictive Agents

The repository now has two legitimate predictive Football agents in the Edge Desk shadow architecture:

- `ROUTE_A_DIRECT_CLASSIFIER`
- `ROUTE_B_COMPOSITE`

Route A is the original direct-classification lane reconstructed from repository evidence:

- model family: logistic regression
- inputs: Day 7 GAP-predicted shots and corners, their sums/differences, and competition identity
- targets: `BTTS` and `Over 2.5`
- calibration policy: evaluate held-out Platt and isotonic, but allow `none` when held-out evidence does not improve the market

Agent 3 is still not implemented and still requires a genuinely new independent model design.

## Route A Validation State

Route A is active only because the validation gate passed on both intended markets.

Accepted out-of-sample results from the committed artifact:

- `BTTS`
  - naive log loss: `0.692930`
  - Route A log loss: `0.691299`
  - naive Brier: `0.249892`
  - Route A Brier: `0.249067`
  - selected calibration: `none`
- `Over 2.5`
  - naive log loss: `0.692669`
  - Route A log loss: `0.687043`
  - naive Brier: `0.249756`
  - Route A Brier: `0.246980`
  - selected calibration: `none`

The acceptance decision remains evidence-based:

- Route A would have stayed inactive if it failed to beat the point-in-time-safe league baseline credibly.
- held-out calibration was tested and documented, not assumed.

## Immutable Ledger Architecture

The Edge Desk ledger remains the permanent append-only store for:

- agent registry
- agent market capability
- agent predictions
- consensus configurations
- consensus evaluations
- decision history

The SQLite immutability and idempotency protections from Phase 1A remain in force.

Route A does not create a parallel history store.

Its first real runtime predictions for supported markets are written directly into the immutable ledger.

## Market Participation Truth

Edge Desk participation is now market-aware by both agent and line context:

- `ROUTE_A_DIRECT_CLASSIFIER`
  - `BTTS`: supported
  - `goals_total 2.5`: supported
  - `1x2`: not supported
  - `asian_handicap`: not supported
  - `corner_total`: not supported
- `ROUTE_B_COMPOSITE`
  - continues to support the currently implemented Route B market families

This means:

- `BTTS` can evaluate two-agent Route A plus Route B consensus
- `goals_total 2.5` can evaluate two-agent Route A plus Route B consensus
- `1x2`, other totals such as `3.5`, corners, and Asian handicap remain one-agent Route B contexts and must stay insufficient

## Consensus Alignment

Phase 2A adds a named Route A plus Route B consensus profile for the overlapping markets only:

- config id: `football_route_a_b_consensus`
- version: `FOOTBALL_ROUTE_A_B_V1`
- rule: same direction required
- spread gate: `<= 0.06`

Consensus is evaluated per market context, not per stored selection row.

That correction matters because both agents persist both sides of binary markets; the consensus layer now collapses those rows into one market vote per agent before applying direction and spread checks.

The global future-oriented consensus configuration remains intact for markets without Route A participation.

## Decision Lifecycle

Even when Route A plus Route B consensus passes, the decision history still records:

- `CONSENSUS_PASSED`
- reason code `VALUE_RISK_PENDING`

It does not claim `QUALIFIED`.

Value integration and risk integration remain later phases.

## Route B Preservation

Route B remains the authoritative current Football application path.

Phase 2A does not:

- change GAP math
- change Day 8 xG math
- change Day 9 Dixon-Coles math
- change Day 10 fair-price math
- change current picks logic
- redesign the current dashboard

Route A is additive.

## No Historical Fabrication

Phase 2A still does not fabricate historical Route A backfill, Agent 3 history, or synthetic three-agent past consensus.

Only real current runtime Route A outputs are added to the permanent ledger.

## Summary

The Football Edge Desk shadow architecture now has two genuine predictive agents with truthful market-scoped consensus on the overlapping markets.

Known remaining gaps after Phase 2A:

- Agent 3 is still not implemented
- true three-agent consensus is still unavailable
- value integration is still pending
- risk integration is still pending
- Edge Desk dashboard presentation is still pending
- historical backfill was not performed
