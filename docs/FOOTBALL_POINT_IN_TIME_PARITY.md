# Football point-in-time integrity and replay parity

Status: audit, 2026-10-13. This PR adds provenance only. The behavioural
safeguards are in a separate PR.

Compared:
- **Live:** `dashboard/snapshot_service.py` built from the tracked research DB
  (cutoff 2026-05-24).
- **Validated walk-forward replay:** Day 8/9/10 manifests, used by #116.
- **Production:** read-only snapshot taken 2026-10-12 15:22Z.

## 1. Parameter fitting: live vs replay

| Parameter | Live snapshot | Walk-forward replay (#116) | Assessment |
|---|---|---|---|
| GAP shots/SOT/corner ratings | Predict-then-update over every completed match | Same | Identical protocol |
| xG conversion (NNLS, competition × venue) | Expanding window, every completed match from 2021/22, refit at each snapshot build | Expanding window from 2021/22, refit per season fold (prior seasons only) | **Same protocol.** Live also uses the current season so far, which is valid for forward fixtures |
| Dixon-Coles rho | In-sample MLE on the expanding window: 0.0 for all five competitions | Selected out of sample: 0.0, except ENG4 −0.05 | Differs for ENG4 only (|Δρ| = 0.05) |
| xi (time decay) | From the Day 9 manifest (0.0), not applied in the matrix | 0.0 | Identical |
| Corner family | In-sample log-likelihood: NB for all five | Prior out-of-sample folds: NB for all five | Identical choice |
| Corner NB dispersion | ENG1 90.6, ENG2 69.8, ENG3 55.4, ENG4 92.5, ESP1 74.0 | 2025/26 fold: 83.3, 71.7, 63.6, 77.6, 87.6 | Moderate; live includes 2025/26 data |

**Conclusion.** The live fit is not leakage for forward fixtures, because every
research match predates every fixture it prices. Its protocol matches the replay
except for ENG4 rho and dispersion drift. The replay's out-of-sample calibration
(AH ECE 0.011) is therefore a fair proxy for live behaviour.

The real risk is predicting a fixture from a model state that already contains
that fixture's day. That would leak.

## 2. Point-in-time checks in production

At the 2026-10-12 snapshot:

| Check | Result |
|---|---|
| Predictions whose `model_as_of` date ≥ kickoff date | **0** of 625 |
| Predictions generated at/after kickoff | 27, all `BLOCKED` |
| Model Picks from a prediction generated at/after kickoff | 0 |
| Model Picks whose source `model_as_of` ≥ kickoff date | 0 |

This holds only implicitly: research data lags results by days, and READY
predictions lock at kickoff. `prediction_service` has **no explicit guard**.
The behavioural PR adds one: a fixture is blocked when `model_as_of` date ≥
kickoff date.

## 3. Team history (evidence for the behavioural PR)

Live history is venue-specific: home matches observed for the home team, away
matches for the away team, within the competition and carried across seasons.
Currently one match suffices. Calibration of every AH catalog line in the
replay, by the minimum of the two venue counts:

| Prior venue matches | Selection ECE (2022/23–2023/24) | Holdout ECE (2024/25–2025/26) | Holdout fixtures |
|---|---|---|---|
| 0 (already blocked live) | 0.043 | 0.057 | 48 |
| **1 (currently admitted)** | **0.067** | 0.015 | 46 |
| 2 | 0.012 | 0.019 | 45 |
| 3–4 | 0.012 | 0.022 | 86 |
| 5+ | 0.003 | 0.011 | 4,607 |

The threshold chosen on the selection seasons is **≥ 2 prior venue matches**.
Promoted teams show no separate effect once history is controlled for, because
competition-scoped team ids already reset them to zero.

## 4. Provenance added here

READY predictions now carry `model_parameters`:
- `fit_protocol`, `research_cutoff`, `model_snapshot_id`;
- the exact conversion coefficients, `rho` (with its source), `xi` (with its
  source) and the corner model.

A prediction can therefore be reproduced after the snapshot changes. Already
recorded before this PR: `model_versions`, `model_as_of`, `model_history`, and
`rho`/`xi`/dispersion inside the scoreline and corner objects.

Known label defect, left unchanged so object ids stay stable:
`corner_object.gap_model_version_corners` holds the statistic name
(`"corners"`) rather than the version. `model_versions.gap_model_version_corners`
is correct.

## 5. Probability and settlement semantics (pinned by tests)

- `effective_win_stake_probability` = P(FW) + ½P(HW), unconditional.
- `1/fair` = W / (W + L), push-excluded; complements sum to 1 per line.
- Summing the live Dixon-Coles matrix through the paper settlement engine
  (`settlement_state_for_market`) reproduces every AH row's win, loss and push
  mass to 1e-9.
