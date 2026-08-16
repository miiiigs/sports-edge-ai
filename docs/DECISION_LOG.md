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

## DEC-010

- `date`: 2026-08-15
- `context`: the client supplied three newer PDFs plus matching Markdown files, and the PDF text is not identical to the Markdown versions
- `decision`: treat the August 15, 2026 PDFs as the primary specification authority after direct user instructions, use the companion Markdown files only where they do not conflict, and record any conflict explicitly
- `reason`: silently blending the PDF and Markdown materials would preserve superseded assumptions and create hidden specification drift
- `alternatives`: treat PDFs and Markdown as interchangeable, or continue using the older handoff stack as the main authority
- `consequences`: current football `1X2` work becomes a benchmark/research lane unless it is re-authorized by the new PDFs; missing `00-developer-brief.md` becomes an explicit blocker
- `status`: active

## DEC-011

- `date`: 2026-08-15
- `context`: the first football GAP migration slice needs usable team identity and state persistence boundaries before multi-provider expansion or repo splitting
- `decision`: allow the first GAP research engine to use deterministic single-source football-data team identifiers inside league-specific state, while explicitly treating that identifier as temporary and preserving a backend-agnostic state contract
- `reason`: this unlocks a correct first GAP engine for the currently loaded archive without pretending that provider display names are the final canonical identity layer
- `alternatives`: block all GAP work until a full cross-provider alias layer exists, or silently bake provider display names into long-term architecture
- `consequences`: first-pass GAP state is keyed by league, statistic, team identifier, and venue with explicit as-of/config metadata; canonical multi-provider team identity remains a later required migration step
- `status`: active

## DEC-012

- `date`: 2026-08-15
- `context`: the first chronological shots GAP experiment needed a temporally valid initialization rule, explicit season-transition behavior, and duplicate-event protection before any downstream modeling
- `decision`: initialize each competition/statistic from the earliest readiness-approved season only, process that same season as warm-up, carry state across later seasons within the same competition without cross-league mixing, and reject duplicate observations inside `GapRatingsEngine`
- `reason`: this keeps initialization leakage out of evaluated seasons, preserves the current-division-history semantics needed later for promoted/relegated-team rules, and prevents accidental state corruption during replay or resume
- `alternatives`: initialize from full evaluation-season averages, reset automatically at every season boundary, or rely on the runner alone to prevent duplicate processing
- `consequences`: evaluated predictions start only after warm-up state exists; new entrants to a competition begin from the competition baseline with zero current-competition observations; idempotency is enforced in the in-memory research engine as well as the experiment layer
- `status`: active
