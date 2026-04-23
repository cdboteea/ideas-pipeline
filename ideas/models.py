"""Data models for Inbox + Ideas frontmatter.

Schemas match the conventions in:
  - ~/Documents/ObsidianVault/_meta/README.md
  - ~/Documents/ObsidianVault/_meta/classification-guide.md
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import hashlib
import re
import unicodedata


def _slugify(text: str, max_words: int = 8) -> str:
    """3-8 word kebab-case slug per classification-guide §3."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    words = text.split()
    stopwords = {"the", "a", "an", "of", "to", "for", "on", "in", "and", "or"}
    words = [w for w in words if w not in stopwords]
    if len(words) > max_words:
        words = words[:max_words]
    slug = "-".join(words)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80] or "untitled"


def _sha256(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


@dataclass
class InboxItem:
    id: str
    captured_at: str
    source_type: str
    source_url: Optional[str] = None
    source_author: Optional[str] = None
    status: str = "pending"
    captured_by: str = "cc-session"
    session_ref: Optional[str] = None
    raw_content: str = ""
    preview: str = ""

    @classmethod
    def make(
        cls,
        title_hint: str,
        source_type: str,
        raw_content: str = "",
        source_url: Optional[str] = None,
        source_author: Optional[str] = None,
        session_ref: Optional[str] = None,
        captured_by: str = "cc-session",
    ) -> "InboxItem":
        now = datetime.now().astimezone()
        date = now.strftime("%Y-%m-%d")
        slug = _slugify(title_hint)
        item_id = f"{date}-{slug}"
        preview = (raw_content or title_hint)[:300].strip()
        return cls(
            id=item_id,
            captured_at=now.isoformat(timespec="seconds"),
            source_type=source_type,
            source_url=source_url,
            source_author=source_author,
            status="pending",
            captured_by=captured_by,
            session_ref=session_ref,
            raw_content=raw_content,
            preview=preview,
        )

    def content_hash(self) -> str:
        return _sha256(self.raw_content or self.preview)

    def frontmatter(self) -> dict:
        d = asdict(self)
        # put multiline fields at bottom for readability
        d.pop("raw_content", None)
        d.pop("preview", None)
        d["source_hash"] = self.content_hash()
        return d


@dataclass
class IdeaNote:
    id: str
    title: str
    captured_at: str
    promoted_at: str
    category: str
    tags: list[str] = field(default_factory=list)
    source_type: str = "thought"
    source_url: Optional[str] = None
    source_author: Optional[str] = None
    source_hash: Optional[str] = None
    related: list[str] = field(default_factory=list)
    supersedes: Optional[str] = None
    status: str = "active"
    summary: str = ""

    def frontmatter(self) -> dict:
        return asdict(self)
