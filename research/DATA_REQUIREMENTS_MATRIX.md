# Data Requirements Matrix

Date created: 2026-08-13

This matrix is used to decide which ideas are honestly testable before building many agents.

| Variable | Sport | Zone | Historical required? | Live required? | Source candidate | Point-in-time available? | Reliability | Cost/license | Needed hypothesis | Needed agent/module | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| starting_pitcher | MLB | A_BLIND | yes | optional | historical game feeds / lineup feeds | likely, if timestamped | unknown | unknown | HYP-006 | future MLB pitching module | NEEDED_SOURCE_REVIEW |
| bullpen_workload | MLB | A_BLIND | yes | optional | play-by-play / prior-game logs | likely | unknown | unknown | HYP-006 | future MLB bullpen module | NEEDED_SOURCE_REVIEW |
| park_factor | MLB | A_BLIND | yes | no | public stats datasets | yes | medium | low | HYP-006 | future MLB environment module | TESTABLE_IF_SOURCED |
| weather | MLB | A_BLIND | yes | yes | weather archives / forecast APIs | sometimes | medium | unknown | HYP-003,HYP-006 | future MLB weather module | NEEDED_SOURCE_REVIEW |
| projected_lineup | MLB | A_BLIND | yes | yes | lineup providers / beat reports | uncertain historically | low to medium | unknown | HYP-003,HYP-006 | future MLB lineup module | PARTIAL_DATA_RISK |
| confirmed_lineup | MLB | A_BLIND | yes | yes | official feeds / lineup services | yes if timestamped | medium | unknown | HYP-003,HYP-006 | future MLB lineup module | NEEDED_SOURCE_REVIEW |
| injuries | MLB | A_BLIND | yes | yes | injury feeds / reports | uncertain historically | low to medium | unknown | HYP-003 | future MLB injury module | PARTIAL_DATA_RISK |
| team_strength | MLB | A_BLIND | yes | optional | results/stat databases | yes | medium | low | HYP-001,HYP-006 | future MLB strength module | TESTABLE_IF_SOURCED |
| opening_line | MLB | B_MARKET | yes | yes | historical odds provider | only if timestamped | unknown | unknown | HYP-001,HYP-002,HYP-003,HYP-005 | M01/M03-style modules | NEEDED_SOURCE_REVIEW |
| pick_time_line | MLB | B_MARKET | yes | yes | historical odds provider | only if timestamped | unknown | unknown | HYP-001,HYP-003,HYP-005 | M01/M03-style modules | NEEDED_SOURCE_REVIEW |
| closing_line | MLB | B_MARKET | yes | yes | historical odds provider | yes | unknown | unknown | HYP-001,HYP-005 | M01/M02/CLV evaluation | NEEDED_SOURCE_REVIEW |
| book_identity | MLB | B_MARKET | yes | yes | historical odds provider | yes | unknown | unknown | HYP-004,HYP-005 | M01/M04-style modules | NEEDED_SOURCE_REVIEW |
| market_consensus | MLB | B_MARKET | yes | yes | multi-book odds provider | yes if sourced | unknown | unknown | HYP-001,HYP-002,HYP-004 | M02/M06-style modules | NEEDED_SOURCE_REVIEW |
| ticket_percentage | MLB | B_MARKET | yes | optional | public betting-share vendor | uncertain | low | unknown | HYP-002,HYP-003 | research-only market-behavior module | BLOCKED_BY_DATA |
| money_percentage | MLB | B_MARKET | yes | optional | public betting-share vendor | uncertain | low | unknown | HYP-002,HYP-003 | research-only market-behavior module | BLOCKED_BY_DATA |
| line_movement_path | MLB | B_MARKET | yes | yes | timestamped odds feed | yes if sourced | unknown | unknown | HYP-003,HYP-005 | M03-style module | NEEDED_SOURCE_REVIEW |
| fair_line | cross-sport | A_BLIND output | yes | yes | model output | yes | n/a | n/a | HYP-001 | T20/F20/future MLB fair-pricing module | FUTURE_BUILD |
| devig_consensus | cross-sport | B_MARKET | yes | yes | derived from timestamped odds | yes if inputs sourced | medium | source-dependent | HYP-001,HYP-004 | M02 | FUTURE_BUILD |
| entry_timestamp | cross-sport | B_MARKET | yes | yes | bettor release log / provider snapshots | currently no | low | unknown | HYP-005 | research logging and M01 timeline | BLOCKED_BY_DATA |
| surface_elo | tennis | A_BLIND | yes | yes | tennis historical results | yes | medium | low | Pack ATP loop | T02 | PACK_V1_PRIORITY |
| serve_percentage | tennis | A_BLIND | yes | yes | tennis stats feeds | yes | medium | unknown | Pack ATP loop | T03 | PACK_V1_PRIORITY |
| return_percentage | tennis | A_BLIND | yes | yes | tennis stats feeds | yes | medium | unknown | Pack ATP loop | T04 | PACK_V1_PRIORITY |
| hold_break_profile | tennis | A_BLIND | yes | yes | derived tennis stats | yes | medium | unknown | Pack ATP loop | T05 | PACK_V1_PRIORITY |

## Football Route A Note

Updated: `2026-08-27`

The Football Route A direct-classification lane was revalidated using only internal Day 7 process-stat features with nested historical GAP fitting and a manifest-gated runtime contract.

Result on Thursday, August 27, 2026:

- no additional external data source was required for the remediation
- the clean point-in-time rebuild still rejected both Route A markets
- Route B remains the only active Football runtime agent
