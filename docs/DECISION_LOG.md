# Decision Log

## DEC-001

- `date`: 2026-08-13
- `context`: repository contains handoff materials and reference artifacts, but no runnable source tree
- `decision`: treat `SPORTS_EDGE_AI_CODEX_MASTER_HANDOFF.md` as the primary operating document
- `reason`: it is the newest consolidated authority present in the repo
- `alternatives`: rely on README only, or start coding from the PDF alone
- `consequences`: all changes must preserve the handoff's firewall, lock, and point-in-time rules
- `status`: active

## DEC-002

- `date`: 2026-08-13
- `context`: Pack skeleton is absent as files in the repository, but Appendix C appears substantive
- `decision`: classify Pack code as recoverable before creating a replacement skeleton
- `reason`: reduces accidental divergence from the Pack architecture
- `alternatives`: start a brand-new scaffold immediately
- `consequences`: next engineering work should prefer reconstruction over redesign
- `status`: active

## DEC-003

- `date`: 2026-08-13
- `context`: client direction has shifted toward strategy reverse engineering and evidence-driven agent selection
- `decision`: prioritize research artifacts in parallel with minimal architecture assessment, not bulk agent implementation
- `reason`: building many agents before validating hypotheses would misalign with current project goals
- `alternatives`: implement the Pack sequentially by agent count
- `consequences`: research docs and data requirements become first-class project assets
- `status`: active

## DEC-004

- `date`: 2026-08-13
- `context`: meeting note confirms MLB first, football immediate parallel, and a smaller specialist-plus-decision prototype
- `decision`: use a research-first prototype path centered on MLB reconstruction, football reconnaissance, odds comparison, and a small decision loop
- `reason`: this better matches the client's explicitly stated near-term priorities
- `alternatives`: continue with a generic Phase 0-first scaffold plan or a 50-agent-first build
- `consequences`: Pack reconstruction remains important, but it supports the research loop instead of replacing it
- `status`: active

## DEC-005

- `date`: 2026-08-13
- `context`: `meeting_2` gives concrete football-source direction and stronger CLV/market-pricing guidance
- `decision`: use `football-data.co.uk` as the first historical football research source, evaluate `API-Football` for live/current-season intake, and treat `football-data.org` as a secondary option
- `reason`: this is the most practical low-cost immediate path supported by current evidence
- `alternatives`: delay football work until later, or start with a paid-only provider without validating the free historical base
- `consequences`: football work can start immediately, but lower-league field quality still needs verification
- `status`: active

## DEC-006

- `date`: 2026-08-13
- `context`: the new meeting note strongly reinforces CLV and market-gap thinking
- `decision`: define the paper/shadow ledger early and make CLV one of the first evaluation targets
- `reason`: trial output history without price capture would weaken the research process
- `alternatives`: wait until later to define decision logging
- `consequences`: audit/history design now becomes part of the immediate foundation work
- `status`: active

## DEC-007

- `date`: 2026-08-13
- `context`: `meeting_3` reinforces a quant-style Python/PostgreSQL stack and suggests more advanced infrastructure for scale
- `decision`: keep Python and PostgreSQL as the current core stack, while deferring `TimescaleDB`, `ClickHouse`, `Redis`, and compiled low-latency layers until scale or use-case evidence demands them
- `reason`: this preserves the right long-term direction without overbuilding the current phase
- `alternatives`: adopt a heavier market-data stack immediately, or ignore the scaling guidance entirely
- `consequences`: the current repo stays lean, but future time-series and live-state expansion remains explicitly compatible with the direction suggested in `meeting_3`
- `status`: active

## DEC-008

- `date`: 2026-08-13
- `context`: `meeting_3` proposes a narrow low-cost test by tracking lower-league corner/card prices against a sharp reference book before deeper automation
- `decision`: absorb this as a research experiment lane, not as a replacement for the core architecture or as an automatic production-scope change
- `reason`: it is a good business-validation idea, but it should remain controlled, timestamped, and separate from formal product-scope assumptions
- `alternatives`: ignore the experiment completely, or let it silently redefine the product scope around corners/cards
- `consequences`: the project can validate whether off-market gaps are frequent and actionable without weakening the firewall, audit trail, or evidence standards
- `status`: active

## DEC-009

- `date`: 2026-08-14
- `context`: the repository is now runnable, football history has expanded into EFL plus La Liga, and the immediate requirement is the first honest football trial loop for Saturday, August 15, 2026
- `decision`: prioritize the football paper/shadow trial pipeline over broader planning, and classify upcoming live outputs as `PAPER_SHADOW_CANDIDATE` while classifying the historical close-vs-close lane as `RESEARCH_SIGNAL`
- `reason`: this keeps live trial output separate from historical market-discrepancy research and aligns the system more closely with the client direction and the Pack lock/firewall rules
- `alternatives`: keep labeling all football outputs as generic `BET_CANDIDATE`, or continue broad architectural planning without tightening the live trial loop
- `consequences`: the repo now emphasizes prediction lock, market lock, market snapshots, conservative trial gating, and up-to-two paper/shadow outputs rather than forced picks
- `status`: active
