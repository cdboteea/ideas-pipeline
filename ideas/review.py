"""Review: interactive Inbox review — promote / discard / defer / edit-and-promote."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
import re

from .config import INBOX, AUTO_ARCHIVE_DAYS
from .storage import list_inbox, read_inbox_item, move_to_archive, _load_md, _write_md


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
