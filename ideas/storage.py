"""Read/write markdown + YAML frontmatter in the vault."""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import re
import yaml

from .config import INBOX, IDEAS, ARCHIVE, CATEGORIES, ensure_dirs
from .models import InboxItem, IdeaNote


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


def _load_md(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2)


def _write_md(path: Path, frontmatter: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = "---\n" + _dump_yaml(frontmatter) + "---\n\n" + body.lstrip()
    path.write_text(out, encoding="utf-8")


def write_inbox(item: InboxItem) -> Path:
    """Write an inbox stub. Idempotent on id collision (returns existing path)."""
    ensure_dirs()
    path = INBOX / f"{item.id}.md"
    if path.exists():
        return path
    fm = item.frontmatter()
    body_parts = []
    if item.raw_content:
        body_parts.append("## Raw content\n\n" + item.raw_content.strip())
    if item.preview and item.preview != item.raw_content:
        body_parts.append("## Preview\n\n" + item.preview.strip())
    body = "\n\n".join(body_parts) if body_parts else "(no content captured)\n"
    _write_md(path, fm, body)
    return path


def write_idea(note: IdeaNote, body_markdown: str) -> Path:
    """Write a promoted idea note to Ideas/<category>/."""
    ensure_dirs()
    if note.category not in CATEGORIES:
        raise ValueError(f"Invalid category {note.category!r}. Must be in {CATEGORIES}")
    path = IDEAS / note.category / f"{note.id}.md"
    if path.exists():
        raise FileExistsError(f"Idea already exists: {path}")
    _write_md(path, note.frontmatter(), body_markdown)
    return path


def list_inbox(status: str = "pending") -> list[Path]:
    """List inbox items matching status (default pending). Sorted oldest first."""
    if not INBOX.exists():
        return []
    items = []
    for path in sorted(INBOX.glob("*.md")):
        fm, _ = _load_md(path)
        if fm.get("status") == status:
            items.append(path)
    return items


def read_inbox_item(path: Path) -> tuple[dict, str]:
    return _load_md(path)


def move_to_archive(path: Path, status: str) -> Path:
    """Move inbox item to Archive/YYYY-MM/ with updated status."""
    from datetime import datetime
    if not path.exists():
        raise FileNotFoundError(path)
    fm, body = _load_md(path)
    fm["status"] = status
    fm["archived_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    # bucket by captured_at's YYYY-MM, fallback to now
    bucket = (fm.get("captured_at") or datetime.now().isoformat())[:7]
    dest_dir = ARCHIVE / bucket
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    _write_md(dest, fm, body)
    path.unlink()
    return dest


def find_duplicate_by_hash(source_hash: str) -> Optional[Path]:
    """Search all of Ideas/ for an existing note with the same source_hash."""
    if not IDEAS.exists():
        return None
    for cat_dir in IDEAS.iterdir():
        if not cat_dir.is_dir():
            continue
        for path in cat_dir.glob("*.md"):
            fm, _ = _load_md(path)
            if fm.get("source_hash") == source_hash:
                return path
    return None


def find_duplicate_by_url(source_url: str) -> Optional[Path]:
    if not source_url or not IDEAS.exists():
        return None
    for cat_dir in IDEAS.iterdir():
        if not cat_dir.is_dir():
            continue
        for path in cat_dir.glob("*.md"):
            fm, _ = _load_md(path)
            if fm.get("source_url") == source_url:
                return path
    return None
