#!/usr/bin/env python3
"""
X/Twitter bookmarks poller → Obsidian Inbox.

Uses the `bird` CLI (NEVER web_fetch on x.com — see CLAUDE.md).

Workflow:
  1. Run `bird bookmarks --count N --json`
  2. For each new tweet (not in our local state), stage to Inbox
     via capture.stage_x_post()
  3. Optionally unbookmark staged tweets on X (default: off — treat
     X bookmarks as durable reading list; state file dedupes us)

Usage:
    poll_x_bookmarks.py [--count 20] [--unbookmark] [--dry-run] [--json]

State: ~/clawd/data/x-bookmarks-state.json — list of processed tweet IDs
(keeps last 1000 so we can run forever without re-staging).

The launchd agent `com.matias.ideas-x-bookmarks` (daily 09:30) runs this.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `ideas` importable when invoked from anywhere
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas.capture import stage_x_post


BIRD_BIN = Path.home() / "clawd" / "bin" / "bird"
STATE_FILE = Path.home() / "clawd" / "data" / "x-bookmarks-state.json"


# ── State ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Bird wrapper ─────────────────────────────────────────────────────────

def fetch_bookmarks(count: int, bird_bin: Path = BIRD_BIN, runner=None) -> list[dict]:
    """
    Run `bird bookmarks --json`. Returns list of tweet dicts.

    `runner` is injected for tests (default: subprocess.run). Must accept
    (cmd, capture_output, text, timeout) and return an object with
    .stdout, .stderr, .returncode.
    """
    if runner is None:
        runner = subprocess.run
    cmd = [str(bird_bin), "bookmarks", "--count", str(count), "--json"]
    result = runner(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"bird bookmarks failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bird returned non-JSON: {e} (output: {result.stdout[:200]!r})")


def unbookmark(tweet_id: str, bird_bin: Path = BIRD_BIN, runner=None) -> None:
    if runner is None:
        runner = subprocess.run
    cmd = [str(bird_bin), "unbookmark", tweet_id]
    runner(cmd, capture_output=True, text=True, timeout=20)


# ── Stage helper ─────────────────────────────────────────────────────────

def _tweet_to_url(tweet: dict) -> str:
    author = tweet.get("author", {}) or {}
    username = author.get("username") or "anonymous"
    tid = tweet.get("id") or ""
    return f"https://x.com/{username}/status/{tid}"


def stage_tweet(tweet: dict) -> str:
    author = tweet.get("author", {}) or {}
    handle = author.get("username") or "anonymous"
    name = author.get("name") or handle
    content = tweet.get("text") or ""
    url = _tweet_to_url(tweet)
    return stage_x_post(
        url=url,
        author=f"@{handle} ({name})",
        content=content,
    )


# ── Core poll ────────────────────────────────────────────────────────────

def poll(
    count: int = 20,
    unbookmark_after: bool = False,
    dry_run: bool = False,
    *,
    bird_bin: Path = BIRD_BIN,
    runner=None,
) -> dict:
    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched": 0,
        "staged": 0,
        "skipped_duplicate": 0,
        "unbookmarked": 0,
        "errors": [],
        "staged_items": [],
        "dry_run": dry_run,
        "unbookmark_after": unbookmark_after,
    }

    bookmarks = fetch_bookmarks(count, bird_bin=bird_bin, runner=runner)
    summary["fetched"] = len(bookmarks)

    state = load_state()
    processed = set(state.get("processed_ids", []))

    for tweet in bookmarks:
        tid = str(tweet.get("id") or "")
        if not tid:
            summary["errors"].append({"tweet_id": None, "error": "missing id"})
            continue
        if tid in processed:
            summary["skipped_duplicate"] += 1
            continue
        try:
            url = _tweet_to_url(tweet)
            if dry_run:
                path = f"(dry-run — would stage {tid})"
            else:
                path = stage_tweet(tweet)
                processed.add(tid)
            summary["staged"] += 1
            summary["staged_items"].append({
                "tweet_id": tid,
                "url": url,
                "author": (tweet.get("author") or {}).get("username"),
                "text_preview": (tweet.get("text") or "")[:80],
                "inbox_path": path,
            })
            if unbookmark_after and not dry_run:
                try:
                    unbookmark(tid, bird_bin=bird_bin, runner=runner)
                    summary["unbookmarked"] += 1
                except Exception as e:  # unbookmark failure is non-fatal
                    summary["errors"].append({"tweet_id": tid, "error": f"unbookmark: {e}"})
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({"tweet_id": tid, "error": f"{type(e).__name__}: {e}"})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        state["processed_ids"] = sorted(processed)[-1000:]   # keep last 1000
        save_state(state)
    return summary


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Poll X bookmarks → Obsidian Inbox")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--unbookmark", action="store_true",
                     help="Remove bookmark on X after staging (default: off)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        summary = poll(count=args.count,
                       unbookmark_after=args.unbookmark,
                       dry_run=args.dry_run)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"X bookmarks poll  [{'dry-run' if args.dry_run else 'live'}]")
    print(f"  fetched:         {summary['fetched']}")
    print(f"  staged:          {summary['staged']}")
    print(f"  skipped-dup:     {summary['skipped_duplicate']}")
    print(f"  unbookmarked:    {summary['unbookmarked']}")
    print(f"  errors:          {len(summary['errors'])}")
    for item in summary["staged_items"]:
        print(f"    ✓ @{item['author']}: {item['text_preview']}")
        print(f"      → {item['inbox_path']}")
    for err in summary["errors"]:
        print(f"    ✗ {err['tweet_id']}: {err['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
