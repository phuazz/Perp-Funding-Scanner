"""
Smoke tests for the perp funding scanner.

Run after a build to verify data integrity and market-hours logic. Exits
non-zero on first failure so CI / Task Scheduler wrappers can flag breakage.

Usage:
    python scripts/test_scanner.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make scripts/ importable so we can call cash_session_open / compute_action
# directly. Tests don't run a network scan — they introspect the existing
# scan.json plus exercise the pure helpers.
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from build import (  # noqa: E402  (import after sys.path tweak)
    cash_session_open,
    compute_action,
    ACTION_STATES,
    SUPPORTED_CALENDAR_YEAR,
)

REPO_ROOT = SCRIPTS_DIR.parent
SCAN_PATH = REPO_ROOT / "data" / "scan.json"
DOCS_PATH = REPO_ROOT / "docs" / "index.html"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append((name, detail or "assertion failed"))


def at_ny(iso: str) -> datetime:
    """Parse an ISO datetime as a wall-clock America/New_York instant."""
    dt = datetime.fromisoformat(iso)
    return dt.replace(tzinfo=NY).astimezone(UTC)


# ---------------------------------------------------------------------------
# Tests: data files and JSON schema
# ---------------------------------------------------------------------------

def test_data_files_exist() -> None:
    check("data/scan.json exists", SCAN_PATH.exists(), str(SCAN_PATH))
    check("docs/index.html exists", DOCS_PATH.exists(), str(DOCS_PATH))


def test_json_parses_and_has_required_top_level() -> None:
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    required_keys = {"generated_at_utc", "config", "summary", "rows", "recommendations"}
    missing = required_keys - set(payload.keys())
    check("scan.json has required top-level keys", not missing, f"missing: {missing}")
    check("scan.json has rows", len(payload.get("rows") or []) > 0,
          "rows array empty")


def test_row_schema() -> None:
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    if not rows:
        return
    required = {
        "symbol", "category", "fund_ann_30d", "fund_z_30d", "fund_z_delta",
        "trend_score", "action", "trade_direction", "cash_session",
        "awaiting_cash_open",
    }
    missing_per_row = [
        (r.get("symbol", "?"), required - set(r.keys())) for r in rows
    ]
    bad = [(s, m) for s, m in missing_per_row if m]
    check("every row has required fields", not bad,
          f"first bad row: {bad[0] if bad else None}")


def test_action_values_are_legal() -> None:
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    bad = [r["symbol"] for r in rows if r.get("action") not in ACTION_STATES]
    check("all rows have legal action state", not bad,
          f"first invalid: {bad[:3]}")


def test_cash_session_alignment_with_category() -> None:
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    bad_tradfi = [r["symbol"] for r in rows
                  if r.get("category", "").startswith("TradFi")
                  and r.get("cash_session") not in {"Open", "Closed"}]
    bad_crypto = [r["symbol"] for r in rows
                  if not r.get("category", "").startswith("TradFi")
                  and r.get("cash_session") is not None]
    check("TradFi rows always have cash_session set", not bad_tradfi,
          f"first: {bad_tradfi[:3]}")
    check("Crypto rows always have cash_session=null", not bad_crypto,
          f"first: {bad_crypto[:3]}")


VALID_HOLDING_WINDOWS = {"Tactical 1-5d", "Swing 5-20d", "Strategic 20d+"}


def test_recommendation_phase2_fields() -> None:
    """Every rec record carries the actionable-setup fields used by the panel."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    recs = payload.get("recommendations") or []
    if not recs:
        # Empty rec set is valid; nothing to check here.
        return
    required = {
        "stop_pct", "stop_vol_multiple", "holding_window",
        "entry_trigger_text", "entry_trigger_met", "carry_scenarios",
        "px_7d_ma", "roundtrip_cost_pct",
    }
    bad_missing = []
    bad_holding = []
    bad_carry = []
    bad_trigger = []
    for r in recs:
        sym = r.get("symbol", "?")
        miss = required - set(r.keys())
        if miss:
            bad_missing.append((sym, miss))
        if r.get("holding_window") not in VALID_HOLDING_WINDOWS:
            bad_holding.append((sym, r.get("holding_window")))
        cs = r.get("carry_scenarios") or {}
        if not {"persists", "decay", "flip"}.issubset(cs.keys()):
            bad_carry.append((sym, list(cs.keys())))
        et = r.get("entry_trigger_text")
        if et is not None and "7d MA" not in et:
            bad_trigger.append((sym, et))
    check("rec records have phase-2 fields", not bad_missing,
          f"first: {bad_missing[0] if bad_missing else None}")
    check("holding_window is one of three known strings", not bad_holding,
          f"first bad: {bad_holding[0] if bad_holding else None}")
    check("carry_scenarios has persists/decay/flip keys", not bad_carry,
          f"first bad: {bad_carry[0] if bad_carry else None}")
    check("entry_trigger_text mentions 7d MA when present", not bad_trigger,
          f"first bad: {bad_trigger[0] if bad_trigger else None}")


