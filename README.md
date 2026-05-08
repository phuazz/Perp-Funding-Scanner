# Binance USD-M funding scanner

Scans the full Binance USD-M perpetual universe (~500 symbols) for paid-direction trade opportunities by combining funding extremity with multi-timeframe trend confirmation.

Live dashboard: `https://phuazz.github.io/Perp-Funding-Scanner/`

## What it shows

- **Headline setups**: stretched funding + trend confirmation = highest conviction trades
- **Funding-vs-price quadrant chart**: coloured by trend status, filterable by category
- **Stretched longs / crowded shorts**: full ranked tables with sparklines
- **Regime shifts**: 7d funding deviation from 30d baseline

## Signal logic

**Funding extremity**
- Stretched long: 30d annualised funding > +30%/yr
- Crowded short: 30d annualised funding < −15%/yr

**Trend score (range −6 to +6)** — sum of 7 sub-signals on 10/20/50d MA stack:
1. Price vs 10d MA
2. Price vs 20d MA
3. Price vs 50d MA
4. 10d MA slope (5d ROC)
5. 20d MA slope (5d ROC)
6. 10d MA vs 20d MA cross
7. 20d MA vs 50d MA cross

Each signal contributes −1 / 0 / +1. Score clamped to [−6, +6].

**Visual mapping**: score ≤ −3 = down (red), −2 to +2 = flat (grey), ≥ +3 = up (green).

**Headline setup criteria**:
- Short: funding > +30%/yr AND trend score ≤ −3 (stretched long rolling over)
- Long: funding < −15%/yr AND trend score ≥ +3 (crowded short reclaiming)

## Architecture

```
template.html                  source of truth, fetch fallback for dev
scripts/build.py               scans Binance, writes JSON, builds docs/index.html
scripts/local_refresh.ps1      Task Scheduler wrapper: scan + commit + push
data/scan.json                 latest scan output (committed by local cron)
docs/index.html                GitHub Pages output (template + injected data)
logs/refresh.log               local refresh log (gitignored)
.github/workflows/refresh.yml  manual-dispatch only (geo-block on US runners)
```

## Setup

### 1. Create the repo
```bash
gh repo create Perp-Funding-Scanner --public --source=. --remote=origin --push
```

### 2. Enable GitHub Pages
Settings → Pages → Source: `Deploy from a branch` → Branch: `main` → Folder: `/docs`.
First deploy will work after the first successful Action run.

### 3. Register the local refresh
GitHub Actions runners get HTTP 451 from Binance fapi (US geo-block), so refresh runs from the local Windows machine via Task Scheduler:

```powershell
$taskName = 'PerpFundingScanner-Refresh'
$scriptPath = 'C:\dev\Perp-Funding-Scanner\scripts\local_refresh.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""

$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At '08:00'),
    (New-ScheduledTaskTrigger -Daily -At '20:00')
)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $triggers -Settings $settings -Principal $principal `
    -Description 'Twice-daily Perp-Funding-Scanner refresh' -Force
```

Verify with `Get-ScheduledTask -TaskName 'PerpFundingScanner-Refresh'`. Watch live with `Get-Content logs\refresh.log -Tail 30 -Wait`.

## Local development

```bash
pip install -r requirements.txt
python scripts/build.py     # ~5–8 min, hits Binance fapi
npx serve .                 # then open http://localhost:3000/template.html
```

`template.html` will fetch `data/scan.json` when opened directly — no rebuild needed for design tweaks.

## Refresh schedule

Twice daily at 08:00 SGT and 20:00 SGT via local Task Scheduler. `StartWhenAvailable` is set, so a missed run (laptop asleep) catches up at next wake within the daily window.

If a scan fails (Binance rate limit, schema change), `local_refresh.ps1` keeps the previous `scan.json`, logs the error to `logs/refresh.log`, and exits non-zero — Task Scheduler will show the failure in its history. The dashboard shows a stale-data banner if the data is more than 18 hours old.

## Known limitations

- Binance fapi can hit rate limits or geo-blocks from GitHub Action runners — the script retries with backoff but persistent failures will need manual investigation.
- Trend signals lag by design — the dashboard is a screen, not a substitute for judgement.
- Funding alone is not a thesis. Pair with an independent fundamental view.
- TradFi perp delisting risk: assume a 2–3 day forced-unwind window when sizing.
- The sparkline is per-settlement funding (not annualised) — a visual aid for trajectory, not an absolute scale.

## Customisation

- Add or remove TradFi tickers: edit `TRADFI_EQUITY_BASES` and `TRADFI_COMMODITY_BASES` in `scripts/build.py`.
- Change thresholds: edit `stretched_long_threshold` / `crowded_short_threshold` / `trend_confirm_score` near the bottom of `build_payload()` in `scripts/build.py`. Update the legend text in `template.html` to match.
- Change refresh schedule: re-run the `Register-ScheduledTask` block above with new `-At` times.
