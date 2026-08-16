"""
Hyperliquid cross-venue funding enrichment
==========================================
Adds Hyperliquid funding columns beside the Binance columns so the dashboard
can compare the SAME underlying on both venues over MATCHED trailing windows.
Motivated by a measurement trap: instantaneous funding snapshots routinely
carry the opposite sign of their own trailing mean (observed 2026-08-16 on
xyz:SKHX — spot −24%/yr against a +33%/yr 30d mean), so only like-for-like
trailing windows are comparable across venues.

Venues covered:
  - Hyperliquid core perps (crypto majors and alts; validator-operated book)
  - trade.xyz HIP-3 builder markets (equities, indices, ETF proxies,
    commodities) — coin ids are dex-prefixed, e.g. "xyz:NVDA"

Methodology (identical to the Binance columns in build.py):
  annualised % = mean per-settlement rate in window x settlements/year x 100
  Positive funding = longs pay shorts, on both venues.
  Windows: trailing 30d and 7d from the scan instant.

Guard layers (fail-open but loud — the scan must never lose the Binance data
because Hyperliquid was unreachable, and must never publish silently wrong
cross-venue numbers):
  1. Identity gate: an exact/alias pair is only published when the two venues'
     prices agree within PRICE_GATE_TOL. Rejections are recorded and shown.
  2. Completeness gate: a market whose fetched window is materially short is
     flagged partial and excluded from spread columns.
  3. Sentinel cross-check: Binance 30d funding for three sentinel symbols is
     recomputed here with an independent method (sum over actual span) and
     compared to build.py's value (mean x detected interval). Disagreement
     degrades the block: hl_* columns stay, spread columns are suppressed.
  4. Any unhandled failure downgrades to status="unavailable" with the error
     recorded; the Binance payload is untouched.

No API key is required for any call. Rate-limited politely; per-market fetch
failures are tolerated and counted.
"""

import json
import time
from datetime import datetime, timezone

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_TIMEOUT = 20
HL_RATE_SLEEP = 0.35          # seconds between funding-history calls
HL_MAX_MARKETS = 260          # defensive bound on markets fetched per run

WINDOW_LONG_D = 30
WINDOW_SHORT_D = 7
MS_PER_DAY = 86_400_000

PRICE_GATE_TOL = 0.05         # 5% max |price deviation| for exact/alias pairs
PARTIAL_MIN_SPAN_D = 25.0     # 30d window spanning fewer days than this = partial
PARTIAL_MIN_FILL = 0.6        # or fewer records than 60% of expected = partial

SENTINEL_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
SENTINEL_TOL_PP = 1.5         # absolute tolerance, percentage points annualised
SENTINEL_TOL_REL = 0.12       # relative tolerance

# Binance base -> Hyperliquid plain name, where tickers differ but the
# underlying is the same instrument. Every alias still passes the price
# identity gate before a pair is published.
ALIASES_TRADFI = {
    "SKHYNIX": "SKHX",
    "SAMSUNG": "SMSN",
    "XAU": "GOLD",
    "XAG": "SILVER",
    "XPT": "PLATINUM",
    "XPD": "PALLADIUM",
    "BZ": "BRENTOIL",
    "BRENT": "BRENTOIL",
    "SPX": "SP500",
}

# Related-but-not-identical underlyings (ETF vs index level, licensed index vs
# synthetic construction). Funding is scale-free so the comparison is still
# meaningful, but the price identity gate cannot apply. Labelled "proxy" on
# the dashboard; spreads are published with that label carried.
PROXY_PAIRS_TRADFI = {
    "SPY": "SP500",
    "QQQ": "XYZ100",
    "NDX": "XYZ100",
}

# Hyperliquid markets always shown on the dashboard watchlist (index / sector
# ETF proxies relevant to gate-and-thrust expression), whether or not a
# Binance pair exists.
WATCHLIST = [
    "xyz:SP500",
    "xyz:XYZ100",
    "xyz:SMH",
    "xyz:XLE",
    "xyz:SOXL",
    "xyz:URNM",
    "xyz:GOLD",
    "xyz:EWY",
    "xyz:EWJ",
]

