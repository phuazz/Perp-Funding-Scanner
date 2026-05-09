"""
One-time backfill of 90d Binance funding-rate history for the liquid universe.

build.py also pulls 90d funding history on every scan and writes the same
data/funding_history.json artifact, so this script is operationally
equivalent to a normal scan. It exists as a documented entry point for:

  - rebuilding the history file from scratch after a schema change
  - manual recovery if data/funding_history.json is corrupted or deleted

Usage:
    python scripts/backfill_funding_history.py

Per the project's storage decision, this writes a single aggregated
data/funding_history.json. Per-symbol intermediate files are not produced.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from build import scan, build_payload, write_output  # noqa: E402


def main() -> int:
    rows = scan(verbose=True)
    payload = build_payload(rows)
    write_output(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
