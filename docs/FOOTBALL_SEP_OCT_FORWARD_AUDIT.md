# September–October Football forward performance comparison

**Status: analysis tool only. No production ledger was retrieved, no production settings were changed, and no betting / provider calls were made.**

## Research question

Did genuine forward-issued Asian Handicap and Corner Model Picks change in performance between September 2026 and October 2026, or is the discrepancy attributable to cumulative/legacy paper-trading metrics?

Use only a **consistent standalone offline copy** of the production football_model_picks.sqlite created with SQLite's online backup API. Railway connector logs and deployment status **cannot substitute** for the underlying ledger. The production file may be in WAL mode; copying just the .sqlite main file while the app is awake is unsafe.

## Read-only procedure

1. Use the owner-controlled production backup and verification runbook in draft PR #122 (docs/FOOTBALL_M1_BACKUP_RUNBOOK.md). The backup must be kept in encrypted owner-controlled storage outside Git and must be verified. Its auth database must never be uploaded to GitHub, Trello, chat, or an unsecured provider.
2. Obtain a **standalone verified logical copy** of football_model_picks.sqlite from the backup. The auditor is not an extraction tool and will reject any copy with .sqlite-wal/.sqlite-shm sidecars.
3. From the repository root, with its normal Python environment:

       python -m sports_ai.dashboard.football_period_comparison --ledger-copy C:/secure/path/football_model_picks.sqlite --snapshot-as-of 2026-10-14T00:00:00+00:00 --output football-forward-period-report.json

   **Replace the illustrative snapshot time with the actual verified copy timestamp.** Alternatively run with PYTHONPATH=code/src on a clean Python 3.10+ environment; only standard-library dependencies are used.
4. Inspect status: RECONCILED_OFFLINE_COPY is a read-only, date-scoped tally, not a claim that every sporting result is final. INTEGRITY_BLOCKED (exit 3) means duplicate identities, post-kickoff sources, invalid trade states, or other integrity failures. Do not publish a blocked result. A missing/corrupt/unsafe DB exits 2.
5. Inspect source rows, excluded cohorts, pending and void counts, and issuance lead times. Reconcile remaining results only through authorized verified sporting-truth processes. Re-run on a new offline backup.

## Periods, denominator and calculations

Kickoff date in Europe/London time (not issuance time):
- September 1–30.
- October 1–9.
- October 10–11 (Brian's weekend).
- October 12–14.
- October 1–14 total, **overlapping** the prior three October slices.

Only rows from football_model_pick_entries with record_origin=LIVE_ISSUED, created_at strictly earlier than kickoff_utc, balanced-line policy and market=asian_handicap/corner_total qualify. The reporter excludes all retrospective rows, paper positions, replay picks and qualified Value Bets. Different population, not comparable with paper strike rate or ROI.

Per market, per league and per kickoff day:
- Issued, settled, pending, void, full win, half win, push, half loss, full loss.
- Stake-weighted win share = (full wins + half of half wins) / (full wins + half of half wins + full losses + half of half losses), pushes excluded.
- Legacy display weighted-success rate is reported separately; do not conflate.
- Median hours between issuance and kickoff, and count >=48 hours early.
- A period with no issued picks or no decisive outcomes has null win share, **not 0%**.
- No bookmaker P&L or ROI is computed from model-only ledger; these rows have no offered price.

## What is already known, not a September comparison

Production deployment logs from Sept 26 and Oct 14 repeatedly reported exactly the same **cumulative** paper metrics:
- Legacy AH: 791 counted positions, 778 settled, +451.005 units / +57.97% historical paper ROI. Those entries are untrustworthy due to the v1 API-Football AH-sign normalization defect documented in PR #106, and are awaiting the reporting quarantine of draft PR #109.
- Corners: 283 counted historic paper entries, 281 settled, +27.71 units / +9.86% ROI, still not a date-scoped forward Model Pick measure.
- Weekend Oct 10–11, in the Oct 13 03:34 UTC frozen earlier ledger snapshot: 80 forward issued, 60 settled, 20 pending, 45.2% stake-weighted overall win share, AH 42.0%, corners 48.1%. **Provisional.**
- The production AH/Corners focus began at 2026-09-26 10:16:20.567997950 UTC. That boundary is for paper-focus analysis and must not be substituted for September 1–30 Model Pick issuance.

The empty September cohort, if encountered, cannot by itself distinguish no historical issuance from a missing ingestion period.

## Release and safety

This script and its tests are for offline audit only. It does not require PR #122 to merge or deploy and does not mutate the original Railway /data volume. Keep this PR in draft. Any operational changes, paid result backfills, Value Bet publication, historical data correction or deployment require separate review. Record the snapshot verification timestamp, report status, and aggregate counts in the existing Trello performance card; do not upload a production database.