TRADFI_DEX = "xyz"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def post_info(session, payload, max_retries=4):
    """POST to the public info endpoint with 429/5xx backoff."""
    for attempt in range(max_retries):
        try:
            r = session.post(HL_INFO_URL, json=payload, timeout=HL_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError(f"info request failed after {max_retries} attempts: {payload.get('type')}")


def fetch_markets(session, dex=None):
    """Return {coin_id: {"mark": float, "day_ntl_vlm_m": float}} for the
    ACTIVE universe of one dex (dex=None is the core book). coin_id keeps the
    dex prefix exactly as the API reports it ("BTC", "xyz:NVDA")."""
    payload = {"type": "metaAndAssetCtxs"}
    if dex:
        payload["dex"] = dex
    meta, ctxs = post_info(session, payload)
    out = {}
    for entry, ctx in zip(meta["universe"], ctxs):
        if entry.get("isDelisted"):
            continue
        mark = ctx.get("markPx") or ctx.get("oraclePx")
        out[entry["name"]] = {
            "mark": float(mark) if mark is not None else None,
            "day_ntl_vlm_m": round(float(ctx.get("dayNtlVlm", 0)) / 1e6, 2),
        }
    return out


def fetch_funding(session, coin, start_ms, end_ms):
    """Paginated fundingHistory for one coin. Returns [(ts_ms, rate)] asc,
    deduplicated on timestamp. The endpoint caps at 500 records per call
    (hourly settlements: a 30d window needs two calls)."""
    out = {}
    cursor = start_ms
    for _ in range(20):
        batch = post_info(session, {
            "type": "fundingHistory", "coin": coin,
            "startTime": cursor, "endTime": end_ms,
        })
        if not batch:
            break
        for b in batch:
            out[int(b["time"])] = float(b["fundingRate"])
        last = int(batch[-1]["time"])
        if len(batch) < 500 or last <= cursor:
            break
        cursor = last + 1
        time.sleep(HL_RATE_SLEEP)
    return sorted(out.items())


# ---------------------------------------------------------------------------
# Pure computation (unit-tested in tests/test_hyperliquid_funding.py)
# ---------------------------------------------------------------------------

def detect_interval_hours(times_ms):
    """Median gap between settlements, in hours. Hyperliquid settles hourly;
    kept general so the same code serves any venue's records."""
    if len(times_ms) < 3:
        return 1
    gaps = []
    for i in range(len(times_ms) - 1):
        g = (times_ms[i + 1] - times_ms[i]) / 3_600_000
        if 0 < g < 24:
            gaps.append(g)
    if not gaps:
        return 1
    return max(1, round(sorted(gaps)[len(gaps) // 2]))


def annualise_pct(mean_rate, interval_h):
    return mean_rate * (365 * 24 / interval_h) * 100


def compute_windows(records, now_ms):
    """Trailing 30d/7d annualised funding from [(ts_ms, rate)] records.

    Returns None when there is not enough data to say anything. Otherwise a
    dict with both windows, record counts, the detected interval, the actual
    span of the long window in days, and a partial flag (span or fill
    materially short of the requested 30d)."""
    if not records or len(records) < 3:
        return None
    long_start = now_ms - WINDOW_LONG_D * MS_PER_DAY
    short_start = now_ms - WINDOW_SHORT_D * MS_PER_DAY
    w30 = [(t, r) for t, r in records if t >= long_start]
    if len(w30) < 3:
        return None
    w7 = [(t, r) for t, r in w30 if t >= short_start]
    times = [t for t, _ in w30]
    interval_h = detect_interval_hours(times)
    span_days = (times[-1] - times[0]) / MS_PER_DAY
    expected = WINDOW_LONG_D * 24 / interval_h
    partial = span_days < PARTIAL_MIN_SPAN_D or len(w30) < PARTIAL_MIN_FILL * expected
    f30 = annualise_pct(sum(r for _, r in w30) / len(w30), interval_h)
    f7 = annualise_pct(sum(r for _, r in w7) / len(w7), interval_h) if w7 else None
    return {
        "f30": f30,
        "f7": f7,
        "n30": len(w30),
        "n7": len(w7),
        "interval_h": interval_h,
        "span_days": round(span_days, 1),
        "partial": partial,
    }


def base_from_symbol(symbol):
    """Binance USD-M symbol -> base asset. The scan universe is USDT-quoted
    only (build.py filters on quoteAsset == USDT)."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def map_base_to_hl(base, category, core_names, xyz_names):
    """Route a Binance base to a Hyperliquid coin id.

    TradFi rows match against the trade.xyz universe; crypto rows against the
    core book. Returns (coin_id, pair_type) or None. pair_type is "exact",
    "alias" (different ticker, same underlying — still price-gated) or
    "proxy" (related underlying, no price gate possible).
    """
    if category.startswith("TradFi"):
        plain = {n.split(":", 1)[1]: n for n in xyz_names}
        if base in plain:
            return plain[base], "exact"
        if base in ALIASES_TRADFI and ALIASES_TRADFI[base] in plain:
            return plain[ALIASES_TRADFI[base]], "alias"
        if base in PROXY_PAIRS_TRADFI and PROXY_PAIRS_TRADFI[base] in plain:
            return plain[PROXY_PAIRS_TRADFI[base]], "proxy"
        return None
    # Crypto: exact name, then the 1000x <-> k-prefix convention
    # (Binance "1000PEPE" and Hyperliquid "kPEPE" both quote 1000 units).
    if base in core_names:
        return base, "exact"
    if base.startswith("1000") and ("k" + base[4:]) in core_names:
        return "k" + base[4:], "exact"
    return None


def price_gate(bn_price, hl_mark):
    """Return (passes, deviation). Deviation is |bn/hl - 1|; None prices fail
    closed — identity cannot be confirmed without both prices."""
    if not bn_price or not hl_mark:
        return False, None
    dev = abs(bn_price / hl_mark - 1)
    return dev <= PRICE_GATE_TOL, dev


def sum_span_annualised(records, now_ms, window_days):
    """Independent annualisation used only by the sentinel check: sum of
    rates over the actual spanned days x 365. Interval-agnostic, so it agrees
    with the mean-x-interval method exactly when the record set is complete
    and diverges when it is gapped — which is what the sentinel is for."""
    start = now_ms - window_days * MS_PER_DAY
    w = [(t, r) for t, r in records if t >= start]
    if len(w) < 2:
        return None
    span_days = (w[-1][0] - w[0][0]) / MS_PER_DAY
    if span_days <= 0:
        return None
    return sum(r for _, r in w) / span_days * 365 * 100


def sentinel_check(funding_history_aggregate, rows_by_symbol, now_ms):
    """Cross-implementation agreement check on Binance 30d funding.

    build.py publishes mean x detected-interval; this recomputes sum / span
    from the same raw records. Material disagreement means one of the two
    implementations (or the underlying record set) is wrong, so cross-venue
    spreads must not be published until a human looks.
    """
    details = []
    status = "ok"
    for sym in SENTINEL_SYMBOLS:
        row = rows_by_symbol.get(sym)
        raw = funding_history_aggregate.get(sym)
        if row is None or not raw:
            details.append({"symbol": sym, "status": "missing"})
            continue
        recomputed = sum_span_annualised([(int(t), float(r)) for t, r in raw], now_ms, WINDOW_LONG_D)
        published = row.get("fund_ann_30d")
        if recomputed is None or published is None:
            details.append({"symbol": sym, "status": "missing"})
            continue
        diff = abs(recomputed - published)
        tol = max(SENTINEL_TOL_PP, SENTINEL_TOL_REL * abs(published))
        ok = diff <= tol
        details.append({
            "symbol": sym, "status": "ok" if ok else "fail",
            "published": round(published, 2), "recomputed": round(recomputed, 2),
            "tolerance": round(tol, 2),
        })
        if not ok:
            status = "fail"
    if all(d["status"] == "missing" for d in details):
        status = "fail"
    return {"status": status, "details": details}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def enrich_payload(payload, funding_history_aggregate, data_dir, verbose=True):
    """Mutates payload in place: adds hl_* fields to matched rows and a
    payload["hyperliquid"] block. Writes data/hl_funding_daily.json (daily
    mean rate per market, for persistence studies). Never raises: any
    unhandled failure records status="unavailable" and leaves the Binance
    payload untouched."""
    try:
        _enrich(payload, funding_history_aggregate, data_dir, verbose)
    except Exception as e:  # fail-open, loudly
        payload["hyperliquid"] = {
            "status": "unavailable",
            "reason": f"{type(e).__name__}: {e}",
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if verbose:
            print(f"Hyperliquid enrichment UNAVAILABLE: {type(e).__name__}: {e}")


def _enrich(payload, funding_history_aggregate, data_dir, verbose):
    t0 = time.time()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; funding-scanner/1.0)"})
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - WINDOW_LONG_D * MS_PER_DAY

    rows = payload.get("rows", [])
    rows_by_symbol = {r["symbol"]: r for r in rows}

    # Guard 3 first: if the two Binance annualisation implementations do not
    # agree on the sentinels, publish hl_* columns but suppress spreads.
    sentinel = sentinel_check(funding_history_aggregate or {}, rows_by_symbol, now_ms)
    spreads_ok = sentinel["status"] == "ok"

    core = fetch_markets(session, dex=None)
    xyz = fetch_markets(session, dex=TRADFI_DEX)
    core_names = set(core)
    xyz_names = set(xyz)

    # Pair every liquid Binance row with a Hyperliquid market where one exists.
    pairs = []      # (row, coin, pair_type)
    rejected = []
    for r in rows:
        cand = map_base_to_hl(base_from_symbol(r["symbol"]), r["category"], core_names, xyz_names)
        if cand is None:
            continue
        coin, pair_type = cand
        hl_mark = (core.get(coin) or xyz.get(coin) or {}).get("mark")
        if pair_type in ("exact", "alias"):
            ok, dev = price_gate(r.get("price"), hl_mark)
            if not ok:
                rejected.append({
                    "symbol": r["symbol"], "hl_symbol": coin, "reason": "price identity gate",
                    "bn_price": r.get("price"), "hl_price": hl_mark,
                    "deviation_pct": round(dev * 100, 2) if dev is not None else None,
                })
                continue
        pairs.append((r, coin, pair_type))

    # Fetch set: paired markets plus the standing watchlist, deduplicated.
    fetch_coins = {coin for _, coin, _ in pairs}
    fetch_coins.update(w for w in WATCHLIST if w in xyz_names or w in core_names)
    fetch_coins = sorted(fetch_coins)[:HL_MAX_MARKETS]

    windows = {}
    fetch_errors = 0
    daily = {}
    for i, coin in enumerate(fetch_coins, 1):
        try:
            recs = fetch_funding(session, coin, start_ms, now_ms)
            w = compute_windows(recs, now_ms)
            if w is not None:
                windows[coin] = w
                daily[coin] = _daily_means(recs)
        except Exception:
            fetch_errors += 1
        if verbose and (i % 40 == 0 or i == len(fetch_coins)):
            print(f"  [hl {i}/{len(fetch_coins)}] funding fetched")
        time.sleep(HL_RATE_SLEEP)

    # Mutate matched rows.
    matched = 0
    partial_n = 0
    for r, coin, pair_type in pairs:
        w = windows.get(coin)
        if w is None:
            continue
        matched += 1
        hl_info = core.get(coin) or xyz.get(coin) or {}
        r["hl_symbol"] = coin
        r["hl_pair_type"] = pair_type
        r["hl_price"] = hl_info.get("mark")
        r["hl_fund_ann_30d"] = round(w["f30"], 2)
        r["hl_fund_ann_7d"] = round(w["f7"], 2) if w["f7"] is not None else None
        r["hl_n_30d"] = w["n30"]
        r["hl_partial"] = w["partial"]
        if w["partial"]:
            partial_n += 1
        publish_spread = spreads_ok and not w["partial"]
        r["spread_ann_30d"] = round(r["fund_ann_30d"] - w["f30"], 2) if publish_spread else None
        r["spread_ann_7d"] = (
            round(r["fund_ann_7d"] - w["f7"], 2)
            if publish_spread and w["f7"] is not None and r.get("fund_ann_7d") is not None
            else None
        )

    watchlist_rows = []
    for coin in WATCHLIST:
        w = windows.get(coin)
        if w is None:
            continue
        hl_info = core.get(coin) or xyz.get(coin) or {}
        paired = next((r["symbol"] for r, c, _ in pairs if c == coin), None)
        watchlist_rows.append({
            "hl_symbol": coin,
            "hl_fund_ann_30d": round(w["f30"], 2),
            "hl_fund_ann_7d": round(w["f7"], 2) if w["f7"] is not None else None,
            "hl_partial": w["partial"],
            "hl_price": hl_info.get("mark"),
            "hl_day_ntl_vlm_m": hl_info.get("day_ntl_vlm_m"),
            "paired_symbol": paired,
        })

    status = "ok" if spreads_ok else "degraded"
    reason = None if spreads_ok else "sentinel cross-check failed — spread columns suppressed"
    payload["hyperliquid"] = {
        "status": status,
        "reason": reason,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": (
            "trailing mean per-settlement funding x settlements/year x 100; "
            "windows 30d/7d matched to the Binance columns; longs pay when positive; "
            f"exact/alias pairs price-gated at {PRICE_GATE_TOL:.0%}"
        ),
        "pairs_matched": matched,
        "pairs_partial": partial_n,
        "pairs_rejected": rejected,
        "markets_fetched": len(fetch_coins),
        "fetch_errors": fetch_errors,
        "sentinel": sentinel,
        "watchlist": watchlist_rows,
    }

    _write_daily(daily, data_dir, payload)
    if verbose:
        print(
            f"Hyperliquid enrichment: {matched} pairs ({partial_n} partial), "
            f"{len(rejected)} rejected by identity gate, {fetch_errors} fetch errors, "
            f"status={status}, {time.time() - t0:.0f}s"
        )


def _daily_means(records):
    """Collapse [(ts_ms, rate)] to [["YYYY-MM-DD", mean_rate], ...] on UTC
    days, for the committed persistence file. Python datetime is 1-indexed
    for months."""
    buckets = {}
    for t, r in records:
        day = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        buckets.setdefault(day, []).append(r)
    return [[day, sum(v) / len(v)] for day, v in sorted(buckets.items())]


def _write_daily(daily, data_dir, payload):
    out = {
        "generated_at_utc": payload.get("generated_at_utc"),
        "window_days": WINDOW_LONG_D,
        "note": "mean per-settlement funding rate per UTC day, per Hyperliquid market",
        "daily": daily,
    }
    path = data_dir / "hl_funding_daily.json"
    path.write_text(json.dumps(out, separators=(",", ":"), allow_nan=False))
