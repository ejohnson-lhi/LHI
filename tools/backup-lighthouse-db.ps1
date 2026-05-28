# Pull the iris-backend SQLite database (and its WAL sidecars, if present)
# from the droplet to a timestamped local file. Keeps the last N days of
# backups, deletes older.
#
# Usage:
#   .\backup-lighthouse-db.ps1               # one-shot, default settings
#   .\backup-lighthouse-db.ps1 -RetentionDays 30
#
# Schedule with Windows Task Scheduler (daily at 3am):
#   schtasks /Create /TN "Lighthouse DB Backup" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"D:\2-Work\ComputerSoftwareDevelopment\AI Reservation Agent\tools\backup-lighthouse-db.ps1`"" /SC DAILY /ST 03:00
#
# Tested with: OpenSSH on Windows (built-in since Windows 10), iris user with
# passwordless sudo / key-based ssh login (same setup deploy.bat uses).

[CmdletBinding()]
param(
    [string]$Remote = "iris@64.23.167.164",
    [string]$RemotePath = "/opt/iris-backend/backend/data/lighthouse.db",
    [string]$LocalDir = "D:\2-Work\ComputerSoftwareDevelopment\AI Reservation Agent\backups\lighthouse-db",
    [int]$RetentionDays = 14
)

$ErrorActionPreference = "Stop"

# Resolve scp from the same OpenSSH install deploy.bat uses (Sysnative
# matters on 32-bit cmd hosts; harmless otherwise).
$Scp = $null
foreach ($candidate in @(
    "$env:SystemRoot\Sysnative\OpenSSH\scp.exe",
    "$env:SystemRoot\System32\OpenSSH\scp.exe"
)) {
    if (Test-Path $candidate) { $Scp = $candidate; break }
}
if (-not $Scp) { $Scp = "scp" }   # fall back to PATH

if (-not (Test-Path $LocalDir)) {
    New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null
}

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$mainDest = Join-Path $LocalDir "lighthouse-$timestamp.db"

Write-Host "[backup] $timestamp -> $mainDest"

# Pull lighthouse.db. WAL/SHM sidecars only exist while the service is
# running with WAL journaling; copy them if present so the snapshot is
# consistent. The shell glob is interpreted on the droplet side.
& $Scp "${Remote}:${RemotePath}" $mainDest
if ($LASTEXITCODE -ne 0) {
    Write-Error "[backup] FAILED: scp returned exit code $LASTEXITCODE"
    exit 1
}

# WAL/SHM sidecars only exist while the service is running with WAL
# journaling and only between checkpoints. Probe their existence over
# ssh first so we don't trigger NativeCommandError on a missing file
# (PowerShell promotes scp's stderr to a script-fatal error under
# $ErrorActionPreference="Stop"; redirecting 2>$null doesn't help).
$Ssh = $null
foreach ($candidate in @(
    "$env:SystemRoot\Sysnative\OpenSSH\ssh.exe",
    "$env:SystemRoot\System32\OpenSSH\ssh.exe"
)) {
    if (Test-Path $candidate) { $Ssh = $candidate; break }
}
if (-not $Ssh) { $Ssh = "ssh" }

foreach ($suffix in @("-wal", "-shm")) {
    $remoteSidecar = "$RemotePath$suffix"
    $probe = & $Ssh $Remote "test -f '$remoteSidecar' && echo yes || true"
    if ($probe -ne "yes") {
        continue
    }
    $localSidecar = Join-Path $LocalDir "lighthouse-$timestamp.db$suffix"
    & $Scp "${Remote}:${remoteSidecar}" $localSidecar
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[backup]   + sidecar $suffix copied"
    }
}

# Retention sweep: delete backups older than $RetentionDays. SQLite is
# tiny (single-digit MB even after months of consent rows), so a long
# window is cheap.
$cutoff = (Get-Date).AddDays(-$RetentionDays)
$oldBackups = Get-ChildItem -Path $LocalDir -Filter "lighthouse-*.db*" -File |
    Where-Object { $_.LastWriteTime -lt $cutoff }
foreach ($f in $oldBackups) {
    Write-Host "[backup] purging $($f.Name) (last write $($f.LastWriteTime))"
    Remove-Item $f.FullName -Force
}

# Report current backup set size for situational awareness.
$count = (Get-ChildItem -Path $LocalDir -Filter "lighthouse-*.db" -File).Count
$totalMB = [math]::Round(((Get-ChildItem -Path $LocalDir -Filter "lighthouse-*.db*" -File |
    Measure-Object -Property Length -Sum).Sum / 1MB), 2)
Write-Host "[backup] OK - $count snapshots in $LocalDir ($totalMB MB total)"
