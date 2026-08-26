# Football Edge Desk Alignment

Date: `2026-08-26`

## Purpose

This document records the alignment between the original Football implementation in this repository and the newer Football Edge Desk architecture direction.

It preserves the requirement history rather than rewriting older material.

## Original Football Architecture

The original Football production path in this repository currently operates around one primary live predictor:

- Route B composite
- GAP baseline state
- Day 8 expected-goals conversion
- Day 9 Dixon-Coles scoreline distribution
- Day 10 fair-price derivation

The current application then layers current-market evaluation, manager logic, safeguards, candidate handling, paper-trading, and dashboard presentation on top of that single prediction payload.

Manager logic is not an agent.

Day 12 safeguards are not agents.

xG conversion is not an independent agent.

Legacy Poisson remains historical and research-oriented work. It is not silently promoted to a new live Football agent.

## Newer Edge Desk Architecture

The newer Football Edge Desk direction expands the architecture toward:

- full slate processing
- permanent individual agent outputs
- configurable consensus
- later value and risk evaluation
- durable qualified-pick and no-bet history
- eventual settlement and review by market context

This is an architecture expansion, not a retroactive claim that three Football agents always existed in production.

## Owner Clarification

The current repository has one legitimate implemented predictive Football agent today:

- `ROUTE_B_COMPOSITE`

Route A remains part of the intended Football direction but is not yet implemented as a live predictor.

Agent 3 remains unresolved and requires a genuinely new model design.

Because only Route B is implemented, the Edge Desk shadow layer must not pretend three-agent consensus already exists.

## Current Route B Status

Route B remains the authoritative current Football prediction path for the application.

Phase 1A does not:

- rewrite Route B
- change Route B probabilities
- change Route B fair prices
- change current qualification behavior
- change current paper-entry eligibility
- redesign the dashboard

## Immutable Ledger Architecture

Phase 1A introduces an immutable Edge Desk shadow ledger beside the existing mutable dashboard prediction store.

The ledger persists:

- agent registry
- agent market capability
- agent predictions
- consensus configurations
- consensus evaluations
- decision history

The shadow ledger uses SQLite-compatible append-only tables with database triggers that reject `UPDATE` and `DELETE` against immutable records.

Retries of the same logical observation are idempotent through stable logical keys and insert-ignore semantics.

Changed model version, changed `as_of`, changed market line, changed selection context, or changed payload truth produce new immutable rows instead of mutation.

## Shadow-Mode Status

The current application remains authoritative.

The new Edge Desk ledger records parallel truth in shadow mode:

- current Football application behavior continues unchanged
- Edge Desk shadow records Route B agent output per market
- Edge Desk shadow records blocked states as durable data
- Edge Desk shadow records consensus truthfully as insufficient when quorum is not available

With only Route B available, Edge Desk consensus cannot pass the multi-agent requirement.

That temporary split is intentional.

## No Historical Fabrication

Phase 1A does not fabricate historical Route A rows, Agent 3 rows, or synthetic multi-agent consensus history.

It also does not rewrite existing stored Football prediction history into fake multi-agent past truth.

Only clear Route B shadow recording is introduced for the new architecture foundation.

## Market Participation Truth

The Edge Desk design must remain market-aware.

Agents do not automatically support every market family.

Phase 1A records Route B as supporting the currently implemented market families used by the repository:

- `1x2`
- `btts`
- `goals_total`
- `asian_handicap`
- `corner_total`

Unsupported capability must not be treated as a positive vote.

## Dashboard Note

The main Edge Desk presentation layer is not implemented in Phase 1A.

The eventual presentation reference is `Edge-Desk-Design-Pack.html`.

If that file is not present in this repository, it remains an external reference by name only.

## Summary

Phase 1A creates truth before presentation.

The repository keeps the current Football application behavior intact while adding the permanent historical foundation needed for future Route A work, Agent 3 design, multi-agent consensus, and later Edge Desk dashboard implementation.
