"""
Binance USD-M Funding Scanner — Build Script
=============================================
Pulls full Binance USD-M perp universe, computes for each symbol:
  - 7d / 30d annualised funding rate
  - 30d cumulative funding cost
  - Trend score (-6 to +6) from 6 sub-signals on 10/20/50 MA stack
  - Price change over 30d, current price, 24h volume

Outputs:
  data/scan.json          — for the dashboard
  docs/index.html         — template with embedded data (built copy)

Run locally for testing:
    python scripts/build.py

Run via GitHub Action: configured in .github/workflows/refresh.yml

Dependencies:
    requests, pandas
"""

import os
import sys
import json
import time
import math
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; funding-scanner/1.0)"})

LOOKBACK_DAYS = 60          # history for funding + price
MIN_VOLUME_USDT_M = 5.0     # liquidity filter for actionable list
RATE_SLEEP = 0.08           # politeness between calls

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
TEMPLATE = REPO_ROOT / "template.html"

# ---------------------------------------------------------------------------
# Category maps
# ---------------------------------------------------------------------------

TRADFI_COMMODITY_BASES = {
    "XAG", "XAU", "XPT", "XPD",
    "OIL", "WTI", "BRENT", "CL", "BZ", "NATGAS", "NGAS",
    "COPPER", "URANIUM", "CORN", "WHEAT", "SOY",
    "COCOA", "COFFEE", "SUGAR", "COTTON",
}

TRADFI_EQUITY_BASES = {
    "NVDA", "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "TSLA",
    "MSTR", "COIN", "HOOD", "CRCL", "PAYP", "AMD", "INTC", "MU", "PLTR", "SMCI", "SNDK",
    "AVGO", "ORCL", "ADBE", "CRM", "NFLX", "DIS",
    "ASML", "TSM", "SAP", "LRCX", "KLAC", "AMAT", "MRVL", "QCOM", "TXN", "CSCO",
    "BA", "GE", "CAT", "DE", "XOM", "CVX",
    "UNH", "JPM", "GS", "BAC", "WFC", "C", "MS",
    "PFE", "MRNA", "LLY", "ABBV", "JNJ",
    "UBER", "LYFT", "SHOP", "SQ", "PYPL", "SOFI",
    "NIO", "LI", "XPEV", "BABA", "BIDU", "PDD", "JD",
    "BILI", "TME", "NTES", "TCOM",
    "SPX", "NDX", "SPY", "QQQ", "IWM", "DIA",
    "GLD", "SLV", "TLT", "HYG", "LQD",
    "VIX", "UVXY", "SOXL", "SOXX", "ARKK",
    "XLE", "XLF", "XLK", "EEM", "EFA", "FXI",
    "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "CNH", "DXY",
}

CRYPTO_MAJORS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA",
    "AVAX", "DOGE", "LINK", "LTC", "DOT", "TRX",
    "MATIC", "TON", "SUI", "APT",
}


def categorise(base: str, contract_type: str = "PERPETUAL") -> str:
    if base in TRADFI_COMMODITY_BASES:
        return "TradFi-Commodity"
    if base in TRADFI_EQUITY_BASES:
        return "TradFi-Equity"
    # Defensive: any TRADIFI_PERPETUAL base not in our maintained sets is still
    # TradFi by Binance's own contract type. Default to equity bucket; promote
    # to TRADFI_COMMODITY_BASES manually if it is a commodity.
    if contract_type == "TRADIFI_PERPETUAL":
        return "TradFi-Equity"
    if base in CRYPTO_MAJORS:
        return "Crypto-Major"
    return "Crypto-Alt"


# ---------------------------------------------------------------------------
# Binance API wrappers
# ---------------------------------------------------------------------------

