# Blockers

Last updated: Saturday, August 15, 2026

## Technical

- live PostgreSQL schema, roles, grants, and trigger-enforced immutability are still missing
- opening-week football risk gating is still basic and needs stricter context penalties
- API-Football is documented as a future option but not implemented in code
- the first Track B recency-weighted model experiment did not beat the frozen baseline and remains rejected
- the promoted `v3` historical model still needs future repeated validation across additional folds or later untouched periods before it should be treated as strongly proven
- the new `v4` Dixon-Coles layer has only cleared validation so far and still needs controlled finalization before any promotion decision

## Data

- current bookmaker-price coverage for Saturday, August 15, 2026 fixtures is still incomplete
- sharp-reference live prices are not yet consistently captured beside retail prices
- lineup, injury, manager-change, and transfer context are not yet integrated into the football gate
- MLB reverse-engineering still lacks exact-date confirmation for many of the 22 sample picks

## Provider / operations

- no approved paid football provider is connected yet
- no production odds-snapshot schedule is running yet outside manual / command-triggered capture
- current fixture discovery can still depend on source availability and league coverage
- the persistent review app can now run the promoted `v3` live watchlist, but it still depends on either a dependable provider or manual fixture / market imports
- the latest Saturday, August 15, 2026 watchlist run returned `fixtures_discovered = 0`, so live operation still needs a dependable fixture source before any candidate can exist
- the `2526` season has already been viewed repeatedly, so it is no longer a perfectly clean future test set for iterative model-family research

## Client confirmation

- which bookmaker(s) matter most for the first football trial comparisons
- whether API-Football credentials will be provided later
- whether MLB remains research-only for now or becomes a future production sport
- which football markets should be prioritized immediately after the first `1X2` paper/shadow loop

## Practical meaning

These blockers do not prevent the project from running a controlled football paper/shadow trial or from continuing Track B model research. They do prevent the project from honestly calling the system production-ready, model-validated, or live-pick ready.
