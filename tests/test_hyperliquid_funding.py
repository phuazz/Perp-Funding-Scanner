"""Unit tests for scripts/hyperliquid_funding.py — pure functions only, no
network. Run with `pytest tests/` from the repo root.

Date convention: Python datetime months are 1-indexed. All synthetic records
are built tz-aware UTC and converted to epoch milliseconds.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hyperliquid_funding as hf


def ms(dt):
    return int(dt.timestamp() * 1000)


def hourly_records(start_dt, end_dt, rate):
    """Inclusive hourly settlement records from start to end at a constant rate."""
    out = []
    t = start_dt
    while t <= end_dt:
        out.append((ms(t), rate))
        t += timedelta(hours=1)
    return out


# ---------------------------------------------------------------------------
# Annualisation and windows
# ---------------------------------------------------------------------------

def test_annualise_constant_hourly_rate():
    # 0.00001 per hour -> x 8760 x 100 = 8.76% annualised, both windows.
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    recs = hourly_records(now - timedelta(days=30), now, 0.00001)
    w = hf.compute_windows(recs, ms(now))
    assert w is not None
    assert abs(w["f30"] - 8.76) < 1e-9
    assert abs(w["f7"] - 8.76) < 1e-9
    assert w["interval_h"] == 1
    assert not w["partial"]


def test_month_boundary_window():
    # Month boundary edge case (mandatory): window maths across 31 Jan -> 1 Feb.
    # Records span 20 Jan to 5 Feb; the 7d window must slice at 29 Jan.
    now = datetime(2026, 2, 5, 0, 0, tzinfo=timezone.utc)
    recs = hourly_records(datetime(2026, 1, 20, 0, 0, tzinfo=timezone.utc), now, 0.0001)
    w = hf.compute_windows(recs, ms(now))
    assert w is not None
    # 7d window: 29 Jan 00:00 to 5 Feb 00:00 inclusive = 169 hourly records.
    assert w["n7"] == 169
    # All 16 days of records fall inside the 30d window.
    assert w["n30"] == len(recs)
    # 16d span against a requested 30d window -> partial must flag.
    assert w["partial"]
    assert abs(w["span_days"] - 16.0) < 0.1


def test_year_boundary_window():
    # Year boundary edge case (mandatory): 31 Dec 2025 -> 1 Jan 2026.
    now = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)
    recs = hourly_records(datetime(2025, 12, 25, 0, 0, tzinfo=timezone.utc), now, -0.00002)
    w = hf.compute_windows(recs, ms(now))
    assert w is not None
    assert w["n7"] == 169
    assert w["n30"] == len(recs)
    # Constant negative rate annualises negative across the boundary.
    assert w["f30"] < 0 and abs(w["f30"] - (-0.00002 * 8760 * 100)) < 1e-9


def test_consistent_coarser_cadence_is_not_partial():
    # A market settling every 4h is complete at its own cadence — the fill
    # check uses the DETECTED interval, so this must not flag partial.
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    full = hourly_records(now - timedelta(days=30), now, 0.00001)
    sparse = full[::4]
    w = hf.compute_windows(sparse, ms(now))
    assert w is not None
    assert w["interval_h"] == 4
    assert not w["partial"]


def test_partial_flag_on_gapped_hourly_series():
    # An hourly market with a 20-day hole in the middle: full 30d span, median
    # gap still 1h, but the record count is far below expectation -> partial.
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    head = hourly_records(now - timedelta(days=30), now - timedelta(days=25), 0.00001)
    tail = hourly_records(now - timedelta(days=5), now, 0.00001)
    w = hf.compute_windows(head + tail, ms(now))
    assert w is not None
    assert w["interval_h"] == 1
    assert w["span_days"] > 25
    assert w["partial"]


def test_windows_returns_none_on_insufficient_data():
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    assert hf.compute_windows([], ms(now)) is None
    assert hf.compute_windows([(ms(now), 0.0001)], ms(now)) is None
    # Records entirely outside the 30d window also yield None.
    old = hourly_records(now - timedelta(days=90), now - timedelta(days=60), 0.0001)
    assert hf.compute_windows(old, ms(now)) is None


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

CORE = {"BTC", "ETH", "SOL", "kPEPE", "kSHIB", "kFLOKI"}
XYZ = {"xyz:NVDA", "xyz:SKHX", "xyz:SMSN", "xyz:SP500", "xyz:XYZ100", "xyz:GOLD", "xyz:DRAM"}


def test_crypto_exact_and_k_prefix_mapping():
    assert hf.map_base_to_hl("BTC", "Crypto-Major", CORE, XYZ) == ("BTC", "exact")
    assert hf.map_base_to_hl("1000PEPE", "Crypto-Alt", CORE, XYZ) == ("kPEPE", "exact")
    assert hf.map_base_to_hl("1000SHIB", "Crypto-Alt", CORE, XYZ) == ("kSHIB", "exact")
    assert hf.map_base_to_hl("NOSUCH", "Crypto-Alt", CORE, XYZ) is None


def test_tradfi_exact_alias_and_proxy_mapping():
    assert hf.map_base_to_hl("NVDA", "TradFi-Equity", CORE, XYZ) == ("xyz:NVDA", "exact")
    assert hf.map_base_to_hl("DRAM", "TradFi-Equity", CORE, XYZ) == ("xyz:DRAM", "exact")
    assert hf.map_base_to_hl("SKHYNIX", "TradFi-Equity", CORE, XYZ) == ("xyz:SKHX", "alias")
    assert hf.map_base_to_hl("SAMSUNG", "TradFi-Equity", CORE, XYZ) == ("xyz:SMSN", "alias")
    assert hf.map_base_to_hl("XAU", "TradFi-Commodity", CORE, XYZ) == ("xyz:GOLD", "alias")
    assert hf.map_base_to_hl("SPY", "TradFi-Equity", CORE, XYZ) == ("xyz:SP500", "proxy")
    assert hf.map_base_to_hl("QQQ", "TradFi-Equity", CORE, XYZ) == ("xyz:XYZ100", "proxy")
    assert hf.map_base_to_hl("ZZZT", "TradFi-Equity", CORE, XYZ) is None


def test_tradfi_never_matches_core_and_crypto_never_matches_xyz():
    # A crypto base whose name collides with an xyz equity must not cross venues.
    assert hf.map_base_to_hl("NVDA", "Crypto-Alt", CORE, XYZ) is None
    # A TradFi base must not match a core crypto coin of the same name.
    assert hf.map_base_to_hl("BTC", "TradFi-Equity", CORE, XYZ) is None


def test_base_from_symbol():
    assert hf.base_from_symbol("BTCUSDT") == "BTC"
    assert hf.base_from_symbol("1000PEPEUSDT") == "1000PEPE"
    assert hf.base_from_symbol("SKHYNIXUSDT") == "SKHYNIX"


# ---------------------------------------------------------------------------
# Identity gate
# ---------------------------------------------------------------------------

def test_price_gate_passes_close_prices():
    ok, dev = hf.price_gate(1175.72, 1182.2)
    assert ok
    assert dev < 0.01


def test_price_gate_rejects_wrong_identity():
    # SKHYUSDT (168) against xyz:SKHX (1182) — different share lines.
    ok, dev = hf.price_gate(167.38, 1182.2)
    assert not ok
    assert dev > 0.5


def test_price_gate_fails_closed_on_missing_price():
    ok, dev = hf.price_gate(None, 1182.2)
    assert not ok and dev is None
    ok, dev = hf.price_gate(1175.72, None)
    assert not ok and dev is None


# ---------------------------------------------------------------------------
# Sentinel cross-implementation check
# ---------------------------------------------------------------------------

def _sentinel_fixture(now, rate=0.0001, interval_h=8, days=35):
    recs = []
    t = now - timedelta(days=days)
    while t <= now:
        recs.append((ms(t), rate))
        t += timedelta(hours=interval_h)
    return recs


def test_sentinel_agreement_on_complete_series():
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    published = 0.0001 * (365 * 24 / 8) * 100  # mean x interval method
    fh = {s: _sentinel_fixture(now) for s in hf.SENTINEL_SYMBOLS}
    rows = {s: {"fund_ann_30d": round(published, 2)} for s in hf.SENTINEL_SYMBOLS}
    out = hf.sentinel_check(fh, rows, ms(now))
    assert out["status"] == "ok"
    assert all(d["status"] == "ok" for d in out["details"])


def test_sentinel_detects_divergence():
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    fh = {s: _sentinel_fixture(now) for s in hf.SENTINEL_SYMBOLS}
    # Published value far from what the records support -> fail.
    rows = {s: {"fund_ann_30d": 50.0} for s in hf.SENTINEL_SYMBOLS}
    out = hf.sentinel_check(fh, rows, ms(now))
    assert out["status"] == "fail"


def test_sentinel_all_missing_is_fail():
    now = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    out = hf.sentinel_check({}, {}, ms(now))
    assert out["status"] == "fail"