def get_with_retry(url, params=None, timeout=15, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(1 + attempt)
    return None


def get_exchange_info():
    return get_with_retry(f"{BASE}/fapi/v1/exchangeInfo")


def discover_universe():
    info = get_exchange_info()
    out = []
    for s in info["symbols"]:
        ctype = s.get("contractType")
        if ctype not in ("PERPETUAL", "TRADIFI_PERPETUAL"):
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        out.append({
            "symbol": s["symbol"],
            "base": s["baseAsset"],
            "category": categorise(s["baseAsset"], ctype),
        })
    return out


def get_funding_history(symbol, days=LOOKBACK_DAYS):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    out = []
    cursor = start
    while cursor < end:
        try:
            batch = get_with_retry(
                f"{BASE}/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": cursor, "limit": 1000},
            )
        except Exception:
            return out
        if not batch:
            break
        for b in batch:
            out.append((b["fundingTime"], float(b["fundingRate"])))
        last = batch[-1]["fundingTime"]
        if last <= cursor:
            break
        cursor = last + 1
        if len(batch) < 1000:
            break
        time.sleep(RATE_SLEEP)
    return out


def get_24h_tickers_bulk():
    data = get_with_retry(f"{BASE}/fapi/v1/ticker/24hr")
    return {t["symbol"]: t for t in data}


