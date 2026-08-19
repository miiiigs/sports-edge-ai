# Decision Log

## DEC-038

- `date`: 2026-08-19
- `context`: the next dashboard build was explicitly redirected to a real football-data.org-based
  operational watchlist refresh rather than more handoff or documentation work
- `decision`: make `football-data.org` the default dashboard fixture provider, keep the accepted
  Day 7 through Day 12 football math stack unchanged, and add the missing `Status` product surface
- `reason`: this matches the new bounded dashboard milestone and removes dependence on the older
  API-Football dashboard path for the active UI
- `alternatives`: keep API-Football as the default dashboard provider, or do a UI-only pass without
  changing the runtime source
- `consequences`: the dashboard now honestly reports `NOT_CONFIGURED` until
  `SPORTS_AI_FOOTBALL_DATA_TOKEN` is set, and the active next-step dependency becomes token-backed
  current-fixture validation rather than more provider experimentation
- `status`: active

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

## DEC-021

- `date`: 2026-08-18
- `context`: the Day 10 fair-pricing work is technically correct, but useful current outputs were still buried inside large JSONL artifacts rather than presented as directly consumable manager-facing deliverables
- `decision`: make product-output packaging a formal completion requirement for every milestone, requiring machine-readable artifacts, reproducibility manifests, a human-readable product/output report, and a compact `current/latest` artifact
- `reason`: milestone completion should now represent both reproducible software state and immediately reviewable product state
- `alternatives`: continue treating technical experiment artifacts as sufficient on their own, or defer presentation artifacts until much later in the build
- `consequences`: Day 10 now includes a product-output generator and manager-facing football report under `research/product_outputs/`; all future Day 11A, Day 12, tennis, and combined milestones must ship both technical and presentation outputs
- `status`: active

## DEC-022

- `date`: 2026-08-18
- `context`: the project had started to accumulate valid completed football slices, but the next-task order still risked bouncing between football, tennis, and broader architecture work
- `decision`: lock the post-Day-10 build order to football Day 11A Manager Core, then football Day 12 safeguards/veto foundation, then a football logical-pipeline freeze before beginning tennis Days 13 through 19 and shared Days 20 through 23
- `reason`: a fixed build order reduces scope drift and keeps the implementation aligned with the latest explicit client direction
- `alternatives`: continue selecting the next slice opportunistically, or begin tennis implementation before football Day 11A and Day 12 close
- `consequences`: Days 7 through 10 stay closed unless a correctness issue is found, football work remains first until Day 12 closes, and the handoff/docs must treat the new sequence as operationally binding
- `status`: active

## DEC-023

- `date`: 2026-08-18
- `context`: after Day 10 closure, the next step needs to advance the football logic path without quietly widening into Day 12 vetoes, tennis work, staking, or live-provider integration
- `decision`: define the next bounded implementation iteration as Day 11A only: a provider-independent football manager core that consumes accepted Day 10 fair prices plus a supplied immutable market/reference snapshot, applies compliant de-vig, settlement-aware edge arithmetic, competition thresholds, and cup restrictions, and emits a full decision log plus Day 11 product outputs
- `reason`: this preserves the closed Day 7 through Day 10 model baseline while completing the next real decision-layer boundary in a way that is auditable and showable
- `alternatives`: start Day 12 safeguards immediately, begin tennis before the football manager path closes, or connect live odds providers during the same iteration
- `consequences`: Day 11A outputs must be explicitly labeled as manager-core or historical/demo when live provider dependencies are not present; Day 12, tennis, staking, and production polling remain intentionally out of scope for the next prompt
- `status`: active

## DEC-024

