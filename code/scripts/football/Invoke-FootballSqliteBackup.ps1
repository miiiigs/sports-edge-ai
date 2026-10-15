<#
.SYNOPSIS
    Windows PowerShell 5.1 path for the Football logical SQLite backup
    (docs/FOOTBALL_M1_BACKUP_RUNBOOK.md section 3B).

.DESCRIPTION
    1. Refuses to run unless the Railway service is already awake (it never wakes it).
    2. Streams sports_ai.diagnostics.football_sqlite_backup into the container over
       stdin-free `railway ssh` (base64 on the command line, no files uploaded). First runs
       `inventory` (every SQLite file under /data, with sizes, plus a free-space check on
       /tmp) and stops if an unrecognised database exists or /tmp is too small. Then runs
       `backup` against /data. Sources are opened mode=ro; copies go to /tmp/fb-<TS>.
    3. Streams /tmp/fb-<TS> back as tar.gz+base64, written byte-for-byte by
       Start-Process (no PowerShell re-encoding), and decodes it locally.
    4. Verifies locally: module `verify` (sha256, integrity_check, row counts),
       Get-FileHash against the manifest, and the container manifest == local manifest.
    5. Only after local verification passes, removes /tmp/fb-<TS> in the container.

    Remote commands contain no quotes, so neither PowerShell 5.1 argument passing nor the
    remote shell can mangle them.

    Output goes to <DestRoot>\<TS>\, which must be outside every Git working tree.
    The directory ACL is reduced to the current user. Nothing sensitive is printed:
    only counts, hashes and pass/fail.

.EXAMPLE
    # Production, during a natural wake window (run from the repository root):
    .\code\scripts\football\Invoke-FootballSqliteBackup.ps1

