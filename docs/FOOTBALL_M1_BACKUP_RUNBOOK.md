# Football `/data` backup and restore runbook (M1 release gate)

Status 2026-10-15: **procedure prepared and rehearsed locally; no production backup
has been taken.** 2026-10-15 update: the inventory now includes
`football_research_live.sqlite`, the backup fails closed on any unrecognised
SQLite file and on insufficient `/tmp` space, and the copy ships a `SHA256SUMS`
file for off-volume verification. A written procedure is not a backup. Release step 3
(`FOOTBALL_M1_RELEASE_CANDIDATE.md` §6) stays open until the owner has run
section 3 and recorded the evidence in section 5.

## 1. What is being protected (verified read-only, 2026-10-14; updated 2026-10-15)

`railway status --json`, project `sports-edge-football`, environment `production`:

| Item | Value |
|---|---|
| Service | `sports-edge-ai`, deployment `6c67200c…`, `sleepApplication=true`, status SLEEPING |
| Volume | mounted at `/data`, state READY, **30000 MB size, about 2647 MB used** (2026-10-14/15; it was 1003 / 2000 MB before the resize) |
| Other services | `football-hourly-wake` and `football-hourly-wake-v2`, no volume. **Neither wakes the app:** both active deployments have no start command (see `docs/FOOTBALL_WAKE_REPAIR_AND_PROVIDER_UTILIZATION.md`, #132). Only the GitHub workflow wakes it, about 4–6 times a day |

| File in `/data` | Contents | Sensitivity |
|---|---|---|
| `sports_edge_auth.sqlite` | users, password hashes, sessions | **secret** |
| `football_dashboard.sqlite` | fixtures, predictions, results, runtime state, provider quota/circuit state | internal |
| `football_paper_trading.sqlite` | market snapshots, paper/value positions | internal |
| `football_model_picks.sqlite` | Model Pick ledger (immutable fields, triggers) | internal |
| `football_research_live.sqlite` | persistent model history: seeded once from the bundled baseline, then refreshed (E0–E3, SP1) and extended by paid API-Football backfills (FRA1). Losing it means re-buying the backfill and re-seeding | internal |
| `football_high_confidence_shadow.sqlite` | only exists once the shadow flag is enabled (it is not) | internal |

2026-10-15: the volume was resized to **30000 MB, 2647 MB used** (1003 MB on
10-14). The extra ~1.6 GB is not yet explained. Step 1 of section 3 lists every
SQLite file under `/data` before anything is copied; any file not in the table
above stops the backup until it is added here and to `DATABASES` (or explicitly
included with `--include-unknown` / acknowledged with `--skip NAME`).

**Paths outside `/data`.** `SPORTS_AI_AUTH_DB_PATH` and
`SPORTS_AI_HIGH_CONFIDENCE_SHADOW_DB_PATH` can move those databases off the
volume, and the inventory only scans `/data`. Before the backup, confirm by
**variable name only** (never print values) that neither is set, or that each
points under `/data`:

```powershell
railway variables --kv | ForEach-Object { ($_ -split '=',2)[0] } | Select-String 'DB_PATH|DATA_DIR'
```

If either is set to a path outside `/data`, stop and extend the inventory first.

All databases run in WAL mode, so a file copy of `*.sqlite` alone can miss committed
rows that are still in `*.sqlite-wal`. **Never copy the files directly.**

## 2. Two independent layers

| Layer | What | Why |
|---|---|---|
| A. Railway volume backup | Copy-on-write, incremental backup of the whole volume, restored as a new volume | Fast whole-volume restore; all DBs from one instant |
| B. Logical copy | `sports_ai.diagnostics.football_sqlite_backup`: SQLite online backup API from `mode=ro` sources, `PRAGMA integrity_check`, row counts and sha256 in `manifest.json` | Proves integrity, survives loss of the volume (Railway notes that wiping a volume deletes its backups), lets row counts be reconciled after deploy |

Both are required before the #122 deploy.

## 3. Owner procedure

### 3A. Railway volume backup

Resolved 2026-10-15 (volume now 30000 MB, 2647 MB used, about 9%): Railway caps
**manual** backups at 50% of the volume size, which blocked a manual backup at
1003 / 2000 MB on 10-14. Kept for reference, the options were:

1. Grow the volume (e.g. to 3 GB) in Railway → project canvas → click the volume → **Live Resize** (Hobby/Pro, zero downtime, grow-only; the Railway CLI has no resize command). Then take a manual backup. Storage is billed on used GB, not provisioned GB; or
2. Enable a **Daily** backup schedule (scheduled backups are not subject to the manual cap per Railway's docs; verify in the UI).

Then:

1. Take the backup **while the service is SLEEPING**. With no process running, nothing is writing, so every database is quiescent and consistent with the others at one instant. If it is awake, wait for it to sleep. Do not stop or restart it for this.
2. Record the backup's date stamp from the Backups tab in section 5.

Railway does not document whether a backup pauses a running service, so the sleeping window avoids the question.

### 3B. Logical copy

Exact sequence (owner, authorised execution window only):

1. **Inventory (read-only):** `inventory --source-dir /data --dest-dir /tmp/fb-<TS>`
   lists every SQLite file with its size and `-wal` size, and checks free space.
   Stop if `unknown` is not empty.
2. **Capacity:** `df -h /data /tmp`. Free space in `/tmp` must be at least
   1.2 × (all databases + `-wal`) + 64 MB; the tool refuses to start otherwise.
   With about 2.6 GB on the volume, `/tmp` needs roughly 3.2 GB free.
3. **Railway volume backup** (layer A, section 3A), taken while SLEEPING.
4. **Logical backup** into `/tmp/fb-<TS>` (this section).
5. **Stream out** as `tar | base64` (no extra disk needed in the container).
6. **Local verify** off-volume: `verify --backup-dir fb-<TS>` (sha256, integrity
   check on the copy, row counts, every required database present, nothing
   inventoried but not copied) and `sha256sum -c SHA256SUMS`.
7. **Delete** `/tmp/fb-<TS>` (and the uploaded module) only after step 6 passes.
8. **Record** the evidence in section 5.

Needs the container awake. Do not wake it just for this: a wake runs the app's own
start-up provider sync. Use a natural wake from the GitHub waker
(`football-production-wake.yml`, about 4–5 a day), polling `railway status --json`
until the deployment is no longer `SLEEPING`.

The backup module ships in #122, so before the deploy it is not in the container.
It is stdlib-only, so it is sent into the container from this branch.

**Supported path: the PowerShell wrapper below.** It is the only rehearsed
path. It uploads the module in small chunks, verifies locally, and deletes the
container copy only after verification passes.

**Manual Bash path: fallback only, unsupported until rehearsed.** Use the
PowerShell wrapper. If it truly cannot run, the sketch below mirrors what the
wrapper does. It must not inline the whole module as one `python -c` argument:
about 16 KB of base64 can exceed command-line limits. It uses `python3`
throughout, and **never deletes the `/tmp` copy before the local verify passes**.

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
MOD=/tmp/fb-$TS.mod
# Upload the module gzip+base64 in quote-free chunks of at most 6000 characters.
gzip -c code/src/sports_ai/diagnostics/football_sqlite_backup.py | base64 -w0 | fold -w 6000 > fb-mod-chunks.txt
railway ssh -s sports-edge-ai "rm -f $MOD"
while IFS= read -r chunk; do railway ssh -s sports-edge-ai "printf %s $chunk >> $MOD"; done < fb-mod-chunks.txt
RUN="base64 -d $MOD | gunzip | python3 -"
# 0. Inventory and capacity preflight (read-only on /data). Stop on any "unknown" file or a capacity error.
railway ssh -s sports-edge-ai "df -h /data /tmp"
railway ssh -s sports-edge-ai "$RUN inventory --source-dir /data --dest-dir /tmp/fb-$TS" > inventory-$TS.json
# 1. Online backup into the container's ephemeral /tmp (never into /data).
railway ssh -s sports-edge-ai "$RUN backup --source-dir /data --dest-dir /tmp/fb-$TS" > manifest-$TS.json
# 2. Stream it out (base64 keeps the stream text-safe over railway ssh).
railway ssh -s sports-edge-ai "tar -C /tmp -czf - fb-$TS | base64 -w0" > fb-$TS.tgz.b64
# 3. Verify locally (block further below). Only when it passes:
# 4. railway ssh -s sports-edge-ai "rm -rf /tmp/fb-$TS $MOD"
```

#### Windows PowerShell 5.1

The Bash commands above do not work as written in Windows PowerShell 5.1. It
re-encodes redirected native output as UTF-16, it strips embedded double quotes
from native arguments, and some launchers truncate command lines at about 8 KB.
`code/scripts/football/Invoke-FootballSqliteBackup.ps1` runs the same steps
safely:

- It refuses to start while the service is `SLEEPING` (it never wakes it).
- It sends the module gzip+base64 in quote-free chunks of at most 6000
  characters to `/tmp/fb-<TS>.mod`, then runs
  `base64 -d /tmp/fb-<TS>.mod | gunzip | python3 - …`. The copy goes to
  `/tmp/fb-<TS>`.
- Step 0 runs `inventory` and stops (removing the module file) if `/data`
  holds an unrecognised SQLite file or `/tmp` lacks space.
- It streams the archive back through `Start-Process -RedirectStandardOutput`
  (byte-exact) and decodes it locally in a stream, with whitespace tolerated.
- It verifies locally: module `verify`, `Get-FileHash` against the manifest,
  and the container manifest equal to the transferred one.
- It also re-hashes every file listed in `SHA256SUMS`.
- Only after verification passes does it remove `/tmp/fb-<TS>` and the module
  file, and confirm both are gone.
- It checks local free space before streaming: the destination drive needs at
  least 3 × the container-reported total source bytes, because the base64
  stream, the archive and the extracted copy all exist at once.
- It refuses a destination inside any Git working tree. It writes to
  `%USERPROFILE%\sports-edge-backups\football\<TS>\`, with the ACL reduced to the
  current user.

Start it **immediately after a natural wake begins**. A wake lasts about 6–20
minutes. If the container goes back to sleep mid-run, the run fails closed:
`/data` is untouched and only the ephemeral `/tmp` copy is lost. Rerun on the
next wake.

```powershell
# From the repository root, with `railway` linked to sports-edge-football / production,
# while `railway status --json` shows the service awake from a natural wake:
.\code\scripts\football\Invoke-FootballSqliteBackup.ps1
# Rehearsal on test databases (Git Bash plays the container; no Railway access):
.\code\scripts\football\Invoke-FootballSqliteBackup.ps1 -LocalRehearsal `
    -RehearsalSourceDir /c/path/to/test-data -DestRoot $env:TEMP\fb-rehearsal
```

It prints and writes `evidence.json`, which holds timestamps, integrity, sha256
and the reconciled-table counts only. The auth database's tables are not counted
in the evidence. Rehearsed 2026-10-14 against WAL-only test databases (every row
still in `-wal`) and a 274 MB database: the counts matched, `.sqlite`/`-wal` were
byte-identical afterwards (`-shm` changes, as expected), and the guards refused
SLEEPING and an in-repo destination.

Locally, outside the repository (Bash path), **before** deleting the container copy:

```bash
base64 -d fb-$TS.tgz.b64 > fb-$TS.tgz && tar -xzf fb-$TS.tgz
python3 -m sports_ai.diagnostics.football_sqlite_backup verify --backup-dir fb-$TS
(cd fb-$TS && sha256sum -c SHA256SUMS)
# Only if both pass:
railway ssh -s sports-edge-ai "rm -rf /tmp/fb-$TS /tmp/fb-$TS.mod"
```

`verify` must print `"ok": true`, and the manifest must show
`"all_integrity_ok": true` with no `missing_required`, `"schema":
"football_sqlite_backup_v2"`, and `football_research_live.sqlite` among the
databases. A v1 manifest (taken before 2026-10-15) fails `verify` because it
lacks the research database.

What the tool guarantees (pinned by `code/tests/test_football_sqlite_backup.py`):
- **Sources are opened `mode=ro`.** Nothing is written to `/data`: no checkpoint and no schema creation. The `.sqlite` and `-wal` bytes are unchanged; `-shm` is shared memory that readers legitimately update.
- **Committed rows still in `-wal` are included.** The online backup restarts if a writer changes the source mid-copy, so each copy is internally consistent.
- **Each copy is converted to a self-contained file** (`journal_mode=DELETE`) and integrity-checked.
- **The manifest holds row counts and hashes only.** No row contents and no source paths.
- **It refuses** to write inside the source directory or over an existing directory.
- **It refuses before copying anything** when an unrecognised SQLite file exists under the source directory, or when destination free space is below 1.2 × source bytes + 64 MB.
- **`reconcile` reads row counts only** on the live side; `--integrity-check` opts in to the heavy full-page check.

**Cross-database consistency.** There is no transaction spanning the files, so they are copied back to back in a fixed order: auth, ledger, research, dashboard, paper. The ledger goes before the dashboard, so every fixture a copied pick references exists in the (newer) dashboard copy. Seconds of skew are possible while awake. For an exact set, rely on layer A taken while sleeping. Layer B is for integrity evidence and off-platform survival.

**Exposure.** The /tmp copy holds the auth database briefly inside the production container. It is deleted once the local verify passes, and the container filesystem is discarded on sleep/redeploy anyway.

### 3C. Storage of backup artifacts

- Keep them on owner-controlled encrypted storage outside every Git working tree, for example `%USERPROFILE%\sports-edge-backups\football\<TS>\` on a BitLocker volume, or a private bucket with server-side encryption.
- Restrict access to the owner. `sports_edge_auth.sqlite` contains password hashes and live sessions.
- **Never** put backups, manifests with production numbers, or excerpts in Git, PR comments, Trello or chat. Only the timestamp, `verify` result and table counts go into section 5, and only on the owner's private record.
- Keep the last 3 pre-release copies. Delete older ones once the release is accepted (step 10).

## 4. Restore and rollback constraints

**Prefer not to restore.** #122 has no DDL. Rolling back the code (`git revert -m 1 <merge>`) plus flags OFF returns to `6c67200`, which reads the same databases. Restore only for corruption or accidental destructive writes.

**Every restore loses forward Model Picks issued after the backup.** They cannot be recreated, because their fixtures have kicked off and issuance is forward-only. Before any restore, take a fresh logical copy (3B) of the current state, so those rows remain auditable.

### Layer A restore (whole volume)

1. Turn issuance, refresh, shadow and backlog flags OFF.
2. Backups tab → Restore on the chosen date stamp. Railway stages a new volume at `/data`. The current volume is kept, unmounted, which makes this the rollback path for the restore itself.
3. Review the staged change, then Deploy.
4. Check `/health`, then `reconcile` against the matching logical copy. Restored counts must equal the backup counts.

Restoring removes the newer backups from the restored volume; they remain on the previous volume.

### Layer B restore (off-platform recovery from the logical copy)

**Layer A (volume restore) is the supported restore path.** Use layer B only if
the volume and its backups are lost, or layer A is unavailable. It is owner-run
and needs a maintenance deploy, because replacing a database under a running
process is unsafe.

**Restore as a set.** `football_dashboard.sqlite`, `football_paper_trading.sqlite`
and `football_model_picks.sqlite` reference each other's fixture ids, so restore
all three from the **same** `TS`. `football_research_live.sqlite` and
`sports_edge_auth.sqlite` may be restored on their own. Never mix files from
different copies inside the set.

1. **Stop all writers.** Turn issuance, refresh, shadow and backlog flags OFF.
   Deploy with the start command temporarily set to a no-op (for example
   `sleep infinity`), so no process opens `/data`. Confirm with
   `railway ssh -s sports-edge-ai "ps aux"` that no Python process is running.
2. **Verify the copy first.** Locally, rerun `verify` and `sha256sum -c
   SHA256SUMS` on the copy you will restore.
3. **Get the files onto the volume.** `railway ssh` has no upload command.
   Upload each file the same way the wrapper uploads the module: gzip+base64,
   split into quote-free chunks of at most 6000 characters, and append them with
   `printf %s <chunk> >> /data/restore-<TS>/<name>.gz.b64`. Then run
   `base64 -d … | gunzip > /data/restore-<TS>/<name>`. Large databases mean many
   chunks; if that is impractical, restore to a new Railway volume through
   layer A instead.
4. **Check the hash on the volume** before placing it:
   `cd /data/restore-<TS> && sha256sum <name>` must equal its `SHA256SUMS` line.
5. **Place it.** For each restored database:
   1. Move the current file aside: `mv /data/<name> /data/restore-<TS>/<name>.replaced`.
   2. Remove its stale `/data/<name>-wal` and `/data/<name>-shm`.
   3. Move the restored file in: `mv /data/restore-<TS>/<name> /data/<name>`.
6. **Ownership and permissions.** `ls -ln /data` must show the restored files
   with the same owner, group and mode as the files they replace. Fix with
   `chown`/`chmod` if they differ.
7. **Verify in place, before restarting.** For each restored database, run
   `PRAGMA integrity_check` through a `mode=ro` connection (`ok` expected), and
   compare row counts with the copy's `manifest.json` using `reconcile` (counts
   only).
8. **Restart.** Restore the normal start command and redeploy. Check `/health`,
   and check that the startup log shows no schema or integrity errors.
9. **Clean up.** Remove `/data/restore-<TS>` only after the release owner
   confirms the restore, and record the restore in section 5.

## 5. Evidence to record (owner)

| Field | Value |
|---|---|
| Railway backup date stamp (3A) | |
| Service state at backup | SLEEPING / awake |
| Logical copy timestamp `TS` (3B) | |
| `verify` result | |
| `all_integrity_ok` | |
| Inventory: files under `/data` and total bytes; `unknown` must be empty | |
| `/tmp` free bytes vs required (manifest `capacity`) | |
| `SHA256SUMS` check (`sha256sum -c`) | |
| Counts: `football_model_pick_entries`, `paper_trade_entries`, `market_snapshots`, `dashboard_predictions`, `dashboard_fixtures`, `football_events` | |
| Storage location (description only) | |

Release step 5 (post-deploy) compares these counts with production. Deploying #122 must not change them.
