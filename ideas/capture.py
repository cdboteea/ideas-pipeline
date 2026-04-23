"""Capture: write inbox stubs.

The assistant (me) calls these when a capture trigger fires during a CC
conversation. No auto-detection magic — explicit call with the right metadata.
"""
from __future__ import annotations
from typing import Optional

from .models import InboxItem
from .storage import write_inbox


def stage_url(
    url: str,
    title_hint: str,
    preview: str = "",
    session_ref: Optional[str] = None,
) -> str:
    """Stage a pasted URL (article, page, doc) to Inbox."""
    item = InboxItem.make(
        title_hint=title_hint or url,
        source_type="url",
        raw_content=preview or url,
        source_url=url,
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)


def stage_x_post(
    url: str,
    author: str,
    content: str,
    session_ref: Optional[str] = None,
) -> str:
    item = InboxItem.make(
        title_hint=f"{author} — {content[:60]}",
        source_type="x-post",
        raw_content=content,
        source_url=url,
        source_author=author,
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)


def stage_pdf(
    pdf_path: str,
    title_hint: str,
    extracted_text: str = "",
    session_ref: Optional[str] = None,
) -> str:
    item = InboxItem.make(
        title_hint=title_hint,
        source_type="pdf",
        raw_content=extracted_text or f"(PDF at {pdf_path}, not extracted)",
        source_url=f"file://{pdf_path}",
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)


def stage_thought(
    text: str,
    title_hint: str = "",
    session_ref: Optional[str] = None,
) -> str:
    """User paragraph-length message → Inbox."""
    item = InboxItem.make(
        title_hint=title_hint or text[:60],
        source_type="thought",
        raw_content=text,
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)


def stage_research_output(
    title: str,
    content: str,
    session_ref: Optional[str] = None,
) -> str:
    """Assistant-generated research worth referencing later."""
    item = InboxItem.make(
        title_hint=title,
        source_type="research-output",
        raw_content=content,
        captured_by="cc-session",
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)
