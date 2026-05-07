<#
.SYNOPSIS
    Push local backend/.env to the droplet, with APP_ENV flipped to production,
    then restart the iris-backend service.

.DESCRIPTION
    Reads backend\.env from the project root, transforms APP_ENV=development
    -> APP_ENV=production in a temp copy, uploads to the droplet via scp,
    sets 600 permissions on the remote file, restarts the systemd service,
    and tails recent logs for confirmation.

    Local .env is never modified. The transformed temp file is deleted after
    the upload completes.

.PARAMETER DropletHost
    SSH target. Default: iris@64.23.167.164.

.PARAMETER RemotePath
    Path of .env on the droplet. Default: /opt/iris-backend/backend/.env.

.EXAMPLE
    .\deploy\push-env.ps1
    Pushes ..\backend\.env to the default droplet.

.EXAMPLE
    .\deploy\push-env.ps1 -DropletHost iris@example.com
    Pushes to a different host.
#>
param(
    [string]$DropletHost = "iris@64.23.167.164",
    [string]$RemotePath = "/opt/iris-backend/backend/.env"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$localEnv = Join-Path $repoRoot "backend\.env"

if (-not (Test-Path $localEnv)) {
    Write-Error "Local .env not found at $localEnv"
    exit 1
}

# Transform APP_ENV in a temp file. -replace uses regex; the [^\r\n]* tail
# matches whatever value's currently there so the swap is always to
# 'production' regardless of starting state.
$content = Get-Content -Path $localEnv -Raw
$transformed = $content -replace '(?m)^APP_ENV=[^\r\n]*$', 'APP_ENV=production'

# UTF-8 without BOM (pydantic-settings handles either, but no BOM is cleaner
# and matches what the file would look like if edited on Linux).
$tempFile = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tempFile, $transformed, (New-Object System.Text.UTF8Encoding $false))

try {
    Write-Host "Uploading $localEnv -> $DropletHost`:$RemotePath"
    & scp $tempFile "$DropletHost`:$RemotePath"
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed with exit code $LASTEXITCODE"
    }

    Write-Host "Setting permissions (chmod 600)"
    & ssh $DropletHost "chmod 600 $RemotePath"
    if ($LASTEXITCODE -ne 0) {
        throw "remote chmod failed"
    }

    Write-Host "Restarting iris-backend"
    & ssh $DropletHost "sudo systemctl restart iris-backend && sleep 1 && sudo systemctl is-active iris-backend"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Service may not have started cleanly. Tailing logs:"
        & ssh $DropletHost "sudo journalctl -u iris-backend -n 30 --no-pager"
        throw "iris-backend not active after restart"
    }

    Write-Host ""
    Write-Host "Done. Verify:"
    Write-Host "  curl https://iris.lighthouseinn-florence.com/health"
} finally {
    if (Test-Path $tempFile) {
        Remove-Item $tempFile -Force
    }
}
