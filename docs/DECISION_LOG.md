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
- `consequences`: current football `1X2` work becomes a benchmark/research lane unless it is re-authorized by the new PDFs; the brief authority must be verified from the supplied files instead of guessed from the missing Markdown mirror
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

## DEC-013

- `date`: 2026-08-17
- `context`: the audit and the Day 7 recovery plan require `shots_on_target` next, but the repo already contains a tested chronological shots shell with the key invariants in place
- `decision`: reuse and generalize the existing chronological shots GAP walk-forward shell for `shots_on_target` rather than creating a second independent implementation, and fail loudly if a readiness-approved SOT row still contains a missing required statistic value
- `reason`: this preserves the already-validated chronology, warm-up, isolation, and duplicate-protection mechanics while tightening the data contract for subsequent statistic lanes
- `alternatives`: duplicate the shots runner into a separate implementation, or silently skip missing approved-input rows
- `consequences`: the repo now has one statistic-parameterized Day 7 replay shell plus a thin SOT wrapper; the next bounded football task is `corners`, not a redesign of the runner architecture
- `status`: active

## DEC-014

- `date`: 2026-08-17
- `context`: the SOT slice succeeded on the real archive and the next Day 7 requirement is the final independent GAP statistic lane before `lambda` fitting
- `decision`: implement `corners` as another thin wrapper over the same validated chronology shell and stop again after the real replay rather than rolling directly into Day 8
- `reason`: this keeps the three Day 7 statistics comparable and preserves a clean boundary between mechanical replay validation and later downstream goal-conversion work
- `alternatives`: build a corners-specific replay path, or start `lambda` fitting and Day 8 work in the same iteration
- `consequences`: Day 7 now has three completed statistic lanes under one shell, and the next bounded football task is walk-forward `lambda` fitting plus an explicit audit of persisted `as_of` coverage
- `status`: active

## DEC-015

- `date`: 2026-08-17
- `context`: the final Day 7 closure pass required an explicit persisted `as_of` contract and a fixed, auditable walk-forward lambda-fitting method before any Day 8 work could be authorized
- `decision`: define historical pre-match prediction-row `as_of` as fixture kickoff when source `kickoff_time` exists or `match_dateT00:00:00` otherwise, and fit lambda on the fixed grid `0.02 / 0.05 / 0.10` with the latest approved season in each competition reserved as a post-selection holdout
- `reason`: this satisfies the PDF requirement that persisted outputs explicitly carry `as_of`, avoids inventing unsupported source timestamps, keeps the fitting search small and predeclared, and prevents repeated exposure of the designated final holdout during selection
- `alternatives`: leave `as_of` implicit in `match_date` and `kickoff_time`, fit lambda on a larger adaptive sweep, or allow the final season into the repeated selection loop
- `consequences`: all three GAP lanes now persist explicit `as_of` and `model_version`; Day 7 fitting artifacts log every tested variant, every selected lambda, and the reserved-holdout discipline; Day 8 is authorized only because the Day 7 acceptance audit now passes
- `status`: active

## DEC-016

- `date`: 2026-08-17
- `context`: the loaded repository snapshot includes `initial_data/00-developer-brief.pdf`, and the bounded next task after Day 7 is the downstream Day 8 conversion/distribution layer for the currently loaded football scope
- `decision`: treat the PDF brief as the authoritative source for `R1-R10`, close the stale missing-Markdown blocker as superseded, and implement Day 8 only as a downstream layer that converts fitted GAP `shots` and `shots_on_target` into expected goals while comparing Poisson versus negative-binomial corner distributions without retuning GAP `lambda`
- `reason`: the PDF is the actual authority, so the missing Markdown mirror is not a live blocker; limiting Day 8 to a downstream conversion layer preserves the frozen Day 7 baseline, protects chronology, and avoids silently widening into odds, staking, or manager work
- `alternatives`: continue treating the missing Markdown mirror as a blocker, retune the GAP layer during Day 8, or jump directly into scoreline and market derivation work
- `consequences`: Day 8 artifacts now carry explicit fixture identity, competition, kickoff, `as_of`, GAP model version, conversion model version, predicted stats, expected goals, and recorded corner-distribution evidence; Day 9 is authorized only through the downstream Dixon-Coles scoreline path
- `status`: active