.EXAMPLE
    # Local rehearsal against test databases, using Git Bash as the "container":
    .\code\scripts\football\Invoke-FootballSqliteBackup.ps1 -LocalRehearsal `
        -RehearsalSourceDir /c/temp/fb-test-data -DestRoot $env:TEMP\fb-rehearsal
#>
[CmdletBinding()]
param(
    [string]$Service = "sports-edge-ai",
    [string]$DestRoot = (Join-Path $env:USERPROFILE "sports-edge-backups\football"),
    [string]$Python = "python",
    [switch]$KeepRemoteCopy,
    # Rehearsal: run the identical remote commands through Git Bash on this machine.
    [switch]$LocalRehearsal,
    [string]$RehearsalSourceDir,
    [string]$BashExe = "C:\Program Files\Git\bin\bash.exe"
)

$ErrorActionPreference = "Stop"
trap { Write-Error $_ -ErrorAction Continue; exit 1 }  # non-zero exit for -File callers
Set-StrictMode -Version 2.0

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$ModulePath = Join-Path $RepoRoot "code\src\sports_ai\diagnostics\football_sqlite_backup.py"
$WinTar = Join-Path $env:SystemRoot "System32\tar.exe"
$ReconciledTables = @{
    "football_dashboard.sqlite"     = @("dashboard_fixtures", "dashboard_predictions", "dashboard_fixture_results")
    "football_paper_trading.sqlite" = @("paper_trade_entries", "paper_trade_updates", "market_snapshots")
    "football_model_picks.sqlite"   = @("football_model_pick_entries")
    "football_research_live.sqlite" = @("football_events")
}

function Invoke-Remote {
    # Runs one quote-free shell command in the container (or Git Bash) and writes its
    # stdout byte-for-byte to $StdoutPath. Returns the exit code.
    param([string]$Command, [string]$StdoutPath, [string]$StderrPath)
    if ($Command -match '["''`$]') { throw "remote command must not contain quotes or `$" }
    if ($LocalRehearsal) {
        $exe = $BashExe
        $argLine = "-c `"$Command`""
    } else {
        $exe = Get-RailwayExe
        $argLine = "ssh -s $Service `"$Command`""
    }
    $p = Start-Process -FilePath $exe -ArgumentList $argLine -NoNewWindow -PassThru `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    $null = $p.Handle  # PS 5.1: keep the handle so ExitCode is populated
    $p.WaitForExit()
    # PS 5.1 occasionally reports a null ExitCode for a process that exits very quickly.
    for ($n = 0; $null -eq $p.ExitCode -and $n -lt 50; $n++) { Start-Sleep -Milliseconds 100; $p.Refresh() }
    if ($null -eq $p.ExitCode) { throw "exit code unavailable for remote command" }
    return $p.ExitCode
}

function Get-RailwayExe {
    # Start-Process needs a real PE file; npm installs `railway` as sh/.cmd/.ps1 shims around bin\railway.exe.
    foreach ($c in @(Get-Command railway -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -like "*.exe") { return $c.Source }
        $npmExe = Join-Path (Split-Path $c.Source -Parent) "node_modules\@railway\cli\bin\railway.exe"
        if (Test-Path $npmExe) { return $npmExe }
    }
    throw "railway.exe not found (install the Railway CLI)"
}

function ConvertFrom-Base64File {
    # Streaming decode; tolerates CR/LF a PTY may add.
    param([string]$InPath, [string]$OutPath)
    $transform = New-Object System.Security.Cryptography.FromBase64Transform(
        [System.Security.Cryptography.FromBase64TransformMode]::IgnoreWhiteSpaces)
    $in = [System.IO.File]::OpenRead($InPath)
    $out = [System.IO.File]::Create($OutPath)
    try {
        $crypto = New-Object System.Security.Cryptography.CryptoStream(
            $in, $transform, [System.Security.Cryptography.CryptoStreamMode]::Read)
        $crypto.CopyTo($out)
        $crypto.Dispose()
    } finally {
        $out.Dispose(); $in.Dispose()
    }
}

function Test-InsideGitWorkTree {
    param([string]$Path)
    $probe = $Path
    while ($probe -and -not (Test-Path $probe)) { $probe = Split-Path $probe -Parent }
    $null = Invoke-Native { & git -C $probe rev-parse --is-inside-work-tree 2>$null }
    return ($LASTEXITCODE -eq 0)
}

function Invoke-Native {
    # PS 5.1 turns native stderr into terminating errors under "Stop"; callers check $LASTEXITCODE.
    param([scriptblock]$Block)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $saved }
}

# --- Preconditions ---------------------------------------------------------------
if (-not (Test-Path $ModulePath)) { throw "backup module not found: $ModulePath" }
if (-not (Test-Path $WinTar)) { throw "Windows tar.exe not found: $WinTar" }
$DestRoot = [System.IO.Path]::GetFullPath($DestRoot)
if (Test-InsideGitWorkTree $DestRoot) { throw "DestRoot is inside a Git working tree: $DestRoot" }

if ($LocalRehearsal) {
    if (-not $RehearsalSourceDir) { throw "-LocalRehearsal needs -RehearsalSourceDir (a Git Bash path)" }
    if (-not (Test-Path $BashExe)) { throw "Git Bash not found: $BashExe" }
    $SourceDir = $RehearsalSourceDir
    $RemotePython = "python"
} else {
    $status = Invoke-Native { & railway status --json } | Out-String | ConvertFrom-Json
    $env0 = $status.environments.edges | Where-Object { $_.node.name -eq "production" }
    $svc = $env0.node.serviceInstances.edges | Where-Object { $_.node.serviceName -eq $Service }
    if (-not $svc) { throw "service $Service not found in the linked Railway project" }
    $state = $svc.node.latestDeployment.status
    if ($state -eq "SLEEPING") {
        throw "service is SLEEPING; wait for a natural wake window (this script never wakes it)"
    }
    if ($state -ne "SUCCESS" -or @($svc.node.activeDeployments).Count -lt 1) {
        throw "latest deployment is $state with no healthy active deployment; not touching production"
    }
    Write-Host "Railway deployment status: $state"
    $SourceDir = "/data"
    $RemotePython = "python3"
}

$TS = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$RemoteDir = "/tmp/fb-$TS"
if ($RemoteDir -notmatch '^/tmp/fb-\d{8}T\d{6}Z$') { throw "unexpected remote dir $RemoteDir" }
$Out = Join-Path $DestRoot $TS
New-Item -ItemType Directory -Path $Out | Out-Null
# Owner-only ACL on the new backup directory (auth DB holds password hashes and sessions).
Invoke-Native { & icacls $Out /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" } | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed on $Out" }

# --- Module transfer --------------------------------------------------------------
# gzip+base64, sent in chunks: some launchers truncate command lines at about 8 KB.
# The chunks go to /tmp/fb-<TS>.mod (outside the backup dir) and are removed in step 4.
$gzBuffer = New-Object System.IO.MemoryStream
$gz = New-Object System.IO.Compression.GZipStream($gzBuffer, [System.IO.Compression.CompressionMode]::Compress)
$moduleBytes = [System.IO.File]::ReadAllBytes($ModulePath)
$gz.Write($moduleBytes, 0, $moduleBytes.Length); $gz.Dispose()
$b64Module = [Convert]::ToBase64String($gzBuffer.ToArray())
$RemoteModule = "/tmp/fb-$TS.mod"
$ChunkSize = 6000
for ($i = 0; $i -lt $b64Module.Length; $i += $ChunkSize) {
    $chunk = $b64Module.Substring($i, [Math]::Min($ChunkSize, $b64Module.Length - $i))
    $redirect = if ($i -eq 0) { ">" } else { ">>" }
    $rc = Invoke-Remote "echo $chunk $redirect $RemoteModule" (Join-Path $Out "module.stdout.txt") (Join-Path $Out "module.stderr.txt")
    if ($rc -ne 0) { throw "module transfer failed (exit $rc)" }
}
$RunModule = "base64 -d $RemoteModule | gunzip | $RemotePython -"
$remoteManifest = Join-Path $Out "container-manifest.json"

# --- 0. Inventory and capacity preflight (read-only) -------------------------------
$inventoryPath = Join-Path $Out "inventory.json"
$rc = Invoke-Remote "$RunModule inventory --source-dir $SourceDir --dest-dir $RemoteDir" $inventoryPath (Join-Path $Out "step0.stderr.txt")
$inventory = Get-Content $inventoryPath -Raw | ConvertFrom-Json
if ($rc -ne 0 -or @($inventory.unknown).Count -gt 0) {
    $null = Invoke-Remote "rm -f $RemoteModule" (Join-Path $Out "step0b.stdout.txt") (Join-Path $Out "step0b.stderr.txt")
}
if ($rc -ne 0) { throw "preflight failed (exit $rc): $($inventory.capacity.error)" }
if (@($inventory.unknown).Count -gt 0) {
    throw "unrecognised SQLite files in ${SourceDir}: $($inventory.unknown -join ', '). Add them to the inventory before backing up."
}
Write-Host ("0/5 inventory OK: {0} databases, {1} source bytes, {2} free in /tmp" -f `
    @($inventory.files.PSObject.Properties).Count, $inventory.capacity.source_bytes, $inventory.capacity.free_bytes)

