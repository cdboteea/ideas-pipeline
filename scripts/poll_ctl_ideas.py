#!/usr/bin/env python3
"""
CTL trade-idea poller — task #123.

Two-step process today (in-session via Chrome MCP, until launchd-fication):

  1. CC navigates Chrome to https://x.com/i/lists/936040010809307136 and
     runs `ideas.x_personal.BOOKMARKS_CAPTURE_JS` to extract recent posts.
     Save the result to a JSON file.

  2. This script reads the JSON, runs each post through codex (gpt-5) for
     classify-and-extract, persists ideas to ~/clawd/data/ctl-trade-ideas.duckdb,
     appends a summary line to ~/clawd/logs/ctl-trade-ideas.log.

Usage:
    poll_ctl_ideas.py <capture.json> [--dry-run] [--json] [--model gpt-5]
    poll_ctl_ideas.py --print-capture-js          # JS to run in Chrome
    poll_ctl_ideas.py --list-recent [--limit N]   # last N persisted ideas

State: ~/clawd/data/ctl-state.json — processed tweet IDs (last 2000).
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas import x_personal
from ideas import ctl_extractor as ctl


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Process CTL Trading list posts → structured trade ideas."
    )
    ap.add_argument("capture", nargs="?", type=Path,
                    help="JSON file from BOOKMARKS_CAPTURE_JS (raw post list).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't persist; just print what would happen.")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON summary instead of human text.")
    ap.add_argument("--print-capture-js", action="store_true",
                    help="Print the JS to run in Chrome via MCP, then exit.")
    ap.add_argument("--list-recent", action="store_true",
                    help="Show recent persisted trade ideas, then exit.")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--model", default=None,
                    help="Codex model override (default: codex CLI's default).")
    args = ap.parse_args()

    if args.print_capture_js:
        # The CTL list uses the SAME timeline structure as the bookmarks page,
        # so the same capture JS works without modification.
        print(x_personal.BOOKMARKS_CAPTURE_JS)
        return 0

    if args.list_recent:
        ideas = ctl.list_recent_ideas(limit=args.limit)
        if args.json:
            print(json.dumps(ideas, indent=2, default=str))
        else:
            print(f"# {len(ideas)} recent CTL trade idea(s)")
            for it in ideas:
                bits = [it["ticker"] or "?", (it["direction"] or "?")[:1].upper()]
                if it["entry_text"]:  bits.append(f"E:{it['entry_text']}")
                if it["stop_text"]:   bits.append(f"S:{it['stop_text']}")
                if it["target_text"]: bits.append(f"T:{it['target_text']}")
                if it["horizon"]:     bits.append(f"H:{it['horizon']}")
                print(f"  {it['posted_at']}  @{it['author_handle']:14s}  "
                      f"{' | '.join(bits)}")
        return 0

    if not args.capture:
        ap.error("capture file required (or use --print-capture-js / --list-recent)")
    if not args.capture.exists():
        print(f"capture not found: {args.capture}", file=sys.stderr)
        return 2

    raw = json.loads(args.capture.read_text())
    posts = x_personal.normalize_capture(raw)

    summary = ctl.process_posts(posts, dry_run=args.dry_run, model=args.model)

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"# CTL pipeline run  (model={summary['model']})")
        print(f"  fetched:           {summary['fetched']}")
        print(f"  skipped (seen):    {summary['skipped_seen']}")
        print(f"  classified idea:   {summary['classified_idea']}")
        print(f"  classified skip:   {summary['classified_skip']}")
        print(f"  extracted valid:   {summary['extracted_valid']}"
              f"{'  (dry-run)' if summary['dry_run'] else ''}")
        print(f"  extracted dropped: {summary['extracted_dropped']}")
        if summary["errors"]:
            print(f"  errors:            {len(summary['errors'])}")
            for e in summary["errors"][:5]:
                print(f"    - {e}")
        for it in summary["ideas"][:10]:
            preview = []
            if it["entry"]:  preview.append(f"E:{it['entry']}")
            if it["stop"]:   preview.append(f"S:{it['stop']}")
            if it["target"]: preview.append(f"T:{it['target']}")
            print(f"  + {it['ticker']:8s} {(it['direction'] or '?')[:1].upper()}  "
                  f"@{it['author']:14s}  {' | '.join(preview)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