- `date`: 2026-08-18
- `context`: the Day 11A prompt is now fully specified and needs to avoid drifting back into an internal-only artifact or a provider-coupled implementation
- `decision`: Day 11A will use frozen Day 10 fair-price rows as immutable model authority, consume a provider-independent supplied normalized market snapshot, calculate booksum plus compliant `Power` and/or `Shin` de-vig, compute settlement-aware edge and minimum acceptable price, apply threshold and cup rules, and emit preliminary candidate / no-bet / rejected outcomes together with manager-facing Day 11 product outputs
- `reason`: this creates the first honest football manager boundary without overstating live readiness or reopening accepted Day 7 through Day 10 model layers
- `alternatives`: recompute fair prices inside Day 11, tie the manager to one odds-provider shape, skip no-bet logging, or postpone manager-facing outputs until later
- `consequences`: Day 11A must remain provider-independent, full-slate, deterministic, and explicitly historical/demo when live inputs are not present; `DAY11_MANAGER_CORE_READY` and `LIVE_PAPER_PIPELINE_READY` must remain separate statuses

## DEC-025

- `date`: 2026-08-19
- `context`: the user explicitly changed the next product surface from more research/report packaging into an operational football dashboard for current fixtures and frozen pre-match predictions
- `decision`: pause tennis, preserve the accepted football Day 7 through Day 12 math stack, and make the active milestone `OPERATIONAL FOOTBALL DASHBOARD V1` built around a real current-fixture provider, a current forward football snapshot, server-side frozen prediction generation, and a local FastAPI dashboard/API
- `reason`: the product question has shifted from "what historical research outputs do we have?" to "what football matches are happening now or next, and what is Sports Edge AI currently predicting?"
- `alternatives`: continue extending report-only handoff artifacts, or fake current fixtures from historical rows
- `consequences`: the dashboard must show real live/today/upcoming fixtures when available, must not fabricate matches, must not reinterpret the pre-match model as an in-play model, and must keep current odds/xG/runtime blockers explicit rather than hidden
- `status`: active
- `status`: active

## DEC-025

- `date`: 2026-08-18
- `context`: the Day 11A scope, constraints, and handoff preparation are now complete, and the next prompt is intended to trigger the actual build rather than another round of planning
- `decision`: treat the next Codex run as the Day 11A implementation run itself, requiring end-to-end manager construction, real decision artifacts from supplied market snapshots, manager-facing outputs, acceptance audit updates, and an explicit stop before Day 12
- `reason`: the project now has enough locked scope and baseline evidence to move directly into implementation without another preparatory cycle
- `alternatives`: continue refining handoff language, re-plan the roadmap again, or treat the next pass as documentation-only work
- `consequences`: the next run should prioritize code, tests, product artifacts, and acceptance evidence over restating the plan; documentation updates should follow the implementation rather than replace it
- `status`: active

## DEC-026

- `date`: 2026-08-18
- `context`: the Day 11A implementation run has now completed from the frozen Day 10 archive and the in-repo supplied historical/demo market snapshots
- `decision`: mark Day 11A complete for the currently loaded football scope, freeze its manager-core arithmetic and normalized market contract as the new upstream baseline, and track `DAY11_MANAGER_CORE_READY` separately from `LIVE_PAPER_PIPELINE_READY`
- `reason`: the repo now has implemented normalization, booksum validation, Shin devig, settlement-aware edge/minimum-price arithmetic, threshold handling, full-slate decision logging, and manager-facing Day 11 outputs without claiming live/provider readiness that has not been proven
- `alternatives`: leave Day 11A as "in progress" despite implemented outputs, or silently treat the manager-core completion as proof that live paper betting is ready
- `consequences`: the next bounded football task is Day 12 safeguards only; Day 10 and Day 11A should not be reopened casually; live-provider integration and current forward-production-fit football parameters remain explicit parallel dependencies
- `status`: active

## DEC-027

