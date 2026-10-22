# Football production recovery and release gate — 2026-10-22

**Evidence cutoff:** 2026-10-22 ~06:00 UTC. **Disposition:** RELEASE BLOCKED; read-only investigation only. This document is **not** a live readiness claim and does not authorize merge, deploy, runtime configuration changes, provider traffic, credential rotation, or volume operations.

## Deployment and storage inventory

- Railway project `8d5cd78d-c59e-446f-9134-9b6e08a675a2`, production environment `95f7c08b-a17f-4453-989b-d81678bef562`.
- Application `sports-edge-ai` (`1a68e1fa-782c-42cb-ad3f-22bda6b9a8ec`): deployment `23906230-d9c3-47f3-b5e3-3ff38298cd3f`, state SLEEPING, released 2026-10-14 from `main` SHA `6c67200c2ea529a242f3fb18e43f7c85a5104865`. `sleepApplication=true`.
- Cron `football-hourly-wake-v2` (`7e81b40f-03d5-41d6-80dc-a7e4ed9f0504`): `alpine:3.20`, `20 * * * *`, command begins `sh -ec ...` and requests `/health`. Railway marks the cron execution active, but not the application processing as completed.
- Older `football-hourly-wake` (`e799b6fc-8529-4a82-a01b-d616163cc704`) is a non-cron curl image service.
- Volume `bb006227-73b2-491d-9442-449c14d714a3`: `sports-edge-ai-volume`, mounted at `/data`, capacity **30,000 MB**. Railway 24-hour disk usage metric near evidence cutoff: **2.795 GB**; this is a service metric, not a verified on-volume SQLite inventory. No backup / restore result was obtained.
- No Railway staged changes. Application tracing disabled; no distributed traces returned for the inspected window.

## 1. Wake callback versus completed work

