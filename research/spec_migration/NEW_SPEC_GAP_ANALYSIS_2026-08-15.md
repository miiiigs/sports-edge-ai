# New Spec Gap Analysis

Last updated: Saturday, August 15, 2026

## Purpose

Map the current repository against the August 15, 2026 PDF-first specification set without
starting a destructive repository split or discarding useful baseline work.

This analysis follows the authority order recorded in
`research/spec_migration/NEW_SPEC_AUTHORITY_ORDER_2026-08-15.md`.

## Verified current repo state

- full test suite before the GAP migration slice: `79 passed`
- full test suite after the chronological shots GAP slice: `100 passed`
- source tree: `46` Python modules under `code/src/sports_ai`
- current storage implementation:
  - local SQLite football research store in `storage/football_research_store.py`
  - local SQLite paper/shadow ledger in `storage/paper_shadow_ledger.py`
  - `psycopg` is declared as a dependency, but the active repo implementation is SQLite-backed
    rather than Postgres-backed
- current football model surface:
  - `poisson_1x2_v1`
  - `poisson_1x2_v2`
  - `poisson_1x2_v3`
  - `poisson_1x2_v4`
  - generic `gap_ratings` core with shots as the first implemented path
  - chronological shots GAP walk-forward runner with reproducible artifacts
  - historical close-value lane using Pinnacle close versus retail close
- current tennis model surface:
  - simple Elo agent
  - point-level Monte Carlo simulator
- historical football ingest coverage currently loaded into the research store:
  - `25` ingest runs
  - `12,080` football events
  - `46,904` football market quote rows
  - competitions: `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons: `2122`, `2223`, `2324`, `2425`, `2526`
- GAP-input readiness from the actual stored rows:
  - generated report artifacts:
    - `research/football_research/football_gap_data_readiness_2026-08-15.md`
    - `research/football_research/football_gap_data_readiness_2026-08-15.json`
  - loaded scope entry count: `25`
  - missing expected scope entry count: `40`
  - loaded competitions: `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - missing competitions: `EFLC`, `FAC`, `FRA1`, `GER1`, `ITA1`, `SCO1`, `UCL`, `UEL`
  - currently loaded league-seasons are `COMPLETE` for:
    - shots
    - shots on target
    - corners
  - loaded `SP1` league-seasons have incomplete referee coverage
- current live/paper pipeline state:
  - persistent local review app exists
  - current watchlist/paper-shadow infrastructure remains usable as baseline operational research
  - this lane is still centered on legacy `1X2`, not the new football market path

## Classification legend

- `IMPLEMENTED_AND_REUSABLE`
- `IMPLEMENTED_NEEDS_MODIFICATION`
- `BASELINE_RESEARCH_ONLY`
- `NOT_IMPLEMENTED`
- `BLOCKED`

## Shared foundations and architecture

| Requirement | Status | Current evidence | Gap / migration note |
| --- | --- | --- | --- |
| PDF-first authority order | `IMPLEMENTED_AND_REUSABLE` | Recorded in `research/spec_migration/NEW_SPEC_AUTHORITY_ORDER_2026-08-15.md` | Keep using this as the migration guardrail. |
| Separate football and tennis repositories | `NOT_IMPLEMENTED` | Current repo is a single shared Python package containing football and tennis code | Dependency map must be completed before any split. |
| Separate football and tennis databases | `NOT_IMPLEMENTED` | Current repo uses shared local SQLite stores and shared review tooling | Concepts are reusable, deployment architecture is not. |
| No shared runtime between football and tennis | `NOT_IMPLEMENTED` | Both sports live in `code/src/sports_ai` | Existing shared runtime is transitional only. |
| Blind sports model before market reveal | `IMPLEMENTED_AND_REUSABLE` | `firewall.py`, `locking.py`, and watchlist prediction lock/market lock flow exist | Keep this pattern when rebuilding football and tennis separately. |
| Append-only market snapshots | `IMPLEMENTED_AND_REUSABLE` | `paper_shadow_market_snapshots` and `football_market_quotes` are append-only in practice | Postgres enforcement is still missing. |
| Immutable decision history including non-bets | `IMPLEMENTED_NEEDS_MODIFICATION` | Ledger stores `PASS`, `WATCH`, `PAPER_SHADOW_CANDIDATE`, `RESEARCH_SIGNAL` | New spec needs sport-specific `BET`, `NO_BET`, `VETOED`, plus sharper audit fields. |
| Postgres as the real system database | `IMPLEMENTED_NEEDS_MODIFICATION` | `psycopg` dependency exists, but active implementation is SQLite | Schema concepts exist; target backend is not implemented. |
| Missing `00-developer-brief.md` and `R1-R10` | `BLOCKED` | PDFs reference this file, but it is absent from the repo | Do not guess omitted rules or shared arithmetic/staking definitions. |

