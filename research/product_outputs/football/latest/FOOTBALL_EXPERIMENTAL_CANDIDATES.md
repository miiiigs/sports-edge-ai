# Football Day 12 Experimental Candidate Report

**HISTORICAL / DEMONSTRATION MARKET INPUT**

**NOT LIVE BETTING OUTPUT**

**NOT REAL-MONEY RECOMMENDATIONS**

## Summary

- Day 12 version: `football_day12_safeguards_v1`
- Day 11 preliminary candidates received: `10299`
- experimental candidates: `4317`
- rejected by AH rule: `0`
- rejected by xG veto: `0`
- rejected by multiple vetoes: `0`
- blocked missing xG data: `5982`
- derivation errors: `0`
- review required: `0`
- survival percentage: `41.92%`
- `DAY12 STATUS`: `PARTIAL`
- `FOOTBALL_LOGICAL_PIPELINE_READY`: `NO`
- `LIVE_PAPER_PIPELINE_READY`: `NO`
- `TENNIS AUTHORIZED`: `NO`

## Real Coverage

- real historical market coverage: `archived closing 1X2 only`
- code / unit-test market support: `1X2 plus generic AH safeguard logic and xG-contract coverage`

## xG Dependency Inspection

- xG source present in repo run-path: `False`
- xG source path: `None`
- xG competitions covered: `[]`
- xG seasons covered: `[]`
- xG sufficient for Day 12 rule: `False`
- xG pass count: `0`
- xG veto count: `0`
- xG missing / blocked count: `5982`
- xG not applicable count: `4317`

## AH Safeguard

- AH pass count: `0`
- AH reject count: `0`
- AH not applicable count: `10299`

## Extreme Edge Diagnostics

| Bucket | Input | Survived | Rejected | Blocked |
| --- | ---: | ---: | ---: | ---: |
| <10% | 1536 | 703 | 0 | 833 |
| 10-25% | 3583 | 1638 | 0 | 1945 |
| 25-50% | 2786 | 1222 | 0 | 1564 |
| 50-100% | 1743 | 635 | 0 | 1108 |
| >100% | 651 | 119 | 0 | 532 |

## Examples

### Experimental Candidate Example

- fixture: `evt_football_20220730_plymouth_barnsley`
- teams: `Plymouth vs Barnsley`
- market: `1x2 AWAY`
- best price: `3.750`
- edge: `10.02%`
- disposition: `EXPERIMENTAL_CANDIDATE`
- reasons: `AH_NOT_APPLICABLE, XG_NOT_REQUIRED_FOR_COMPETITION`

### AH Rejection Example

- No AH rejection exists in this real run because the in-repo historical/demo input covers archived closing 1X2 only.

### xG Rejection Example

- No xG veto exists in this real run because no point-in-time-valid xG source is connected in the current repo path.

### Blocked xG Example

- fixture: `evt_football_20220805_crystal_palace_arsenal`
- teams: `Crystal Palace vs Arsenal`
- market: `1x2 HOME`
- best price: `5.010`
- edge: `99.05%`
- disposition: `BLOCKED_MISSING_XG_DATA`
- reasons: `XG_INPUT_PATH_UNCONFIGURED`

## Representative Outputs

| Competition | Fixture | Market | Best | Edge | AH | xG | Final |
| --- | --- | --- | ---: | ---: | --- | --- | --- |
| ENG3 | Plymouth vs Barnsley | 1x2 AWAY | 3.750 | 10.02% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Accrington vs Charlton | 1x2 HOME | 2.900 | 54.69% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Derby vs Oxford | 1x2 AWAY | 3.760 | 32.59% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Cheltenham vs Peterboro | 1x2 HOME | 3.290 | 40.86% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Lincoln vs Exeter | 1x2 AWAY | 2.900 | 10.28% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Port Vale vs Fleetwood Town | 1x2 AWAY | 4.400 | 12.99% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Ipswich vs Bolton | 1x2 AWAY | 4.750 | 22.98% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Wycombe vs Burton | 1x2 AWAY | 4.900 | 20.59% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Cambridge vs Milton Keynes Dons | 1x2 AWAY | 2.530 | 14.60% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Morecambe vs Shrewsbury | 1x2 HOME | 3.400 | 47.45% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Shrewsbury vs Accrington | 1x2 AWAY | 3.650 | 46.56% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Milton Keynes Dons vs Sheffield Weds | 1x2 HOME | 3.280 | 34.82% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Charlton vs Derby | 1x2 HOME | 3.020 | 31.43% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Barnsley vs Cheltenham | 1x2 AWAY | 4.750 | 40.25% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Peterboro vs Morecambe | 1x2 AWAY | 6.140 | 49.33% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Forest Green vs Ipswich | 1x2 HOME | 4.180 | 51.33% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Fleetwood Town vs Plymouth | 1x2 HOME | 3.110 | 34.75% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
| ENG3 | Exeter vs Port Vale | 1x2 AWAY | 3.630 | 19.04% | AH_NOT_APPLICABLE | XG_NOT_APPLICABLE | EXPERIMENTAL_CANDIDATE |
