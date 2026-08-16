# Blockers

Last updated: Saturday, August 15, 2026

## Technical

- `00-developer-brief.md` is referenced by the governing PDFs, but is not present in the repo, so
  `R1-R10` and shared arithmetic/staking references cannot be fully verified
- the supplied PDFs and companion Markdown files are not identical, so requirements must continue
  to be resolved through the PDF-first authority order rather than merged casually
- the final football/tennis split architecture is still unresolved at the dependency-map level, so
  the current repo remains a temporary migration workspace
- the live production PostgreSQL design is still unresolved and should not be assumed from the
  current SQLite research implementation

## Data

- the football historical scope required by the PDFs is not fully loaded yet:
  missing competitions in the current research store are `EFLC`, `FAC`, `FRA1`, `GER1`, `ITA1`,
  `SCO1`, `UCL`, and `UEL`
- canonical multi-provider football team identity / alias mapping is not implemented yet
- `SP1` referee coverage is incomplete in the current loaded scope; this does not block GAP field
  readiness, but it does limit later referee/cards/veto functionality for that league

## Provider / operations

- no resolved sharp-reference live feed is connected yet for the football migration target
- lineup, injury, manager-change, and other veto-layer context feeds are still unresolved
- no approved production odds-snapshot schedule or final football live-runtime design is in place

## Client confirmation

- whether `00-developer-brief.md` can be provided or its omitted rules should be restated elsewhere
- whether quarter-Kelly or half-Kelly is authoritative once the upstream brief question is resolved
- which sharp-reference provider should be treated as authoritative for football live scanning
- when the football/tennis repository and database split should be executed after dependency mapping

## Practical meaning

These blockers do not prevent continued football GAP migration work inside the current repository.
They do prevent the project from honestly claiming that the full football-spec archive, staking
rules, provider stack, or final deployment architecture are already settled.