## Football requirements

| Requirement | Status | Current evidence | Gap / migration note |
| --- | --- | --- | --- |
| Historical football ingest from Football-Data | `IMPLEMENTED_AND_REUSABLE` | `football_history.py`, `football_data_co_uk.py`, research store, loaded five-season archive | Strong base for the new football build. |
| Coverage/null-rate auditing by league-season | `IMPLEMENTED_AND_REUSABLE` | `football_gap_data_readiness.py` measures actual stored-row completeness and match-date span per league-season | Reuse this report as the readiness gate instead of relying on CSV headers. |
| Field readiness vs scope readiness distinction | `IMPLEMENTED_AND_REUSABLE` | Readiness report classifies loaded rows separately from expected-but-missing scope as `NOT_LOADED` | Keep this distinction explicit as scope expands. |
| Canonical team identity / alias mapping | `NOT_IMPLEMENTED` | Current research store and first GAP slice still rely on single-source team names | Temporary single-source identity is acceptable for first GAP research only; full alias mapping remains required later. |
| GAP ratings using shots, shots on target, and corners | `IMPLEMENTED_NEEDS_MODIFICATION` | `agents/football/gap_ratings.py` is generic across supported statistics and tested with shots first | Needs experiment/persistence wiring and full multi-statistic execution. |
| Chronological historical shots GAP replay | `IMPLEMENTED_AND_REUSABLE` | `experiments/football_gap_shots_walkforward.py` runs deterministic pre-match predictions and post-match updates over readiness-approved history | Reuse this runner shell for `shots_on_target` and `corners` rather than rewriting chronology logic. |
| Hard rule that goals are not GAP inputs | `IMPLEMENTED_AND_REUSABLE` | GAP config/observation validation rejects goals-like statistics and tests enforce the rule | This requirement is now encoded, not just documented. |
| Home/away-specific GAP state | `IMPLEMENTED_AND_REUSABLE` | GAP state is keyed by team plus venue, with separate attack/defense records | Preserve this venue split when persistence is added. |
| Goal expectation generated downstream of GAP stats | `BASELINE_RESEARCH_ONLY` | Current Poisson layers estimate goals directly from historical goals/results | Existing expected-goals scaffolding is useful research, not spec-compliant implementation. |
| Dixon-Coles low-score correction | `IMPLEMENTED_NEEDS_MODIFICATION` | `poisson_1x2_v4.py` contains low-score adjustment math | Reuse the math, but rebuild it as a GAP downstream scoreline layer rather than a goals-first product path. |
| Over/under goals pricing | `NOT_IMPLEMENTED` | Current repo prices only `1X2` | Must be derived from the new scoreline path. |
| Both teams to score pricing | `NOT_IMPLEMENTED` | No BTTS market output in code | Missing. |
| Corners pricing | `NOT_IMPLEMENTED` | No corners market output in code | Missing despite data availability. |
| Asian handicap pricing including quarter-lines | `NOT_IMPLEMENTED` | No handicap pricing code exists | Missing. |
| `1X2` as primary football path | `BASELINE_RESEARCH_ONLY` | v1-v4 football models are all `1X2`-centric | Preserve as baseline research, do not delete. |
| Direct classification Route A for football | `NOT_IMPLEMENTED` | No logistic/GBM classifier on GAP outputs exists | Missing. |
| Scoreline Route B as cross-check layer | `IMPLEMENTED_NEEDS_MODIFICATION` | Current Poisson/DC scoreline logic exists for `1X2` | Reusable math, wrong upstream input path. |
| xG sanity check / veto input | `NOT_IMPLEMENTED` | No FBref/Understat intake or xG comparison logic in code | Missing. |
| Football veto agent | `NOT_IMPLEMENTED` | Current watchlist has simple gating, not the PDF veto layer | Missing lineup, injuries, manager-change, congestion, xG divergence, and market-move vetoes. |
| Cup market restrictions | `NOT_IMPLEMENTED` | No competition-type-specific market restriction logic | Missing. |
| Promoted/relegated team embargo | `NOT_IMPLEMENTED` | Current minimum-history gate catches some cases indirectly | Needs explicit rule and logging. |
| Sharp reference live feed | `BLOCKED` | Historical Pinnacle close exists in stored football quotes; live sharp feed is not connected | External provider / exchange decision is still unresolved. |
| Price scanner Type A (model edge) | `IMPLEMENTED_NEEDS_MODIFICATION` | Legacy watchlist compares model fair probability to available price | Current path does not use the new market priorities or GAP signals. |
| Price scanner Type B (price dispersion independent of model) | `IMPLEMENTED_NEEDS_MODIFICATION` | Historical close-value lane logs `RESEARCH_SIGNAL` from retail versus Pinnacle close | Current lane is close-only historical research, not live scanner operation. |
| Distinct Type A vs Type B historical reporting | `IMPLEMENTED_NEEDS_MODIFICATION` | Watchlist and historical close lane are already separated conceptually | Keep the separation, but formalize it in the new football build. |
| Football live/paper execution loop | `IMPLEMENTED_NEEDS_MODIFICATION` | Review app + watchlist + ledger exist | Current flow is `1X2` only and should remain baseline while GAP architecture matures. |

