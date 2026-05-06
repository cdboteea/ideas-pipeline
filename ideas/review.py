"""Review: interactive Inbox review — promote / discard / defer / edit-and-promote."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
import re

from .config import INBOX, AUTO_ARCHIVE_DAYS
from .storage import list_inbox, read_inbox_item, move_to_archive, _load_md, _write_md


# Tracks when the user last opened a review session. Used by
# `ideas inbox-status --since-last-review` and the session-start surfacing
# primitive — items captured AFTER this timestamp count as "new since last
# review", items deferred AFTER count as "discussed but not promoted".
LAST_REVIEW_FILE = Path.home() / "clawd" / "data" / "ideas-last-review.json"


def get_last_review_at() -> Optional[datetime]:
    """Return the timestamp of the user's last review-session start, or None."""
    if not LAST_REVIEW_FILE.exists():
        return None
    try:
        data = json.loads(LAST_REVIEW_FILE.read_text())
        ts = data.get("last_review_at")
        if not ts:
            return None
        return datetime.fromisoformat(ts)
    except (ValueError, json.JSONDecodeError):
        return None


def mark_review_now(state_file: Path = None) -> datetime:
    """Update LAST_REVIEW_FILE to 'right now'. Called when an interactive
    review session begins."""
    target = state_file or LAST_REVIEW_FILE
    now = datetime.now().astimezone()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "last_review_at": now.isoformat(timespec="seconds"),
    }, indent=2))
    return now


def iter_pending(
    since: Optional[str] = None,
    source_type: Optional[str] = None,
    tag_filter: Optional[str] = None,
) -> Iterator[tuple[Path, dict]]:
    """Yield (path, frontmatter) for pending items matching filters."""
    for path in list_inbox(status="pending"):
        fm, _ = read_inbox_item(path)
        if since and fm.get("captured_at", "") < since:
            continue
        if source_type and fm.get("source_type") != source_type:
            continue
        if tag_filter:
            tags = fm.get("tags_proposed") or fm.get("tags") or []
            if tag_filter not in tags:
                continue
        yield path, fm


def inbox_summary() -> dict:
    """Return counts by source_type + oldest/newest timestamps."""
    counts: dict[str, int] = {}
    oldest = None
    newest = None
    total = 0
    for path, fm in iter_pending():
        total += 1
        st = fm.get("source_type", "unknown")
        counts[st] = counts.get(st, 0) + 1
        cap = fm.get("captured_at")
        if cap:
            if oldest is None or cap < oldest:
                oldest = cap
            if newest is None or cap > newest:
                newest = cap
    return {
        "total_pending": total,
        "by_source_type": counts,
        "oldest": oldest,
        "newest": newest,
    }


def inbox_status(since_last_review: bool = False,
                  source_type: Optional[str] = None,
                  state_file: Path = None) -> dict:
    """
    Returns a session-start-friendly summary of the Inbox:

      {
        "new":        N,    # captured AFTER last_review_at (or all, if no last review yet)
        "deferred":   N,    # last_touched AFTER last_review_at (we discussed it, no decision yet)
        "untouched":  N,    # captured BEFORE last_review_at AND no last_touched (truly stale)
        "total":      N,
        "since":      ISO timestamp of the divider, or null if no last review,
        "by_source":  { "x-post": ..., "url": ..., ... }   # for "new" only
      }

    When `since_last_review=False`, returns total Inbox state with no slicing.
    """
    last = get_last_review_at() if since_last_review else None
    last_iso = last.isoformat(timespec="seconds") if last else None

    counters = {"new": 0, "deferred": 0, "untouched": 0, "total": 0}
    by_source: dict[str, int] = {}

    for path, fm in iter_pending(source_type=source_type):
        counters["total"] += 1
        cap = fm.get("captured_at") or ""
        touched = fm.get("last_touched") or ""

        if not since_last_review or not last:
            counters["new"] += 1
            st = fm.get("source_type", "unknown")
            by_source[st] = by_source.get(st, 0) + 1
            continue

        # With a last_review_at divider:
        captured_after = cap > last_iso if cap else False
        touched_after = touched > last_iso if touched else False

        if captured_after:
            counters["new"] += 1
            st = fm.get("source_type", "unknown")
            by_source[st] = by_source.get(st, 0) + 1
        elif touched_after:
            counters["deferred"] += 1
        else:
            counters["untouched"] += 1

    return {
        "new":        counters["new"],
        "deferred":   counters["deferred"],
        "untouched":  counters["untouched"],
        "total":      counters["total"],
        "since":      last_iso,
        "by_source":  by_source,
    }


def discard(inbox_path: Path | str) -> Path:
    """Move inbox item directly to Archive/ with status: discarded."""
    return move_to_archive(Path(inbox_path), status="discarded")


def defer(inbox_path: Path | str, note: str = "") -> None:
    """Bump last_touched; optionally append a defer note. Stays in Inbox."""
    p = Path(inbox_path)
    fm, body = read_inbox_item(p)
    fm["last_touched"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if note:
        fm["defer_note"] = note
    _write_md(p, fm, body)


def auto_archive_expired() -> list[Path]:
    """
    Move pending items older than AUTO_ARCHIVE_DAYS to Archive/ with status:
    auto-archived. Called by the daily launchd cron.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_ARCHIVE_DAYS)
    archived = []
    for path, fm in iter_pending():
        cap = fm.get("last_touched") or fm.get("captured_at")
        if not cap:
            continue
        try:
            cap_dt = datetime.fromisoformat(cap)
        except ValueError:
            continue
        if cap_dt.astimezone(timezone.utc) < cutoff:
            dest = move_to_archive(path, status="auto-archived")
            archived.append(dest)
    return archived
