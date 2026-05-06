#!/usr/bin/env python3
"""Enrich open CTL threads with MFE/MAE/pnl + auto-close on stop/target.

Same logic as `ideas ctl-enrich`, available as a standalone script for
launchd / cron use. Run me daily after market close.

State / reads:
  - ~/clawd/data/ctl-trade-ideas.duckdb (source of open threads)
  - ~/clawd/data/firstrate.duckdb (read-only, daily bars)
  - ~/clawd/logs/ctl-enrich.log (one summary line per fire)

Usage:
    enrich_ctl_outcomes.py [--json]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas import ctl_outcomes


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich CTL open threads.")
    ap.add_argument("--json", action="store_true", help="JSON output.")
    args = ap.parse_args()

    summary = ctl_outcomes.enrich_all_open()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print("# CTL enrichment")
        for k, v in summary.items():
            if k == "errors":
                print(f"  {k}: {len(v)}")
                for e in v[:5]:
                    print(f"    - {e}")
            else:
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