def get_klines_60d(symbol):
    end = int(time.time() * 1000)
    start = end - 65 * 86_400_000
    return get_with_retry(
        f"{BASE}/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "1d",
            "startTime": start,
            "endTime": end,
            "limit": 70,
        },
    )


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def detect_interval_hours(times_ms):
    if len(times_ms) < 3:
        return 8
    gaps = []
    for i in range(len(times_ms) - 1):
        g = (times_ms[i + 1] - times_ms[i]) / 3_600_000
        if 0 < g < 24:
            gaps.append(g)
    if not gaps:
        return 8
    return round(sorted(gaps)[len(gaps) // 2])


def annualise_pct(mean_rate, hours_per_settlement):
    settlements_per_year = 365 * 24 / hours_per_settlement
    return mean_rate * settlements_per_year * 100


def compute_trend_score(closes):
    """
    Returns (score, sub_signals_dict) where score is -6 to +6.
    Sub-signals (each -1, 0, +1):
      1. price vs 10d MA
      2. price vs 20d MA
      3. price vs 50d MA
      4. 10d MA slope (5d ROC)
      5. 20d MA slope (5d ROC)
      6. 10d MA vs 20d MA (cross)
      7. 20d MA vs 50d MA (cross)
    Score is sum of 7 signals, clamped to [-6, +6] for clean visual mapping.
    """
    if closes is None or len(closes) < 55:
        return 0, {}

    s = pd.Series(closes)
    ma10 = s.rolling(10).mean()
    ma20 = s.rolling(20).mean()
    ma50 = s.rolling(50).mean()

    last = s.iloc[-1]
    last_ma10 = ma10.iloc[-1]
    last_ma20 = ma20.iloc[-1]
    last_ma50 = ma50.iloc[-1]

    def sgn(x, eps=0.001):
        if pd.isna(x):
            return 0
        if x > eps:
            return 1
        if x < -eps:
            return -1
        return 0

    sig = {}

    # price vs MAs
    sig["price_vs_ma10"] = sgn((last - last_ma10) / last_ma10) if last_ma10 else 0
    sig["price_vs_ma20"] = sgn((last - last_ma20) / last_ma20) if last_ma20 else 0
    sig["price_vs_ma50"] = sgn((last - last_ma50) / last_ma50) if last_ma50 else 0

    # MA slope (5d ROC)
    if len(ma10.dropna()) >= 6:
        roc10 = (ma10.iloc[-1] / ma10.iloc[-6] - 1)
        sig["ma10_slope"] = sgn(roc10)
    else:
        sig["ma10_slope"] = 0
    if len(ma20.dropna()) >= 6:
        roc20 = (ma20.iloc[-1] / ma20.iloc[-6] - 1)
        sig["ma20_slope"] = sgn(roc20)
    else:
        sig["ma20_slope"] = 0

    # crosses
    if not pd.isna(last_ma10) and not pd.isna(last_ma20):
        sig["ma10_vs_ma20"] = sgn((last_ma10 - last_ma20) / last_ma20)
    else:
        sig["ma10_vs_ma20"] = 0
    if not pd.isna(last_ma20) and not pd.isna(last_ma50):
        sig["ma20_vs_ma50"] = sgn((last_ma20 - last_ma50) / last_ma50)
    else:
        sig["ma20_vs_ma50"] = 0

    raw_score = sum(sig.values())
    # clamp to [-6, +6]
    score = max(-6, min(6, raw_score))

    return score, sig


def trend_label(score):
    if score >= 3:
        return "up"
    if score <= -3:
        return "down"
    return "flat"


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(verbose=True):
    universe = discover_universe()
    tickers = get_24h_tickers_bulk()
    if verbose:
        print(f"Universe: {len(universe)} perpetuals")

    rows = []
    for i, u in enumerate(universe, 1):
        sym = u["symbol"]
        try:
            fh = get_funding_history(sym, days=LOOKBACK_DAYS)
            if len(fh) < 5:
                continue

            df = pd.DataFrame(fh, columns=["ts", "rate"])
            df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df = df.sort_values("time").reset_index(drop=True)
            interval_h = detect_interval_hours(df["ts"].tolist())

            now = pd.Timestamp.now(tz="UTC")
            d7 = df[df["time"] >= now - pd.Timedelta(days=7)]
            d30 = df[df["time"] >= now - pd.Timedelta(days=30)]
            if len(d30) < 3:
                continue

            f30 = annualise_pct(d30["rate"].mean(), interval_h)
            f7 = annualise_pct(d7["rate"].mean() if len(d7) else 0, interval_h)
            f30_cum = d30["rate"].sum() * 100

            # 30d funding history for sparkline (downsample to ~30 points)
            spark_raw = d30["rate"].tolist()
            if len(spark_raw) > 60:
                step = max(1, len(spark_raw) // 30)
                spark = spark_raw[::step][:30]
            else:
                spark = spark_raw

            # Price + volume from bulk ticker
            t = tickers.get(sym, {})
            vol_m = float(t.get("quoteVolume", 0)) / 1e6
            price = float(t.get("lastPrice", 0))
            chg_24h = float(t.get("priceChangePercent", 0))

            # Klines for trend score + 30d price change + realised vol + 7d hi/lo
            score = 0
            sub = {}
            chg_30d = float("nan")
            rv_10d_ann = float("nan")
            px_7d_high = None
            px_7d_low = None
            try:
                klines = get_klines_60d(sym)
                if klines and len(klines) >= 30:
                    closes = [float(k[4]) for k in klines]
                    highs = [float(k[2]) for k in klines]
                    lows = [float(k[3]) for k in klines]
                    score, sub = compute_trend_score(closes)
                    if len(closes) >= 31:
                        chg_30d = (closes[-1] / closes[-31] - 1) * 100
                    if len(closes) >= 11:
                        rets = pd.Series(closes[-11:]).pct_change().dropna()
                        if len(rets) >= 5 and rets.std() > 0:
                            rv_10d_ann = float(rets.std() * (365 ** 0.5) * 100)
                    if len(highs) >= 7:
                        px_7d_high = round(max(highs[-7:]), 6)
                        px_7d_low = round(min(lows[-7:]), 6)
            except Exception:
                pass

            # Vol-normalised funding (Sharpe-of-carry). Cross-sectionally comparable.
            fund_z_30d = round(f30 / rv_10d_ann, 3) if rv_10d_ann and not math.isnan(rv_10d_ann) else None
            fund_z_7d = round(f7 / rv_10d_ann, 3) if rv_10d_ann and not math.isnan(rv_10d_ann) else None
            # Freshness: vol-normalised acceleration of positioning over the last week vs the
            # 30d structural baseline. Mean-reversion edge concentrates where this is large.
            fund_z_delta = round(fund_z_7d - fund_z_30d, 3) if (fund_z_7d is not None and fund_z_30d is not None) else None

            rows.append({
                "symbol": sym,
                "category": u["category"],
                "price": round(price, 6),
                "vol_24h_m": round(vol_m, 2),
                "px_chg_24h": round(chg_24h, 2),
                "px_chg_30d": round(chg_30d, 2) if not math.isnan(chg_30d) else None,
                "px_7d_high": px_7d_high,
                "px_7d_low": px_7d_low,
                "fund_interval_h": interval_h,
                "fund_ann_30d": round(f30, 2),
                "fund_ann_7d": round(f7, 2),
                "fund_cum_30d": round(f30_cum, 3),
                "fund_delta_7d_30d": round(f7 - f30, 2),
                "fund_spark": [round(r * 100, 4) for r in spark],
                "realised_vol_10d_ann": round(rv_10d_ann, 2) if not math.isnan(rv_10d_ann) else None,
                "fund_z_30d": fund_z_30d,
                "fund_z_7d": fund_z_7d,
                "fund_z_delta": fund_z_delta,
                "trend_score": score,
                "trend_label": trend_label(score),
                "n_settlements_30d": len(d30),
            })

            if verbose and (i % 25 == 0 or i == len(universe)):
                print(f"  [{i}/{len(universe)}] processed")
            time.sleep(RATE_SLEEP)
        except Exception as e:
            if verbose:
                print(f"  [{i}/{len(universe)}] {sym}: ERROR {e}")
            continue

    return rows


FUND_Z_QUALIFY = 1.0       # |fund_z_30d| above this enters the rec table
CARRY_HOLD_DAYS = 14       # implied 14d delta-neutral carry
TOPLIST_LIMIT = 20         # cap on rec rows shown


def _risk_note(cat, vol_m):
    if cat in ("TradFi-Equity", "TradFi-Commodity"):
        return "TradFi delisting risk"
    if cat == "Crypto-Major":
        return "Major OI"
    if vol_m < 20:
        return "Low-liquidity tail"
    return ""


def build_recommendations(liquid):
    qualified = []
    for r in liquid.to_dict(orient="records"):
        fz = r.get("fund_z_30d")
        ts = r.get("trend_score") or 0
        fz_delta = r.get("fund_z_delta")
        if fz is None:
            continue
        if abs(fz) < FUND_Z_QUALIFY:
            continue
        # Direction: short paid carry if positive z + negative trend, long if negative z + positive trend.
        if fz > 0 and ts < 0:
            side, setup = "Short perp", "Stretched long topping"
        elif fz < 0 and ts > 0:
            side, setup = "Long perp", "Crowded short reclaim"
        else:
            continue  # funding extreme but trend neutral or aligned with crowd: no rec
        # Composite: structural extremity x trend confirmation x freshness multiplier.
        # Freshness elevates positioning that has accelerated past its 30d baseline (vulnerable to
        # mean reversion) above chronic structurals at the same fund_z level.
        freshness_mult = 1.0 + abs(fz_delta) if fz_delta is not None else 1.0
        magnitude = abs(fz) * max(abs(ts) / 6.0, 0.5) * freshness_mult
        # Stop: nearest 7d swing in the trade-against direction.
        stop = r.get("px_7d_high") if side == "Short perp" else r.get("px_7d_low")
        # Carry: % of notional captured if held delta-neutral CARRY_HOLD_DAYS days.
        carry_pct = round(abs(r["fund_ann_30d"]) * CARRY_HOLD_DAYS / 365.0, 2)
        qualified.append({
            "symbol": r["symbol"],
            "category": r["category"],
            "side": side,
            "setup": setup,
            "magnitude": round(magnitude, 3),
            "fund_ann_30d": r["fund_ann_30d"],
            "fund_z_30d": fz,
            "fund_z_delta": fz_delta,
            "realised_vol_10d_ann": r.get("realised_vol_10d_ann"),
            "trend_score": ts,
            "trend_label": r["trend_label"],
            "price": r["price"],
            "stop_price": stop,
            "carry_pct_14d": carry_pct,
            "vol_24h_m": r["vol_24h_m"],
            "risk_note": _risk_note(r["category"], r["vol_24h_m"]),
        })

    qualified.sort(key=lambda x: x["magnitude"], reverse=True)

    # Conviction: tertile by magnitude among qualifying.
    n = len(qualified)
    if n:
        top_n = max(1, n // 10)
        mid_n = max(1, n // 4)
        for i, rec in enumerate(qualified):
            if i < top_n:
                rec["conviction"] = 3
            elif i < top_n + mid_n:
                rec["conviction"] = 2
            else:
                rec["conviction"] = 1
            rec["rank"] = i + 1

    return qualified[:TOPLIST_LIMIT]


def build_payload(rows):
    df = pd.DataFrame(rows)

    # Liquid universe
    liquid = df[df["vol_24h_m"] >= MIN_VOLUME_USDT_M].copy()

    recommendations = build_recommendations(liquid)

    # Legacy headline-symbols list, kept for back-compat with anything reading scan.json.
    headline_syms = [r["symbol"] for r in recommendations if r.get("conviction", 0) >= 2]

    summary = {
        "universe_total": int(len(df)),
        "universe_liquid": int(len(liquid)),
        "stretched_longs": int((liquid["fund_ann_30d"] > 30).sum()),
        "crowded_shorts": int((liquid["fund_ann_30d"] < -15).sum()),
        "trend_confirmed_shorts": int(
            ((liquid["fund_ann_30d"] > 30) & (liquid["trend_score"] <= -3)).sum()
        ),
        "trend_confirmed_longs": int(
            ((liquid["fund_ann_30d"] < -15) & (liquid["trend_score"] >= 3)).sum()
        ),
        "regime_shifts": int(
            (liquid["fund_delta_7d_30d"].abs() > 25).sum()
        ),
        "recommendations_total": len(recommendations),
        "recommendations_high_conviction": sum(1 for r in recommendations if r.get("conviction") == 3),
    }

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": {
            "lookback_days": LOOKBACK_DAYS,
            "min_volume_usdt_m": MIN_VOLUME_USDT_M,
            "stretched_long_threshold": 30,
            "crowded_short_threshold": -15,
            "trend_confirm_score": 3,
            "fund_z_qualify": FUND_Z_QUALIFY,
            "carry_hold_days": CARRY_HOLD_DAYS,
        },
        "summary": summary,
        "rows": liquid.to_dict(orient="records"),
        "recommendations": recommendations,
        "headline_symbols": headline_syms,
    }
    return payload


def write_output(payload):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    out_json = DATA_DIR / "scan.json"
    out_json.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out_json} ({out_json.stat().st_size / 1024:.1f} KB)")

    if not TEMPLATE.exists():
        print(f"WARNING: {TEMPLATE} not found — skipping docs/index.html build")
        return

    template_html = TEMPLATE.read_text()
    embedded = json.dumps(payload, separators=(",", ":"))
    placeholder = "/*__INJECT_DATA__*/null"
    if placeholder not in template_html:
        print(f"WARNING: placeholder {placeholder!r} not in template — docs/index.html will fall back to fetch")
        out_html = template_html
    else:
        out_html = template_html.replace(placeholder, embedded)

    out_index = DOCS_DIR / "index.html"
    out_index.write_text(out_html)
    print(f"Wrote {out_index} ({out_index.stat().st_size / 1024:.1f} KB)")


def main():
    print(f"Starting scan at {datetime.now(timezone.utc).isoformat()}")
    rows = scan(verbose=True)
    if not rows:
        print("ERROR: No data — check Binance API access.")
        sys.exit(1)
    payload = build_payload(rows)
    print(f"Summary: {payload['summary']}")
    write_output(payload)
    print("Done.")


if __name__ == "__main__":
    main()
