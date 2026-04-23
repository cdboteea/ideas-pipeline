"""Promote: move an Inbox stub → Ideas/<category>/ with full classification.

Per _meta/classification-guide.md. Enforces frontmatter schema; the assistant
is responsible for the actual classification decision (category + tags +
summary + related), this module just writes the result correctly.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import CATEGORIES
from .models import IdeaNote
from .storage import (
    INBOX,
    IDEAS,
    read_inbox_item,
    write_idea,
    move_to_archive,
    find_duplicate_by_hash,
    find_duplicate_by_url,
)


SUMMARY_TEMPLATE = """# {title}

**One-line:** {summary}

**Key points:**
{key_points}

**Why this matters to us:**
{why_this_matters}

**Action items:**
{action_items}

**Source:**
{source_note}

---

*Related:* {related_inline}
"""


def _format_key_points(points: list[str]) -> str:
    if not points:
        return "- (none)"
    return "\n".join(f"- {p}" for p in points)


def _format_action_items(actions: list[str]) -> str:
    if not actions:
        return "- [ ] None — reference only"
    return "\n".join(f"- [ ] {a}" for a in actions)


def _format_related(related: list[str]) -> str:
    if not related:
        return "_(none)_"
    return ", ".join(f"[[{r.strip('[]')}]]" for r in related)


def promote(
    inbox_path: Path | str,
    category: str,
    title: str,
    summary: str,
    tags: list[str],
    key_points: Optional[list[str]] = None,
    why_this_matters: str = "",
    action_items: Optional[list[str]] = None,
    related: Optional[list[str]] = None,
    source_note: str = "",
    supersedes: Optional[str] = None,
    status: str = "active",
) -> dict:
    """
    Promote an inbox item to Ideas/<category>/.

    Returns: dict with keys: success, path, duplicate_of, message.
    """
    inbox_path = Path(inbox_path)
    if not inbox_path.exists():
        return {"success": False, "path": None, "message": f"Inbox item not found: {inbox_path}"}

    if category not in CATEGORIES:
        return {
            "success": False,
            "path": None,
            "message": f"Invalid category {category!r}. Must be one of {CATEGORIES}",
        }

    fm, _body = read_inbox_item(inbox_path)
    if fm.get("status") != "pending":
        return {
            "success": False,
            "path": None,
            "message": f"Inbox item status is {fm.get('status')!r}, not pending. Use defer then promote.",
        }

    # Dedup checks per classification-guide §6
    source_hash = fm.get("source_hash")
    source_url = fm.get("source_url")
    dup_path = None
    if source_hash:
        dup_path = find_duplicate_by_hash(source_hash)
    if not dup_path and source_url:
        dup_path = find_duplicate_by_url(source_url)
    if dup_path:
        return {
            "success": False,
            "path": str(dup_path),
            "duplicate_of": str(dup_path),
            "message": f"Duplicate of existing note: {dup_path}. Merge manually per guide §6.",
        }

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    note = IdeaNote(
        id=fm["id"],
        title=title,
        captured_at=fm.get("captured_at", now),
        promoted_at=now,
        category=category,
        tags=tags,
        source_type=fm.get("source_type", "thought"),
        source_url=fm.get("source_url"),
        source_author=fm.get("source_author"),
        source_hash=source_hash,
        related=related or [],
        supersedes=supersedes,
        status=status,
        summary=summary,
    )

    body = SUMMARY_TEMPLATE.format(
        title=title,
        summary=summary,
        key_points=_format_key_points(key_points or []),
        why_this_matters=why_this_matters or "_(tbd)_",
        action_items=_format_action_items(action_items or []),
        source_note=source_note or (source_url or "(in-conversation)"),
        related_inline=_format_related(related or []),
    )

    idea_path = write_idea(note, body)

    # Move original inbox stub to Archive with status: promoted
    archive_path = move_to_archive(inbox_path, status="promoted")

    return {
        "success": True,
        "path": str(idea_path),
        "archived_stub": str(archive_path),
        "message": f"Promoted to {idea_path}",
    }


def promote_direct(
    title: str,
    category: str,
    summary: str,
    source_type: str,
    tags: list[str],
    source_url: Optional[str] = None,
    source_author: Optional[str] = None,
    raw_content: str = "",
    key_points: Optional[list[str]] = None,
    why_this_matters: str = "",
    action_items: Optional[list[str]] = None,
    related: Optional[list[str]] = None,
    source_note: str = "",
    session_ref: Optional[str] = None,
) -> dict:
    """
    Direct promote — when user uses !idea / !idb / explicit natural phrasing.
    Skips the Inbox entirely. Creates the stub, archives it, writes the Idea.
    """
    from .capture import InboxItem
    from .storage import write_inbox

    item = InboxItem.make(
        title_hint=title,
        source_type=source_type,
        raw_content=raw_content or summary,
        source_url=source_url,
        source_author=source_author,
        session_ref=session_ref,
        captured_by="cc-session-direct-promote",
    )
    inbox_path = write_inbox(item)

    return promote(
        inbox_path=inbox_path,
        category=category,
        title=title,
        summary=summary,
        tags=tags,
        key_points=key_points,
        why_this_matters=why_this_matters,
        action_items=action_items,
        related=related,
        source_note=source_note,
    )