## Tennis requirements

| Requirement | Status | Current evidence | Gap / migration note |
| --- | --- | --- | --- |
| Tennis in its own standalone build | `NOT_IMPLEMENTED` | Tennis code lives inside the shared package | Must be split only after dependency mapping. |
| Surface-adjusted Elo | `IMPLEMENTED_NEEDS_MODIFICATION` | `agents/tennis/elo.py` has a simple Elo book and agent | Current version lacks surface separation, Sackmann K-factor, and shrinkage rules from the new spec. |
| Barnett-Clarke serve/return adjustment | `NOT_IMPLEMENTED` | No tennis serve/return data ingest or opponent-adjustment module exists | Missing. |
| Recursive Markov hierarchy | `IMPLEMENTED_NEEDS_MODIFICATION` | `agents/tennis/simulation.py` models scoring flow via Monte Carlo | Reuse scoring intuition only; required exact recursive Markov path is not implemented. |
| Intransitivity filter / graph agent | `NOT_IMPLEMENTED` | No graph/intransitivity code exists | Missing. |
| Three-agent manager | `NOT_IMPLEMENTED` | No tennis manager matching the August 15 spec exists | Missing. |
| Tennis veto as kill-only layer | `NOT_IMPLEMENTED` | No tennis veto module exists | Missing. |
| ATP + Challenger historical ingest and odds join | `NOT_IMPLEMENTED` | No tennis data ingestion layer exists in repo | Missing. |
| Blind independence enforced architecturally | `IMPLEMENTED_NEEDS_MODIFICATION` | `manifest/agents_manifest.json` marks two tennis agents as `A_BLIND` and firewall exists | Spec requires separate processes and three independent agents; current setup is only conceptual. |

## Measurement, pricing, and safeguards

