# Current Fixture Model Coverage Audit

Date: Thursday, August 20, 2026

## A. Current Slate Summary

- Railway production baseline from the user-supplied Thursday, August 20, 2026 status: `51 discovered / 32 supported / 23 ready / 27 blocked`
- local stored football-data.org snapshot available in this repo: `46 discovered`
- local environment verified on Thursday, August 20, 2026 does not expose `SPORTS_AI_FOOTBALL_DATA_TOKEN`, so this audit replays the stored current fixture snapshot rather than a fresh live sync
- local deterministic snapshot rebuild on Thursday, August 20, 2026 produced `current_snapshot_77b65ccf14b446c1f7aa52d3daed76a512b2d1070c05fdf0d84ee7f7cfd6f378` with `model_as_of = 2026-05-24T23:59:59+00:00`
- the specific Railway snapshot id supplied in the brief, `current_snapshot_ad88f657255d67b34cd27ed6ce88280103a2ca7c1a194f0e3254ee33174f637b`, was not directly present in the local runtime artifacts, so the snapshot-state conclusions below are based on the local deterministic rebuild

## B. Competition Mapping Table

| Fixture | Provider competition | Provider code/id | Internal competition | Supported |
| --- | --- | --- | --- | --- |
| Arsenal vs Coventry City | Premier League | PL/2021 | ENG1 | YES |
| Hull City vs Man United | Premier League | PL/2021 | ENG1 | YES |
| West Ham vs Charlton | Championship | ELC/2016 | ENG2 | YES |
| Preston NE vs Wolverhampton | Championship | ELC/2016 | ENG2 | YES |
| Atleti vs Málaga | Primera Division | PD/2014 | ESP1 | YES |
| Getafe vs Santander | Primera Division | PD/2014 | ESP1 | YES |

Result: the suspicious mappings are validated as provider-truth, not an internal competition-map bug.

## C. Remaining Unresolved Teams

- `Deportivo` | provider=`Deportivo` | competition=`ESP1` | reason=`club identity may be known, but no approved current model history mapping exists yet`

## D. Model-History Missing Teams

- `Coventry City` -> `Coventry` | current competition=`ENG1` | missing inputs=`shots, shots_on_target, corners`
- `Hull City` -> `Hull` | current competition=`ENG1` | missing inputs=`shots, shots_on_target, corners`
- `Lincoln City` -> `Lincoln City` | current competition=`ENG2` | missing inputs=`shots, shots_on_target, corners`
- `West Ham` -> `West Ham` | current competition=`ENG2` | missing inputs=`shots, shots_on_target, corners`
- `Wolverhampton` -> `Wolves` | current competition=`ENG2` | missing inputs=`shots, shots_on_target, corners`
- `Bolton` -> `Bolton` | current competition=`ENG2` | missing inputs=`shots, shots_on_target, corners`
- `Santander` -> `Racing Santander` | current competition=`ESP1` | missing inputs=`shots, shots_on_target, corners`
- `Málaga` -> `Malaga` | current competition=`ESP1` | missing inputs=`shots, shots_on_target, corners`

## E. Historical Data Evidence

Team: `Coventry City`
Canonical name: `Coventry`
Historical matches: `230`
Latest historical match: `2026-05-02`
Stats available: `shots=230` / `shots_on_target=230` / `corners=230`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Hull City`
Canonical name: `Hull`
Historical matches: `230`
Latest historical match: `2026-05-02`
Stats available: `shots=230` / `shots_on_target=230` / `corners=230`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Lincoln City`
Canonical name: `Lincoln City`
Historical matches: `230`
Latest historical match: `2026-05-02`
Stats available: `shots=230` / `shots_on_target=230` / `corners=230`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `West Ham`
Canonical name: `West Ham`
Historical matches: `190`
Latest historical match: `2026-05-24`
Stats available: `shots=190` / `shots_on_target=190` / `corners=190`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Wolverhampton`
Canonical name: `Wolves`
Historical matches: `190`
Latest historical match: `2026-05-24`
Stats available: `shots=190` / `shots_on_target=190` / `corners=190`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Bolton`
Canonical name: `Bolton`
Historical matches: `230`
Latest historical match: `2026-05-02`
Stats available: `shots=230` / `shots_on_target=230` / `corners=230`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Santander`
Canonical name: `Racing Santander`
Historical matches: `0`
Latest historical match: `NONE`
Stats available: `shots=0` / `shots_on_target=0` / `corners=0`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Málaga`
Canonical name: `Malaga`
Historical matches: `0`
Latest historical match: `NONE`
Stats available: `shots=0` / `shots_on_target=0` / `corners=0`
Prediction eligible: `NO`
Reason: `MODEL_HISTORY_MISSING_MULTIPLE`
Team: `Deportivo`
Canonical name: `UNRESOLVED`
Historical matches: `0`
Latest historical match: `NONE`
Stats available: `shots=0` / `shots_on_target=0` / `corners=0`
Prediction eligible: `NO`
Reason: `TEAM_IDENTITY_UNRESOLVED`

## F. Fixes Applied

- added deterministic football-data.org provider-team-id coverage for `ESP1 / 84 / Málaga -> Malaga`
- preserved the strict resolver rule that bare `Deportivo` stays unresolved without provider-id or club-context evidence
- confirmed that the accepted current football GAP snapshot is competition-specific; historical state is not transferred automatically across leagues for promoted/relegated clubs

## G. Legitimate Blockers Remaining

- `Coventry City`, `Hull City`, `Lincoln City`, `West Ham`, `Wolverhampton`, `Bolton`, and `Santander` all have real historical rows elsewhere in the loaded archive, but they have `0` rows in their current model competition inside the accepted snapshot, so the present block is legitimate under the current competition-specific GAP design
- `Málaga` can now be identified deterministically, but it still has no loaded `ESP1` model history in the accepted snapshot
- bare `Deportivo` remains unresolved intentionally because the local stored snapshot does not contain a matching live provider payload and the short name is not safe to auto-map without provider-id evidence
- a fresh live verification of the Railway-like current slate is still blocked locally by the missing `SPORTS_AI_FOOTBALL_DATA_TOKEN`

## H. Before / After Prediction Coverage

- production baseline from the brief: `23 ready / 27 blocked`
- stale local stored runtime before current-code replay: `16 ready / 30 blocked`
- stored local snapshot after current-code replay: `21 ready / 24 blocked`
- stored local supported-current identity failures improved from `13` unresolved team-side blocks in the persisted local prediction rows to `0` unresolved team-side blocks in the current replay

