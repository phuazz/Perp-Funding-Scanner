# Binance USD-M funding scanner

Scans the full Binance USD-M perpetual universe (~700 symbols) for paid-direction trade opportunities by combining funding extremity with multi-timeframe trend confirmation, and compares funding on the same underlying against Hyperliquid (core perps + trade.xyz HIP-3 markets) over matched trailing windows.

Live dashboard: `https://phuazz.github.io/Perp-Funding-Scanner/`

## What it shows

- **Headline setups**: stretched funding + trend confirmation = highest conviction trades
- **Funding-vs-price quadrant chart**: coloured by trend status, filterable by category
- **Stretched longs / crowded shorts**: full ranked tables with sparklines
- **Regime shifts**: 7d funding deviation from 30d baseline
- **Cross-venue funding (Hyperliquid, added 2026-08-16)**: same underlying on both venues, 30d/7d trailing windows computed with the identical annualisation on each side, plus an index/sector watchlist (xyz:SP500, XYZ100, SMH, XLE, SOXL, URNM, GOLD, EWY, EWJ)

## Cross-venue methodology and guards (2026-08-16)

Instantaneous funding snapshots are not comparable across venues — the day the
extension was built, xyz:SKHX printed −24%/yr spot against a +33%/yr 30d mean.
Both venues therefore publish the same construction: mean per-settlement rate
in the trailing window × settlements/year × 100, longs pay when positive,
windows 30d and 7d anchored at the scan instant. Spread = Binance −
Hyperliquid; positive means longs pay more on Binance.

Pairing rules (`scripts/hyperliquid_funding.py`):

- **Routing**: TradFi rows match only trade.xyz markets; crypto rows match only
  the Hyperliquid core book (a name collision must not cross venues).
- **exact / alias** (e.g. `SKHYNIXUSDT` ↔ `xyz:SKHX`, `XAUUSDT` ↔ `xyz:GOLD`):
  published only if the two venues' prices agree within ±5% (identity gate;
  rejections are listed in `scan.json` and counted on the dashboard).
- **proxy** (`SPY`/`SPX` ↔ `xyz:SP500`, `QQQ`/`NDX` ↔ `xyz:XYZ100`): related but
  not identical underlyings — funding is scale-free so the comparison stands,
  prices do not; labelled on the dashboard.
- **k-prefix**: Binance `1000PEPE` ↔ Hyperliquid `kPEPE` (both quote 1000 units).

Guard layers, per the vault's unattended-agent rule (fail-open but loud):

1. **Identity gate** as above — wrong-instrument pairs are dropped, visibly.
2. **Completeness gate**: a market whose fetched window is materially short
   (span < 25d or records < 60% of the detected cadence's expectation) is
   flagged `partial` and its spreads are suppressed.
3. **Sentinel cross-check**: Binance 30d funding for BTC/ETH/SOL is recomputed
   with an independent method (sum over actual span × 365) and compared to the
   published mean × interval value; disagreement beyond max(1.5pp, 12%)
   degrades the block — hl columns stay, spreads are suppressed, banner shown.
4. **Fail-open**: any Hyperliquid failure leaves the Binance scan untouched and
   sets `hyperliquid.status` to `unavailable`, which the dashboard banners.

Outputs: `hl_*` and `spread_ann_*` fields on matched rows in `data/scan.json`,
a `hyperliquid` block (status, sentinel result, rejected pairs, watchlist), and
`data/hl_funding_daily.json` (mean rate per UTC day per market, for spread-
persistence studies). Unit tests: `pytest tests/` (16 tests, including the
mandatory month- and year-boundary date cases). Runtime cost: roughly +2–4
minutes per scan at a polite 0.35s spacing; no API key on either venue.

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
template.html                    source of truth, fetch fallback for dev
scripts/build.py                 scans Binance, writes JSON, builds docs/index.html
scripts/hyperliquid_funding.py   cross-venue enrichment (pairing, gates, sentinel)
scripts/local_refresh.ps1        Task Scheduler wrapper: scan + commit + push
tests/                           unit tests for the cross-venue module (pytest)
data/scan.json                   latest scan output (committed by local cron)
data/hl_funding_daily.json       Hyperliquid daily-mean funding per market (30d)
docs/index.html                  GitHub Pages output (template + injected data)
logs/refresh.log                 local refresh log (gitignored)
.github/workflows/refresh.yml    manual-dispatch only (geo-block on US runners)
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

That catch-up behaviour has a cost: a run firing at wake often finds the network stack not yet up. Every failure in the fortnight to 2026-08-12 was a DNS resolution error on such a delayed start (08:15, 09:00 and 21:33, against a normal 08:03 / 20:03), each discarding half a day of data for a condition that clears itself in seconds. `local_refresh.ps1` therefore waits for the API before scanning: it resolves the host and then calls the ping endpoint — a resolver can answer from cache while the route is still down — retrying for up to five minutes. The scheduled task additionally carries `RestartCount = 3` at ten-minute intervals, which covers the script failing to launch at all.

If a scan fails (Binance rate limit, schema change), `local_refresh.ps1` keeps the previous `scan.json`, logs the error to `logs/refresh.log`, and exits non-zero — Task Scheduler will show the failure in its history. Exit code 2 is reserved for a connectivity stall so it stays distinguishable from a genuine scan failure. The dashboard shows a stale-data banner if the data is more than 18 hours old.

**Liveness signal.** A healthy run touches `logs/last_success.txt`, including the healthy no-change case. This exists because the commit history cannot serve as the heartbeat on its own: it moves only when the data changes, so a run that fires and fails writes nothing and looks exactly like a run with nothing to write. That blind spot is normally closed by watching the CI run, which is not available here since the GitHub Actions half was retired in May (see above). The vault's `fleet_watch` reads the sentinel as `perp-funding local run` with a 24-hour tolerance, which absorbs one missed run of the twice-daily pair. The file is machine-local and gitignored.

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