def test_carry_scenario_invariants() -> None:
    """For a non-zero rate: persists >= decay >= flip == 0 (by construction)."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    recs = payload.get("recommendations") or []
    bad = []
    for r in recs:
        cs = r.get("carry_scenarios") or {}
        p, d, f = cs.get("persists"), cs.get("decay"), cs.get("flip")
        if p is None or d is None or f is None:
            continue
        # decay should be roughly half of persists; flip is exactly 0 by spec.
        if not (p >= d - 1e-6 and d >= f - 1e-6 and abs(f) < 1e-6):
            bad.append((r.get("symbol"), p, d, f))
    check("carry scenarios obey persist >= decay >= flip == 0",
          not bad, f"first bad: {bad[0] if bad else None}")


def test_track_record_shape() -> None:
    """track_record is either None (Untested) or a dict with the three known keys."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    bad = []
    for r in rows:
        tr = r.get("track_record")
        if tr is None:
            continue
        if not isinstance(tr, dict):
            bad.append((r["symbol"], "not-dict"))
            continue
        if not {"signal_count", "hit_rate"}.issubset(tr.keys()):
            bad.append((r["symbol"], list(tr.keys())))
            continue
        if not (0 <= tr["hit_rate"] <= 1):
            bad.append((r["symbol"], f"hit_rate {tr['hit_rate']}"))
    check("track_record has signal_count + hit_rate, hit_rate in [0,1]",
          not bad, f"first bad: {bad[0] if bad else None}")


def test_universe_rank_present_and_consistent() -> None:
    """Every liquid row with non-null fund_z_30d has universe_rank in [1, total],
    and the total matches the count of ranked rows."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    ranked = [r for r in rows if r.get("universe_rank") is not None]
    if not ranked:
        return
    totals = {r["universe_rank_total"] for r in ranked}
    bad_rank = [r["symbol"] for r in ranked
                if r["universe_rank"] < 1 or r["universe_rank"] > r["universe_rank_total"]]
    check("universe_rank_total agrees across rows", len(totals) == 1,
          f"distinct totals: {totals}")
    check("universe_rank in [1, total]", not bad_rank,
          f"first bad: {bad_rank[:3]}")
    # Verify every row with non-null fund_z is ranked
    rows_with_z = [r for r in rows if r.get("fund_z_30d") is not None]
    bad_unranked = [r["symbol"] for r in rows_with_z if r.get("universe_rank") is None]
    check("every row with fund_z gets a universe_rank", not bad_unranked,
          f"first bad: {bad_unranked[:3]}")


def test_topdecile_recommendations_present() -> None:
    """Top-decile rec list exists and uses cutoff <= absolute threshold."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    check("recommendations_topdecile key present", "recommendations_topdecile" in payload)
    check("topdecile_cutoff_fund_z key present", "topdecile_cutoff_fund_z" in payload)
    cutoff = payload.get("topdecile_cutoff_fund_z")
    if cutoff is not None:
        # Top decile cutoff should be at or above the 0.5 floor and could be
        # above or below the absolute threshold of 1.0 depending on regime.
        check("topdecile cutoff respects 0.5 floor", cutoff >= 0.5,
              f"cutoff = {cutoff}")


def test_funding_history_file() -> None:
    """data/funding_history.json exists and contains entries for liquid symbols."""
    fh_path = REPO_ROOT / "data" / "funding_history.json"
    check("data/funding_history.json exists", fh_path.exists(), str(fh_path))
    if not fh_path.exists():
        return
    fh = json.loads(fh_path.read_text(encoding="utf-8"))
    check("funding_history has 'history' key", "history" in fh)
    if "history" in fh:
        check("funding_history has at least 50 symbols",
              len(fh["history"]) >= 50,
              f"got {len(fh['history'])}")


