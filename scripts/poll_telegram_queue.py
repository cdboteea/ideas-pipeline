#!/usr/bin/env python3
"""
Telegram-queue drainer → Obsidian Inbox.

Design rationale: OpenClaw already holds the Telegram `getUpdates` long-poll
for `@matiasassistantbot`. A second consumer would steal messages. Instead,
this poller drains a JSONL queue that ANY source can append to:

  - OpenClaw Telegram handler (when user says "!idea" or "/stage")
  - Webhook receiver (future)
  - Manual `echo '{...}' >> queue.jsonl`

Queue location: ~/clawd/data/telegram-queue.jsonl
Each line: {
    "text": "full message body",
    "sender": "Matias",
    "chat_id": 8463750100,
    "message_id": 12345,
    "received_at": "2026-04-23T17:20:00Z"
}

On each run: drain the queue, stage each entry to Obsidian Inbox,
rename-and-truncate the file (so concurrent writers don't lose new messages
during the drain).

Usage:
    poll_telegram_queue.py [--queue <path>] [--dry-run] [--json]
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas.capture import stage_telegram


DEFAULT_QUEUE = Path(os.environ.get(
    "TELEGRAM_QUEUE_FILE",
    str(Path.home() / "clawd" / "data" / "telegram-queue.jsonl"),
)).expanduser()


_EMPTY = object()  # sentinel: empty line (distinguish from malformed)


def _parse_line(line: str):
    line = line.strip()
    if not line:
        return _EMPTY
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def drain_queue(queue: Path, dry_run: bool = False) -> dict:
    """Atomically claim the current queue file, then drain.

    Writers who append after we rename will see an empty/new file — no
    messages are lost. The rename is O(1) on the same filesystem.
    """
    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "queue": str(queue),
        "found": 0,
        "staged": 0,
        "skipped_malformed": 0,
        "errors": [],
        "staged_items": [],
        "dry_run": dry_run,
    }

    if not queue.exists() or queue.stat().st_size == 0:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return summary

    # Atomically rename the queue aside so writers don't race us
    if dry_run:
        working = queue  # read in place
    else:
        working_dir = queue.parent
        working_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{queue.stem}-claim-",
            suffix=queue.suffix,
            dir=str(working_dir),
        )
        os.close(fd)
        working = Path(tmp_name)
        try:
            os.rename(str(queue), str(working))
        except FileNotFoundError:
            working.unlink(missing_ok=True)
            return summary

    # Read & stage each line
    try:
        with working.open("r", encoding="utf-8") as f:
            for line in f:
                entry = _parse_line(line)
                if entry is _EMPTY:
                    continue                       # blank line — silent skip
                if entry is None:
                    summary["skipped_malformed"] += 1
                    continue
                summary["found"] += 1
                try:
                    if dry_run:
                        inbox_path = f"(dry-run — would stage)"
                    else:
                        inbox_path = stage_telegram(
                            text=str(entry.get("text", "")),
                            sender=str(entry.get("sender", "anonymous")),
                            chat_id=entry.get("chat_id"),
                            received_at=entry.get("received_at"),
                            message_id=entry.get("message_id"),
                        )
                    summary["staged"] += 1
                    summary["staged_items"].append({
                        "sender": entry.get("sender"),
                        "chat_id": entry.get("chat_id"),
                        "text_preview": str(entry.get("text", ""))[:80],
                        "inbox_path": inbox_path,
                    })
                except Exception as e:  # noqa: BLE001
                    summary["errors"].append({
                        "message_id": entry.get("message_id"),
                        "error": f"{type(e).__name__}: {e}",
                    })
    finally:
        if not dry_run:
            # Delete the claimed queue (everything processed, or errors are
            # recorded — we don't re-drive).
            working.unlink(missing_ok=True)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Drain Telegram message queue → Obsidian Inbox")
    ap.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        summary = drain_queue(args.queue.expanduser(), dry_run=args.dry_run)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"Telegram queue drain  [{'dry-run' if args.dry_run else 'live'}]")
    print(f"  queue:           {summary['queue']}")
    print(f"  found:           {summary['found']}  "
          f"staged: {summary['staged']}  "
          f"malformed: {summary['skipped_malformed']}  "
          f"errors: {len(summary['errors'])}")
    for item in summary["staged_items"]:
        print(f"    ✓ @{item['sender']}: {item['text_preview']}")
        print(f"      → {item['inbox_path']}")
    for err in summary["errors"]:
        print(f"    ✗ {err.get('message_id')}: {err['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
