#requires -Version 5.1
# Local twice-daily refresh, invoked by Windows Task Scheduler.
# Runs the Binance scan, commits and pushes data/scan.json + docs/index.html if changed.
# Pure ASCII: PowerShell 5.1 reads non-BOM files as ANSI 1252 and mangles em-dashes.

$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
$LogFile = Join-Path $LogDir 'refresh.log'
# Touched only when a run completes healthily, including the healthy no-change
# case. The git heartbeat cannot serve as the liveness signal on its own: it
# moves only when the data changes, so a run that fires and fails writes
# nothing and looks identical to a run with nothing to write. Watched by
# fleet_watch as 'perp-funding local run'.
$SuccessFile = Join-Path $LogDir 'last_success.txt'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    Add-Content -Path $LogFile -Value "[$ts] [$Level] $Message" -Encoding utf8
}

function Set-SuccessSentinel {
    param([string]$Outcome)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    Set-Content -Path $SuccessFile -Value "$stamp $Outcome" -Encoding utf8
}

# Wait for Binance fapi to be genuinely reachable before scanning.
# StartWhenAvailable is set on the scheduled task, so a run missed while the
# laptop slept fires at wake -- often before the network stack is up. Every
# failure in the fortnight to 2026-08-12 was a DNS resolution error on exactly
# such a catch-up start (08:15, 09:00 and 21:33, against a normal 08:03/20:03),
# and each one discarded a full half-day of data for a condition that clears
# itself within seconds. DNS alone is not sufficient evidence: a resolver can
# answer from cache while the route is still down, so the probe follows the
# lookup with a real request to the API's own ping endpoint.
function Wait-ForApi {
    param(
        [string]$HostName = 'fapi.binance.com',
        [string]$ProbeUrl = 'https://fapi.binance.com/fapi/v1/ping',
        [int]$MaxAttempts = 20,
        [int]$DelaySeconds = 15
    )
    # PowerShell 5.1 does not negotiate TLS 1.2 by default on every host.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            [void][System.Net.Dns]::GetHostEntry($HostName)
            $resp = Invoke-WebRequest -Uri $ProbeUrl -UseBasicParsing -TimeoutSec 10
            if ($resp.StatusCode -eq 200) {
                Write-Log "api probe ok on attempt $i of $MaxAttempts"
                return $true
            }
            Write-Log "api probe returned HTTP $($resp.StatusCode) (attempt $i of $MaxAttempts)" 'WARN'
        }
        catch {
            Write-Log "api not reachable yet (attempt $i of $MaxAttempts): $($_.Exception.Message)" 'WARN'
        }
        if ($i -lt $MaxAttempts) { Start-Sleep -Seconds $DelaySeconds }
    }
    return $false
}

# Hardcoded Python path -- Task Scheduler -NoProfile shell does not reliably
# resolve `python` or `py` (Windows Store App Execution Aliases). Update this
# line if Python is reinstalled to a different location.
$PythonExe = 'C:\Users\phuaz\AppData\Local\Python\pythoncore-3.14-64\python.exe'

try {
    Set-Location $RepoRoot
    Write-Log "=== refresh start (repo: $RepoRoot, user: $env:USERNAME) ==="

    if (-not (Test-Path $PythonExe)) {
        Write-Log "python not found at $PythonExe -- aborting" 'ERROR'
        exit 1
    }
    Write-Log "python: $PythonExe"

    # Exit code 2 is reserved for "never got a network", to keep a connectivity
    # stall distinguishable from a genuine scan failure in the task history.
    if (-not (Wait-ForApi)) {
        Write-Log 'api unreachable after all attempts -- keeping previous data, no commit' 'ERROR'
        exit 2
    }

    $scriptPath = Join-Path $RepoRoot 'scripts\build.py'
    Write-Log "running: $PythonExe $scriptPath"
    $scanOutput = & $PythonExe $scriptPath 2>&1
    $scanExit = $LASTEXITCODE
    foreach ($line in $scanOutput) {
        Write-Log "scan> $line" 'SCAN'
    }
    Write-Log "scan exit code: $scanExit"

    if ($scanExit -ne 0) {
        Write-Log 'scan failed -- keeping previous data, no commit' 'ERROR'
        exit $scanExit
    }

    & git add data docs 2>&1 | Out-Null
    & git diff --cached --quiet
    $diffCode = $LASTEXITCODE
    if ($diffCode -eq 0) {
        # A scan that ran cleanly and found nothing new is a healthy run, so it
        # marks the sentinel. Only the pipeline's health is being asserted here,
        # not that the data moved.
        Set-SuccessSentinel 'ok (no change)'
        Write-Log 'no staged changes, done'
        exit 0
    }

    $commitMsg = 'auto: refresh ' + (Get-Date -Format 'yyyy-MM-dd HH:mm') + ' SGT'
    Write-Log "committing: $commitMsg"
    $commitOut = & git commit -m $commitMsg 2>&1
    $commitExit = $LASTEXITCODE
    foreach ($line in $commitOut) { Write-Log "git> $line" 'GIT' }
    if ($commitExit -ne 0) {
        Write-Log "commit failed with code $commitExit" 'ERROR'
        exit $commitExit
    }

    Write-Log 'pushing to origin/main'
    $pushOut = & git push origin main 2>&1
    $pushExit = $LASTEXITCODE
    foreach ($line in $pushOut) { Write-Log "git> $line" 'GIT' }
    if ($pushExit -ne 0) {
        Write-Log "push failed with code $pushExit, commit is local only" 'ERROR'
        exit $pushExit
    }

    Set-SuccessSentinel 'ok (committed and pushed)'
    Write-Log '=== refresh complete ==='
    exit 0
}
catch {
    Write-Log "unhandled exception: $($_.Exception.Message)" 'FATAL'
    Write-Log "at line: $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())" 'FATAL'
    exit 99
}