# --- 1. Online backup inside the container ----------------------------------------
$rc = Invoke-Remote "$RunModule backup --source-dir $SourceDir --dest-dir $RemoteDir" $remoteManifest (Join-Path $Out "step1.stderr.txt")
if ($rc -ne 0) { throw "remote backup failed (exit $rc); see step1.stderr.txt. Remote dir: $RemoteDir" }
Write-Host "1/5 container backup OK -> $RemoteDir"

# --- 2. Stream it out ---------------------------------------------------------------
$b64Path = Join-Path $Out "fb-$TS.tgz.b64"
$rc = Invoke-Remote "tar -C /tmp -czf - fb-$TS | base64 -w0" $b64Path (Join-Path $Out "step2.stderr.txt")
if ($rc -ne 0) { throw "remote tar stream failed (exit $rc); remote copy kept at $RemoteDir" }
$tgz = Join-Path $Out "fb-$TS.tgz"
ConvertFrom-Base64File $b64Path $tgz
Invoke-Native { & $WinTar -xzf $tgz -C $Out }
if ($LASTEXITCODE -ne 0) { throw "tar extract failed; remote copy kept at $RemoteDir" }
Remove-Item $b64Path
Write-Host "2/5 transferred and extracted"

# --- 3. Local verification --------------------------------------------------------
$BackupDir = Join-Path $Out "fb-$TS"
$env:PYTHONPATH = Join-Path $RepoRoot "code\src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$verifyJson = Invoke-Native { & $Python -m sports_ai.diagnostics.football_sqlite_backup verify --backup-dir $BackupDir } | Out-String
$verifyRc = $LASTEXITCODE
$verify = $verifyJson | ConvertFrom-Json
$manifest = Get-Content (Join-Path $BackupDir "manifest.json") -Raw | ConvertFrom-Json
$containerManifest = Get-Content $remoteManifest -Raw | ConvertFrom-Json

