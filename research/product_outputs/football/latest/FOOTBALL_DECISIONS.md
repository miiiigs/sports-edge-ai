# Football Day 11 Manager Report

**HISTORICAL / DEMONSTRATION MARKET INPUT**

**NOT LIVE BETTING OUTPUT**

**PRELIMINARY DAY 11 MANAGER DECISIONS**

## Summary

- manager version: `football_day11_manager_core_v1`
- final de-vig method: `shin`
- threshold config version: `football_day11_competition_thresholds_v1`
- decision records: `28992`
- preliminary candidates: `10299`
- no bets: `18693`
- rejected by rule: `0`
- missing required market data: `0`
- stale markets: `0`
- `DAY11_MANAGER_CORE_READY`: `YES`
- `LIVE_PAPER_PIPELINE_READY`: `NO`

## Representative Decisions

| Competition | Fixture | Market | Model | Best | Edge | Threshold | Decision | Reasons |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| ENG1 | evt_football_20220805_crystal_palace_arsenal | 1x2 HOME | 2.517 | 5.010 | 99.05% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_fulham_liverpool | 1x2 HOME | 4.032 | 11.950 | 196.35% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_fulham_liverpool | 1x2 DRAW | 4.166 | 6.930 | 66.36% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_newcastle_nott_m_forest | 1x2 DRAW | 4.040 | 4.300 | 6.44% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_bournemouth_aston_villa | 1x2 HOME | 2.471 | 4.250 | 71.97% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_leeds_wolves | 1x2 HOME | 2.209 | 2.500 | 13.15% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_tottenham_southampton | 1x2 DRAW | 3.995 | 5.500 | 37.67% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_tottenham_southampton | 1x2 AWAY | 3.038 | 10.000 | 229.20% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_newcastle_nott_m_forest | 1x2 AWAY | 2.605 | 7.000 | 168.68% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_everton_chelsea | 1x2 HOME | 3.839 | 7.080 | 84.44% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220806_everton_chelsea | 1x2 DRAW | 3.866 | 4.300 | 11.23% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220807_leicester_brentford | 1x2 AWAY | 2.859 | 4.400 | 53.89% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220807_man_united_brighton | 1x2 AWAY | 3.012 | 5.000 | 66.02% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220807_west_ham_man_city | 1x2 DRAW | 4.493 | 5.550 | 23.52% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220807_west_ham_man_city | 1x2 HOME | 5.220 | 8.500 | 62.84% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220813_aston_villa_everton | 1x2 AWAY | 3.361 | 5.370 | 59.78% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220813_man_city_bournemouth | 1x2 DRAW | 4.614 | 15.000 | 225.09% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |
| ENG1 | evt_football_20220813_brighton_newcastle | 1x2 HOME | 2.250 | 2.560 | 13.76% | 5.00% | PRELIMINARY_CANDIDATE | PASS_EDGE_THRESHOLD, PASS_MINIMUM_PRICE |

## Notes

- This report uses frozen Day 10 model prices plus supplied historical/demo market snapshots only.
- No live provider polling, staking, Kelly sizing, or Day 12 veto logic is applied here.
- The current historical/demo market input available in-repo is 1X2 only, so this manager run is a real full-slate 1X2 decision pass rather than a claim of full live-market coverage.
