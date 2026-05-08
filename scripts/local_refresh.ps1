#requires -Version 5.1
# Local twice-daily refresh, invoked by Windows Task Scheduler.
# Runs the Binance scan, commits and pushes data/scan.json + docs/index.html if changed.
# Pure ASCII: PowerShell 5.1 reads non-BOM files as ANSI 1252 and mangles em-dashes.

$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
$LogFile = Join-Path $LogDir 'refresh.log'

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
    Add-Content -Path $LogFile -Value "[$ts] [$Level] $Message" -Encoding utf8
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

    Write-Log '=== refresh complete ==='
    exit 0
}
catch {
    Write-Log "unhandled exception: $($_.Exception.Message)" 'FATAL'
    Write-Log "at line: $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())" 'FATAL'
    exit 99
}