$problems = New-Object System.Collections.Generic.List[string]
if ($verifyRc -ne 0 -or -not $verify.ok) { $problems.Add("verify: $($verify.problems -join '; ')") }
if (-not $manifest.all_integrity_ok) { $problems.Add("manifest: all_integrity_ok=false") }
if (@($manifest.missing_required).Count -gt 0) { $problems.Add("missing: $($manifest.missing_required -join ',')") }
foreach ($line in Get-Content (Join-Path $BackupDir "SHA256SUMS")) {
    $parts = $line -split "  ", 2
    $hash = (Get-FileHash -Algorithm SHA256 (Join-Path $BackupDir $parts[1])).Hash.ToLower()
    if ($hash -ne $parts[0]) { $problems.Add("$($parts[1]): SHA256SUMS mismatch") }
}
if (($containerManifest | ConvertTo-Json -Depth 10 -Compress) -ne ($manifest | ConvertTo-Json -Depth 10 -Compress)) {
    $problems.Add("container manifest differs from transferred manifest")
}
$evidence = [ordered]@{}
foreach ($db in $manifest.databases.PSObject.Properties) {
    $hash = (Get-FileHash -Algorithm SHA256 (Join-Path $BackupDir $db.Name)).Hash.ToLower()
    if ($hash -ne $db.Value.sha256) { $problems.Add("$($db.Name): Get-FileHash mismatch") }
    $counts = [ordered]@{}
    if ($ReconciledTables.ContainsKey($db.Name)) {
        foreach ($t in $ReconciledTables[$db.Name]) { $counts[$t] = $db.Value.row_counts.$t }
    }
    $evidence[$db.Name] = [ordered]@{
        integrity_check = $db.Value.integrity_check; sha256 = $hash; bytes = $db.Value.bytes
        copied_at = $db.Value.copied_at; reconciled_counts = $counts
    }
}
if ($problems.Count -gt 0) {
    throw ("local verification FAILED; remote copy kept at ${RemoteDir}: " + ($problems -join " | "))
}
Write-Host "3/5 local verify OK (sha256, integrity_check, row counts, manifest match)"

# --- 4. Remove the container copy -------------------------------------------------
if (-not $KeepRemoteCopy) {
    $rc = Invoke-Remote "rm -rf $RemoteDir $RemoteModule" (Join-Path $Out "step4.stdout.txt") (Join-Path $Out "step4.stderr.txt")
    if ($rc -ne 0) { throw "remote cleanup failed (exit $rc) for $RemoteDir" }
    $rc = Invoke-Remote "test ! -e $RemoteDir -a ! -e $RemoteModule" (Join-Path $Out "step4b.stdout.txt") (Join-Path $Out "step4b.stderr.txt")
    if ($rc -ne 0) { throw "remote copy still present: $RemoteDir" }
    Write-Host "4/5 container copy removed"
}

# --- 5. Non-sensitive evidence ----------------------------------------------------
$summary = [ordered]@{
    ts = $TS; started_at = $manifest.started_at; completed_at = $manifest.completed_at
    verify_ok = $true; all_integrity_ok = $manifest.all_integrity_ok
    source_bytes = $manifest.capacity.source_bytes; tmp_free_bytes = $manifest.capacity.free_bytes
    tgz_sha256 = (Get-FileHash -Algorithm SHA256 $tgz).Hash.ToLower()
    databases = $evidence
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $Out "evidence.json")
Write-Host "5/5 evidence.json written"
$summary | ConvertTo-Json -Depth 6
