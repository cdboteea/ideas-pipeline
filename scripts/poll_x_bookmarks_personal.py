#!/usr/bin/env python3
"""
Personal-X bookmarks poller → Obsidian Inbox.

Companion to `poll_x_bookmarks.py` (which uses bird@clawdbot59030 — the
suspended bot account that CANNOT see Matias's personal bookmarks).

This script processes a CAPTURE FILE produced by Claude-in-Chrome — the
agent navigates Chrome to https://x.com/i/bookmarks, runs
`ideas.x_personal.BOOKMARKS_CAPTURE_JS`, and writes the result to a
JSON file. This script then diffs vs personal-state and stages new
bookmarks via `ideas.capture.stage_x_post`.

Usage:
    poll_x_bookmarks_personal.py <capture.json> [--dry-run] [--json]
    poll_x_bookmarks_personal.py --print-capture-js     # prints the JS to run

The state file is `~/clawd/data/x-bookmarks-personal-state.json` —
distinct from the bird-side state to avoid cross-account dedup poisoning.

Future: a launchd job that automates step 1 (Chrome MCP capture) once
the long-running CC-session-with-Chrome-MCP pattern is established. Until
then, the capture step is in-session-only.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas.x_personal import (
    BOOKMARKS_CAPTURE_JS,
    poll_personal,
    PERSONAL_STATE_FILE,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll personal-X bookmarks "
                                 "from a Claude-in-Chrome capture file.")
    ap.add_argument("capture", nargs="?", type=Path,
                    help="JSON file produced by the BOOKMARKS_CAPTURE_JS payload.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't stage; just report what would happen.")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON summary instead of human-readable text.")
    ap.add_argument("--print-capture-js", action="store_true",
                    help="Print the JS source to run in Chrome via MCP and exit.")
    ap.add_argument("--state-file", type=Path, default=PERSONAL_STATE_FILE,
                    help=f"State file path (default: {PERSONAL_STATE_FILE}).")
    args = ap.parse_args()

    if args.print_capture_js:
        print(BOOKMARKS_CAPTURE_JS)
        return 0

    if not args.capture:
        ap.error("capture file is required (or use --print-capture-js)")
    if not args.capture.exists():
        print(f"capture file not found: {args.capture}", file=sys.stderr)
        return 2

    summary = poll_personal(
        args.capture,
        dry_run=args.dry_run,
        state_file=args.state_file,
    )

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"# personal-X bookmarks poll  ({summary['source']})")
        print(f"  fetched:  {summary['fetched']}")
        print(f"  staged:   {summary['staged']}{'  (dry-run)' if summary['dry_run'] else ''}")
        print(f"  skipped:  {summary['skipped_duplicate']}")
        if summary["errors"]:
            print(f"  errors:   {len(summary['errors'])}")
            for e in summary["errors"][:5]:
                print(f"    - {e}")
        for item in summary["staged_items"][:10]:
            print(f"  + {item['tweet_id']}  @{item['author']}: {item['text_preview']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