- `date`: 2026-08-19
- `context`: the accepted next prompt is now the full Day 12 build prompt, and it adds explicit guardrails around football-specific safeguards, xG-data truthfulness, and the historical closing-`1X2` feed limitation
- `decision`: treat the next bounded implementation iteration as Day 12 only, preserve Day 11A as a frozen downstream input, preserve the archived closing-`1X2` run as historical/demo rather than point-in-time-live evidence, and allow Day 12 to remain `PARTIAL` if required point-in-time-valid xG input is genuinely unavailable
- `reason`: this keeps the repo aligned with the latest explicit client instruction while preventing Day 12 from being falsely marked complete through invented xG or by overstating what the archived `1X2` input proves
- `alternatives`: widen the next run into tennis or live-provider work, reopen Day 11A arithmetic, or silently pass xG-veto requirements without a valid upstream xG source
- `consequences`: the next run should implement only football safeguards/veto logic, preserve the distinction between real historical market coverage and architecture-only coverage, and stop again for review before tennis begins
- `status`: active

## DEC-028

- `date`: 2026-08-19
- `context`: the accepted Day 12 prompt has now been tightened further to prevent another preparation-only pass and to require a fresh verified full-suite baseline before implementation begins
- `decision`: treat the next Day 12 run as the actual implementation pass, require a full root `pytest -q` verification before coding, require real Day 12 deliverables plus acceptance evidence, and explicitly allow `DAY12 STATUS = PARTIAL` if mandatory point-in-time-valid xG is genuinely unavailable
- `reason`: this protects the repository from starting Day 12 on top of a broken baseline, prevents another handoff-only cycle, and keeps the xG dependency honest instead of rewarding fabricated evidence
- `alternatives`: rely on only the handoff-bundle test as the pre-run baseline, soften the Day 12 deliverable requirement to docs-only work, or force a `COMPLETE` label even if mandatory xG is blocked
- `consequences`: the next run must report the actual green pre-Day-12 full-suite count, preserve Day 11A as frozen input, generate the Day 12 report/CSV/JSONL/manifest/audit, and stop again before tennis whether Day 12 ends `COMPLETE` or `PARTIAL`

## Wednesday, August 19, 2026 - Day 12 implemented honestly as partial

- `context`: the repo has now completed the actual Day 12 implementation pass, including real code,
  tests, product outputs, and a root-suite verification after execution
- `decision`: accept the Day 12 safeguard layer as implemented but mark the milestone `PARTIAL`
  because the current repository still lacks a connected point-in-time-valid xG source for the
  competitions where the football spec requires that check
- `reason`: this preserves truthful status reporting while still closing the bounded engineering work
  that could be completed without fabricating upstream data
- `evidence`: root-suite baseline before coding `187 passed in 40.06s`; root-suite after
  implementation `195 passed in 33.22s`; real Day 12 run consumed `10299` Day 11 preliminary
  candidates and produced `4317` `EXPERIMENTAL_CANDIDATE` plus `5982`
  `BLOCKED_MISSING_XG_DATA`
- `consequences`: tennis remains unauthorized, `FOOTBALL_LOGICAL_PIPELINE_READY` remains `NO`,
  live paper readiness remains `NO`, and the next football dependency to resolve is the truthful xG
  data path rather than another arithmetic rewrite
- `status`: active

## DEC-029

- `date`: 2026-08-19
- `context`: Operational Football Dashboard V1 was running locally, but the verified August 19-26
  empty slate needed to be distinguished between a genuine no-fixture window and a provider/runtime
  issue before current-football runtime claims could be trusted
- `decision`: harden the active dashboard into `Football Dashboard V1.1 Current Runtime Validation`
  by surfacing provider query diagnostics, degraded-state reporting, manual refresh control,
  `next`/`predictions` APIs, and strict pre-match lock enforcement without changing the accepted
  Day 7 through Day 12 model mathematics
- `reason`: the real API-Football path is authenticated, but the configured plan blocks current
  season fixture rows for the scoped leagues, so the product must report that truth explicitly
  rather than implying that the slate was simply empty
- `alternatives`: leave the V1 empty state as-is, switch providers mid-iteration, or reopen the
  accepted football model layers
- `consequences`: Dashboard V1.1 now reports `fixture_feed.state = DEGRADED` with the actual
  provider-plan error, current prediction generation remains frozen pre-match only, and the next
  football runtime dependency is a provider path that can expose at least one real supported current
  fixture
- `status`: active