- Railway cron deploy logs for 2026-10-20 through 2026-10-22 contain repeated **Starting Container** messages, not `wake-start`, `wake-ok` or a verified callback/completion record.
- App HTTP logs contain occasional `GET /health` 200 by `curl/8.5.0`, at irregular times. Those responses do **not** prove cron ownership or scheduler completion.
- App startup logs **do** prove some fixture syncs complete when requests or other traffic wake the app (2026-10-22 04:02:40 UTC: 56 upcoming, 51 READY, 5 BLOCKED). This does not prove an independent hourly cadence.
- Existing draft [#137](https://github.com/miiiigs/sports-edge-ai/pull/137), integrating [#134](https://github.com/miiiigs/sports-edge-ai/pull/134) + [#136](https://github.com/miiiigs/sports-edge-ai/pull/136), introduces durable `wake_evidence_state`, read-only `/health/wake?since=...`, and exit-status-checked Railway cron runner. Do not replace with another wake implementation.
- After owner-authorized release, correlate scheduled minute, cron runner's `wake-start`, HTTP callback, scheduler persisted last useful cycle after the start timestamp, and `wake-ok`; monitor at least 24 hours. **A 200 from `/health` is not the acceptance condition.**

## 2. SQLite settlement failure and safe boundary

- Confirmed production error 2026-10-21 22:20:30 UTC: `sqlite3.OperationalError: database is locked`, stack `scheduler_worker.py:153` -> `services.py:5656` -> `_persist_model_pick_settlement_state` -> `storage.py:505`. Another occurrence logged 2026-10-20 15:04:56 UTC.
- The traced failing operation is the **dashboard runtime status UPSERT**, after the separate settlement-ledger flow. It cannot establish whether a given ledger entry was or was not settled. Writer owning the competing SQLite lock remains unidentified.
- #137's `_persist_model_pick_settlement_state_with_busy_retry` uses at most three attempts with fresh connections, closes them, retries only SQLite BUSY/LOCKED for this status checkpoint and never repeats the settlement ledger. Its tests cover release of a real WAL writer lock, lock exhaustion, idempotent rerun and non-BUSY schema error.
- **Do not automatically replay ledger settlement** on status-checkpoint failure. Do not infer single/duplicate settlement from logs: first reconcile a separately approved read-only SQLite snapshot including forward-issued and settled Model Pick ledger rows, verified sporting-truth rows and checkpoint state.

## 3. Exactly five blocked fixtures: 2026-10-22 04:02 UTC startup

| Fixture | Competition | Match (UTC kickoff) | Logged blocker |
| --- | --- | --- | --- |
| `api-football:1557443` | ENG1 | Ipswich – Nottingham Forest, Oct 23 19:00 | `AWAY_TEAM_IDENTITY_UNRESOLVED` |
| `api-football:1563219` | ENG2 | Lincoln – Burnley, Oct 24 14:00 | `HOME_MODEL_HISTORY_MISSING` |
| `api-football:1563224` | ENG2 | Stoke City – Bristol City, Oct 24 14:00 | `HOME_TEAM_IDENTITY_UNRESOLVED` |
| `api-football:1564380` | ENG3 | Blackpool – Mansfield Town, Oct 24 14:00 | `AWAY_TEAM_IDENTITY_UNRESOLVED` |
| `api-football:1564390` | ENG3 | Stevenage – Milton Keynes Dons, Oct 24 14:00 | `AWAY_MODEL_HISTORY_MISSING` |

The 2026-10-22 startup totals were **56 upcoming / 51 READY / 5 BLOCKED**. Existing **unmerged** [#122](https://github.com/miiiigs/sports-edge-ai/pull/122) includes #112 identity corrections for Nottingham Forest, Stoke City and Mansfield Town. Existing **unmerged** [#125](https://github.com/miiiigs/sports-edge-ai/pull/125) addresses the Lincoln / Milton Keynes Dons competition-catalogue spelling mismatch. Re-test the same IDs with read-only snapshot replay after the corrections have been combined; verify their canonical IDs, approved aliases and mature model history. Do not fabricate teams, bypass the eight-match maturity guard, or change live predictions.

## 4. FRA1 historical-data freshness and API budget

- App freshness log at 2026-10-22 04:02 UTC: research state READY; fresh divisions `E0,E1,E2,E3,SP1`; `FRA1` last complete match `2026-05-29`, age **146 days**, against a **21-day** threshold.
- `FRA1_PREEXEC_READONLY` at 2026-10-22 04:02 UTC reports season 2025 API-Football job **COMPLETE** (`307/307`), no failed or pending rows, circuit **CLOSED**, diagnostic **OK**, no calls or writes from the diagnostic, and operational remaining **4445** out of 4500. This establishes the previous backfill's checkpoint state, not current-season freshness.
- Existing [#124](https://github.com/miiiigs/sports-edge-ai/pull/124) identifies the root mismatch: scheduled research refresh covers `E0,E1,E2,E3,SP1`, while FRA1 is an unsupported one-off history competition. It proposes scoping **aggregate** freshness gating to the model's supported competitions while retaining unsupported-stale diagnostics. It does not refresh the FRA1 database.
- A paid FRA1 refresh is outside scope and needs explicit provider budget/cost approval. Do not rerun the completed 2025 history job or treat a completed backfill as current-season coverage.

## 5. Market activity, result sync and Model Picks

- 2026-10-21 app startup logs show current market collector `COLLECTION_ONLY`, provider connected, up to **480 snapshots** for one matched fixture; latest 2026-10-22 startups show zero current fixtures, zero new snapshots and no paper entries. This is window-specific, not proof the collector has stopped globally.
- 5Dollar shadow logs (2026-10-22 04:02 UTC): 18 considered, 2 selected, 5 calls, 16 reused rows, **zero newly written**, 12 ever-covered, 8 corner-covered and 10 missing corners. The proposed unchanged-price rotation fix is already [#126](https://github.com/miiiigs/sports-edge-ai/pull/126), unmerged; do not increase provider caps or turn shadow output into live picks.
- Result synchronization and Model Pick settlement loops log `started: interval_seconds=600` on app startup; no standalone successful completion and reconciliation counts were established by this audit.
- Prediction **READY** does not mean a genuinely **LIVE_ISSUED** Model Pick. The existing issuance path is separate. Counts of forward-issued, settled and outstanding picks **remain unverified** until an approved read-only ledger snapshot is available. No issuance flag changes are authorized.

## 6. Backup, rollback, security and acceptance gate

- Official Railway [volume backup documentation](https://docs.railway.com/volumes/backups) states manual backups are limited to 50% of volume capacity; 30,000 MB capacity therefore implies a nominal 15,000 MB manual-backup ceiling. Current service disk usage is below that but does **not** prove an eligible or successful backup.
- Existing draft [#122](https://github.com/miiiigs/sports-edge-ai/pull/122) provides the SQLite backup approach; follow-up [#128](https://github.com/miiiigs/sports-edge-ai/pull/128) documents critical research-DB, WAL, free-space and integrity guards. No authorized production backup/restore verification was observed here. A backup created but not restore-tested does not clear this gate.
- **Do not restore to the live volume during recovery preparation.** Railway volume restore stages a replacement mount and redeploy; a live-data mistake could be destructive. Preserve `/data` and independently verify offline exports and hashes.
- Railway's [Oct 21 security disclosure](https://blog.railway.com/p/security-disclosure-wednesday-october-21st) advises rotating Railway-stored environment secrets and private registry credentials after potential third-party access. The [Railway status page](https://status.railway.com/) reports build/deployment restrictions and platform degradation; require an explicit fresh platform clearance before deploying. **No credentials were fetched or rotated by this audit.**
- Production app rollback candidate is `6c67200c2ea529a242f3fb18e43f7c85a5104865` / Railway deployment `23906230-d9c3-47f3-b5e3-3ff38298cd3f`. App rollback and cron-command rollback are **separate** coordinated actions; neither should touch `/data`. Rollback feasibility metadata is not a rehearsed recovery.
- Release gates (all required): (1) owner/security clearance and secret rotation plan; (2) actual verified off-volume backup and restore rehearsal; (3) exact merged-code CI and known legacy failures categorized; (4) confirmed publication/settlement baselines from read-only snapshots; (5) rollback plan + owner approval; (6) separately coordinated app and cron changes; (7) 24h wake-to-completion, settlement, and publication acceptance.

## 7. CI and non-deployment guarantee

- #137 exact-head `bb7b83a46cbca689ce59c1ba5b83887a41f952b0` passed [Football CI #39377400904](https://github.com/miiiigs/sports-edge-ai/actions/runs/39377400904): **1363 passed, 14 deselected, one warning** under the configured regression gate.
- The full legacy suite produced **1363 passed / 14 failed**; CI's debt-visibility step does not make those failures disappear. Tests are valid code evidence, not production acceptance.
- This documentation-only PR changes no executable code, runtime flag, Railway config, model rules, SQLite database, or provider access. **No deploy or production mutation was performed.**
