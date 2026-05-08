#requires -Version 5.1
# Local twice-daily refresh — invoked by Windows Task Scheduler.
# Runs the Binance scan, commits and pushes data/scan.json + docs/index.html if changed.

$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
$LogFile = Join-Path $LogDir 'refresh.log'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    "[$timestamp] [$Level] $Message" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

Set-Location $RepoRoot
Write-Log "=== refresh start (repo: $RepoRoot) ==="

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Log "python not on PATH — aborting" 'ERROR'
    exit 1
}
Write-Log "python: $($pythonCmd.Source)"

$scriptPath = Join-Path $RepoRoot 'scripts\build.py'
$scanOutput = & $pythonCmd.Source $scriptPath 2>&1
$scanExit = $LASTEXITCODE
$scanOutput | ForEach-Object { Write-Log "scan: $_" 'SCAN' }

if ($scanExit -ne 0) {
    Write-Log "scan failed (exit $scanExit) — keeping previous data, no commit" 'ERROR'
    exit $scanExit
}

& git add data docs 2>&1 | Out-Null
& git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Log "no changes to commit — done"
    exit 0
}

$commitMsg = 'auto: refresh ' + (Get-Date -Format 'yyyy-MM-dd HH:mm') + ' SGT'
& git commit -m $commitMsg 2>&1 | ForEach-Object { Write-Log "git: $_" 'GIT' }
if ($LASTEXITCODE -ne 0) {
    Write-Log "commit failed (exit $LASTEXITCODE)" 'ERROR'
    exit $LASTEXITCODE
}

& git push origin main 2>&1 | ForEach-Object { Write-Log "push: $_" 'GIT' }
if ($LASTEXITCODE -ne 0) {
    Write-Log "push failed (exit $LASTEXITCODE) — commit is local only" 'ERROR'
    exit $LASTEXITCODE
}

Write-Log "=== refresh complete ==="
exit 0