def test_roundtrip_cost_formula() -> None:
    """roundtrip_cost_pct = 0.10 (10bps slippage round-trip) + |fund_ann_30d| * 14/365."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    recs = payload.get("recommendations") or []
    bad = []
    for r in recs:
        f = r.get("fund_ann_30d")
        rt = r.get("roundtrip_cost_pct")
        if f is None or rt is None:
            continue
        expected = 0.10 + abs(f) * 14 / 365.0
        if abs(rt - expected) > 0.02:  # rounding tolerance
            bad.append((r.get("symbol"), expected, rt))
    check("roundtrip_cost_pct = 10bps + |F| * 14/365", not bad,
          f"first bad: {bad[0] if bad else None}")


def test_stop_vol_multiple_when_realised_vol_present() -> None:
    """If stop_pct and realised_vol are both populated, stop_vol_multiple should be too."""
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    recs = payload.get("recommendations") or []
    bad = []
    for r in recs:
        if r.get("stop_pct") is not None and r.get("realised_vol_10d_ann"):
            if r.get("stop_vol_multiple") is None:
                bad.append(r.get("symbol"))
    check("stop_vol_multiple computed when inputs available", not bad,
          f"first bad: {bad[:3] if bad else None}")


def test_trade_direction_format() -> None:
    if not SCAN_PATH.exists():
        return
    payload = json.loads(SCAN_PATH.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    bad = []
    for r in rows:
        td = r.get("trade_direction")
        if td is None:
            continue
        # Must be "Long X" or "Short X" with X = symbol
        ok = td.startswith("Long ") or td.startswith("Short ")
        if not ok or not td.endswith(r["symbol"]):
            bad.append((r["symbol"], td))
    check("trade_direction is 'Long SYMBOL' or 'Short SYMBOL'", not bad,
          f"first bad: {bad[:3]}")


# ---------------------------------------------------------------------------
# Tests: cash_session_open against known dates
# ---------------------------------------------------------------------------

# Each tuple: (NY local ISO, expected, description). Open boundary 09:30
# inclusive, close boundary exclusive (16:00 / 13:00 ET reads as Closed).
CASH_CASES = [
    # Normal weekdays
    ("2026-01-02T10:00:00", True,  "Friday Jan 2 normal session"),
    ("2026-09-15T09:30:00", True,  "exactly at open"),
    ("2026-09-15T15:59:00", True,  "1 minute before close"),
    ("2026-09-15T16:00:00", False, "exactly at close (excluded)"),
    ("2026-09-15T09:29:00", False, "1 minute before open"),
    # Weekends
    ("2026-01-03T10:00:00", False, "Saturday"),
    ("2026-01-04T10:00:00", False, "Sunday"),
    # 2026 full-close holidays
    ("2026-01-01T10:00:00", False, "New Year's Day"),
    ("2026-01-19T10:00:00", False, "MLK Day"),
    ("2026-02-16T10:00:00", False, "Presidents Day"),
    ("2026-04-03T10:00:00", False, "Good Friday"),
    ("2026-05-25T10:00:00", False, "Memorial Day"),
    ("2026-06-19T10:00:00", False, "Juneteenth"),
    ("2026-07-03T10:00:00", False, "Independence Day observed (Jul 4 is Sat)"),
    ("2026-09-07T10:00:00", False, "Labor Day"),
    ("2026-11-26T10:00:00", False, "Thanksgiving"),
    ("2026-12-25T10:00:00", False, "Christmas Day"),
    # 2026 half days
    ("2026-11-27T11:00:00", True,  "Black Friday before 13:00"),
    ("2026-11-27T12:59:00", True,  "Black Friday 1 min before close"),
    ("2026-11-27T13:00:00", False, "Black Friday at half-day close"),
    ("2026-12-24T11:00:00", True,  "Christmas Eve before 13:00"),
    ("2026-12-24T13:00:00", False, "Christmas Eve at half-day close"),
    # Month boundary
    ("2026-01-30T10:00:00", True,  "last weekday of January"),
    ("2026-02-02T10:00:00", True,  "first weekday of February"),
    # Year boundary (last 2026 weekday and first 2027 weekday)
    ("2026-12-31T10:00:00", True,  "Thursday Dec 31 2026 (no holiday)"),
    # 2027 falls outside the supported calendar; helper falls back to weekday/time only.
    # Jan 1 2027 is Friday — would normally be a holiday but the fallback misses it.
    # This documents that gap and forces an explicit calendar update.
    ("2027-01-04T10:00:00", True,  "2027 Monday — fallback to weekday-only check"),
]


def test_market_hours_known_dates() -> None:
    bad: list[tuple[str, bool, bool]] = []
    for iso, expected, desc in CASH_CASES:
        got = cash_session_open(at_ny(iso))
        if got != expected:
            bad.append((f"{iso} ({desc})", expected, got))
    check(
        f"market hours: {len(CASH_CASES)} known dates",
        not bad,
        f"first failure: {bad[0] if bad else None}",
    )


def test_supported_calendar_year_constant() -> None:
    check("supported calendar year is 2026", SUPPORTED_CALENDAR_YEAR == 2026,
          f"got {SUPPORTED_CALENDAR_YEAR}")


# ---------------------------------------------------------------------------
# Tests: compute_action coverage
# ---------------------------------------------------------------------------

ACTION_CASES = [
    # (fund_z, trend_score, cash, is_tradfi, expected_action)
    (0.5, +6, None, False, "Avoid"),     # funding moderate
    (-0.5, +6, None, False, "Avoid"),
    (None, +6, None, False, "Avoid"),    # missing fund_z
    (2.0, -6, None, False, "Live"),      # short with strong rolling-over trend
    (2.0, -3, None, False, "Live"),      # short at boundary
    (2.0, -2, None, False, "Stage"),     # inflecting
    (2.0, +2, None, False, "Stage"),
    (2.0, +3, None, False, "Wait"),      # short, mild against
    (2.0, +5, None, False, "Wait"),
    (2.0, +6, None, False, "Avoid"),     # short, saturated against
    (-2.0, +6, None, False, "Live"),     # long with reclaiming trend
    (-2.0, -6, None, False, "Avoid"),    # long, max capitulation
    # Cash-hours downgrade only fires on TradFi + closed + Live
    (-2.0, +6, "Open",   True, "Live"),
    (-2.0, +6, "Closed", True, "Stage"),
    (-2.0, +6, "Closed", False, "Live"), # crypto: cash flag ignored
    (2.0, -2, "Closed", True, "Stage"),  # already Stage, stays Stage (no extra flag)
]


def test_compute_action_table() -> None:
    bad: list[tuple] = []
    for fz, ts, cash, tradfi, expected in ACTION_CASES:
        got = compute_action(fz, ts, cash, tradfi)
        if got[0] != expected:
            bad.append((fz, ts, cash, tradfi, expected, got))
    check(
        f"compute_action: {len(ACTION_CASES)} cases",
        not bad,
        f"first failure: {bad[0] if bad else None}",
    )


def test_awaiting_cash_open_flag() -> None:
    # Live downgraded by closed cash should set awaiting_cash_open = True.
    a, _, await_open = compute_action(-2.0, +6, "Closed", True)
    check("downgraded action sets awaiting_cash_open", a == "Stage" and await_open is True)
    # Native Stage (no downgrade) should not set the flag.
    a, _, await_open = compute_action(2.0, 0, "Closed", True)
    check("native Stage does not set awaiting_cash_open", a == "Stage" and await_open is False)
    # Crypto rows never trigger the cash-hours path.
    a, _, await_open = compute_action(-2.0, +6, None, False)
    check("crypto Live preserves Live + no cash flag", a == "Live" and await_open is False)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_TESTS = [
    test_data_files_exist,
    test_json_parses_and_has_required_top_level,
    test_row_schema,
    test_action_values_are_legal,
    test_cash_session_alignment_with_category,
    test_trade_direction_format,
    test_recommendation_phase2_fields,
    test_carry_scenario_invariants,
    test_track_record_shape,
    test_universe_rank_present_and_consistent,
    test_topdecile_recommendations_present,
    test_funding_history_file,
    test_roundtrip_cost_formula,
    test_stop_vol_multiple_when_realised_vol_present,
    test_market_hours_known_dates,
    test_supported_calendar_year_constant,
    test_compute_action_table,
    test_awaiting_cash_open_flag,
]


def main() -> int:
    for fn in ALL_TESTS:
        fn()

    for name in PASSED:
        print(f"  PASS  {name}")
    for name, detail in FAILED:
        print(f"  FAIL  {name} -- {detail}")

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
