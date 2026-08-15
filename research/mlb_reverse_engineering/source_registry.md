# MLB Reverse Engineering Source Registry

Date created: 2026-08-13

This file is MLB-sample specific. Global provider decisions belong in `docs/DATA_SOURCE_REGISTRY.md`.

## Confirmed Current Sources

| Source | Type | Current use | Reliability note | Status |
| --- | --- | --- | --- | --- |
| `initial_data/bets_referral.png` | screenshot | Normalize visible 22 plays and screenshot win/loss marks | Useful for listed play text only; not enough for dates, books, or timestamps | ACTIVE |
| `initial_data/Sports-AI-Developer-Pack-V1(2).pdf` | design reference | Architectural constraints and Pack skeleton evidence | High value for architecture, not a historical MLB market source | ACTIVE |
| `Baseball-Reference` | public historical results | Candidate event matching and score verification | Strong for schedules, results, and box-score dates; not itself a sportsbook source | ACTIVE |
| `Action Network` | public odds reference | Candidate market-line matching | Useful for public game-line references, but not a complete point-in-time source-of-record for entry price or sportsbook identity | ACTIVE |
| `NBC Sports betting previews` | public odds reference | Supplemental candidate line checks | Useful for public preview line references; not a complete historical market database | ACTIVE |
| `EV Analytics previews` | public odds reference | Supplemental candidate line checks | Useful for candidate O/U snapshots in some games; still not enough for exact pick-time provenance | ACTIVE |

## Needed Historical Source Categories

| Category | Why needed | Point-in-time critical? | Current status |
| --- | --- | --- | --- |
| MLB schedules/results | identify exact candidate events | yes | NEEDED |
| Historical odds snapshots | opening, pick-time, close, movement | yes | NEEDED |
| Sportsbook identity | determine which book priced the pick | yes | NEEDED |
| Starting pitchers | sports context reconstruction | yes | NEEDED |
| Lineups/injuries | test whether move was news-driven | yes | NEEDED |
| Weather/park factors | totals and run-environment context | yes | NEEDED |
| Ticket/money percentages | evaluate line-resistance claims | yes | UNVERIFIED_AVAILABILITY |

## Known Unknowns

- No exact dates are visible in the screenshot.
- No sportsbook is visible in the screenshot.
- No entry price or pick timestamp is visible in the screenshot.
- Repeated team-only plays imply multiple plausible candidate events.

## Current Candidate Notes

- `MLB22-001`: 2024-06-08 Mets vs Phillies is a live candidate because public sources show total `9.5` and the final score was `7-2`.
- `MLB22-004`: 2024-06-07 Dodgers vs Yankees is a live candidate because public sources show total `9` and the final score was `2-1`.
- `MLB22-007`: 2024-07-09 Marlins vs Astros is a live candidate, but public sources disagree between `8` and `8.5`.
- `MLB22-008`: more than one Royals-Tigers under `8.5` winner appears plausible, so it remains unresolved.
- `MLB22-021`: 2024-05-05 matches the loss shape, but current public line references show `8.5`, not the listed `9.5`.