## DEC-017

- `date`: 2026-08-17
- `context`: Day 8 already produces pre-match `expected_goals_home` and `expected_goals_away`, while the Day 9 PDF text still describes Dixon-Coles in terms of attack/defence parameters plus a home-advantage gamma term
- `decision`: treat Day 8 expected goals as the canonical Day 9 Poisson intensities, fit only the downstream Dixon-Coles low-score dependence layer around them, and omit an explicit Day 9 gamma term to avoid double-counting the competition-and-venue asymmetry already encoded in Day 8
- `reason`: this is the least contradictory interpretation of the current PDF-first stack because it preserves the closed Day 8 goals layer instead of silently rebuilding a second competing expected-goals model
- `alternatives`: re-estimate a second goals model through explicit alpha/beta/gamma strengths, or inject an extra gamma shift on top of Day 8 intensities
- `consequences`: Day 9 artifacts now expose an explicit interface policy, persist the final scoreline matrix as the single goals-based probability object for Day 10, and record that time decay was checked on the fixed `xi` grid but selected `xi=0.0` for every loaded competition
- `status`: active

## DEC-018

- `date`: 2026-08-18
- `context`: Day 9 historical rows contain useful evaluation fields such as realized goals, while Day 10 must derive fair prices from a frozen pre-match model object only
- `decision`: build Day 10 from a strict sanitized pre-match pricing contract that contains only fixture identity, kickoff, `as_of`, model versions, the persisted Day 9 scoreline matrix and truncation metadata, and the Day 8 corner-distribution object with predicted home/away corners, selected family, and dispersion
- `reason`: this preserves the blind-model boundary and prevents realized outcomes or bookmaker-era fields from leaking into deterministic market pricing
- `alternatives`: pass full Day 9 historical rows straight into market derivation, or rebuild corner inputs ad hoc from other intermediate data
- `consequences`: Day 10 now has deterministic scoreline-object and corner-object ids, sanitized pricing inputs, explicit source-object lineage on every fair-price row, and an invariant that changing realized outcomes cannot change any Day 10 price
- `status`: active

## DEC-019

- `date`: 2026-08-18
- `context`: Day 10 introduces integer and quarter-line goals totals, Asian handicap, and corner totals, which include push, half-win, and half-loss states that are not binary events
- `decision`: price Day 10 settlement-aware markets by zero expected value using `fair_decimal_price = 1 + L/W`, where `W = P(full_win) + 0.5*P(half_win)` and `L = P(full_loss) + 0.5*P(half_loss)`, and preserve the full settlement-probability breakdown on every market row
- `reason`: naive reciprocal win-probability pricing is only correct for no-push binary markets and would silently misprice integer and quarter-line products
- `alternatives`: use `1 / P(win)` everywhere, or encode separate hand-written quarter-line formulas per market family
- `consequences`: Day 10 now uses one generic settlement engine across goals totals, corner totals, and Asian handicap; strong AH-versus-1X2 invariants are enforceable; Day 11 must define how any `model_probability` or edge field represents push and quarter-line markets before thresholds are applied
- `status`: active

## DEC-020

- `date`: 2026-08-18
- `context`: the loaded Day 8 and Day 9 historical artifacts are valid for walk-forward evaluation, but their final holdout-season parameters are not automatically the same thing as a current forward-production snapshot for 2026-27 paper output
- `decision`: record forward-production-fit readiness as a separate dependency from Day 10 completion and do not treat final historical holdout parameters as current live parameters by default
- `reason`: conflating historical evaluation state with current production state would create a hidden time-travel violation once the system moves from historical replay to present-tense paper prediction
- `alternatives`: reuse the latest holdout parameter values as if they were production-current, or defer the distinction until after Day 11
- `consequences`: Day 10 can close honestly for historical fair-price derivation while the project still tracks a separate unresolved task to produce current as-of-valid Day 8 conversion coefficients, Day 8 corner distribution parameters, and Day 9 rho for live paper output
- `status`: active