| Requirement | Status | Current evidence | Gap / migration note |
| --- | --- | --- | --- |
| De-vig methods including power and Shin | `IMPLEMENTED_AND_REUSABLE` | `agents/market/devig.py` and `test_devig.py` cover proportional, power, Shin, and consensus weighting | Good shared arithmetic candidate, subject to any blocked `00-developer-brief.md` clarifications. |
| Calibration tracking | `IMPLEMENTED_NEEDS_MODIFICATION` | `evaluation/metrics.py` and football backtest calibration buckets exist | Reusable, but not yet wired into the new football and tennis product loops. |
| Walk-forward historical evaluation | `IMPLEMENTED_AND_REUSABLE` | Football backtest, Track B development, and finalization flows are chronological and season-scoped | Strong reusable research infrastructure. |
| Configuration-count discipline | `IMPLEMENTED_NEEDS_MODIFICATION` | Experiments and artifacts are logged, but there is no single enforced configuration counter | Needs stronger formal logging under the new spec. |
| CLV logging with closing-margin adjustment | `IMPLEMENTED_NEEDS_MODIFICATION` | `evaluation/clv.py` exists and ledger captures close updates | Current CLV formula is simpler than the PDF requirement and does not include the `(1 + closing_margin)` division. |
| Paper-trading / paper-shadow mode | `IMPLEMENTED_AND_REUSABLE` | Ledger, review app, watchlist, and historical signal lane already exist | Keep this infrastructure and rename/restructure as needed per sport. |
| Data freshness gates | `IMPLEMENTED_NEEDS_MODIFICATION` | Provider diagnostics and snapshot timestamps exist | No full fail-closed freshness gate matching PDF thresholds is implemented. |
| Monitoring / reporting | `IMPLEMENTED_NEEDS_MODIFICATION` | Review app, dashboard, summary, and experiment docs exist | Need new-spec weekly reporting, alert thresholds, and sport-specific ops views. |
| Quarter-Kelly vs half-Kelly rule | `BLOCKED` | PDFs and Markdown disagree; missing `00-developer-brief.md` may contain the actual shared rule | Must be clarified before staking implementation is treated as authoritative. |

## Current repo assets that should be preserved

### `IMPLEMENTED_AND_REUSABLE`

- `firewall.py`
- `locking.py`
- `agents/market/devig.py`
- `storage/football_research_store.py` schema ideas and intake flow
- `storage/paper_shadow_ledger.py` immutable-decision and snapshot concepts
- football historical ingestion and export pipeline
- football backtest / Track B experiment scaffolding
- `experiments/football_gap_data_readiness.py`
- goals-prohibition and GAP-isolation tests

### `IMPLEMENTED_NEEDS_MODIFICATION`

- `agents/football/gap_ratings.py`
- `pipeline/football_watchlist.py`
- `review_app.py`
- `evaluation/clv.py`
- `evaluation/metrics.py`
- tennis Elo and tennis scoring code
- current SQLite stores as a transitional persistence layer
- `poisson_1x2_v4.py` low-score correction math

### `BASELINE_RESEARCH_ONLY`

- `poisson_1x2_v1`
- `poisson_1x2_v2`
- `poisson_1x2_v3`
- `poisson_1x2_v4`
- historical football close-value lane as a research-only signal study
- all existing `1X2` Poisson experiment artifacts
- current live `1X2` watchlist lane

## Architecture conflicts

1. The repo is still a single shared runtime, while the new PDFs require separate football and tennis builds.
2. Football legacy development is still centered on goals-based `1X2` Poisson variants, while the new football authority is GAP on shots, shots on target, and corners.
3. Tennis code is only a lightweight reference scaffold and does not yet match the new three-agent architecture.
4. Persistence is currently SQLite-first, while the target design is Postgres-backed with stronger immutability and audit guarantees.
5. The current repo still lacks a canonical multi-provider football identity layer.
6. The current successful research/live-operational lane depends on legacy `1X2`, not the new football market order.

## Data / provider blockers

1. `00-developer-brief.md` is missing.
2. Live sharp reference feed is unresolved.
3. API-Football / lineup / injury feed is not connected.
4. Canonical team alias resolution across provider inputs is not implemented.
5. Public xG intake is not connected.
6. Full football-spec historical scope is not yet loaded into the research store.

## Recommended safe migration order

1. Keep the current repo intact as the migration workspace.
2. Preserve football `1X2` Poisson v1-v4 as baseline research only.
3. Use the new football GAP readiness report as the authoritative field/scope gate.
4. Reuse the new walk-forward shell for `shots_on_target`.
5. Reuse the same shell for `corners`.
6. Only after the three statistic-specific runners are proven, fit lambda chronologically by league and statistic.
7. Rebuild football Route A and Route B on top of GAP outputs, not on historical goals.
8. Only after dependency mapping is complete, split football and tennis into separate repos and databases.

## Single smallest next coding task

Reuse the same chronological walk-forward framework for `shots_on_target`, keeping the same
readiness gate, warm-up policy, deterministic ordering, and duplicate-event protection.

Why this is now the next task:

- the stored-row readiness gate now exists and has been run against the real research database
- the generic GAP core and the chronological shots runner are both now proven mechanically
- the next smallest useful step is to validate the same state machinery on `shots_on_target`
  without yet touching lambda fitting, goal conversion, pricing, or scanning
