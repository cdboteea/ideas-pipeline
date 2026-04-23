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


def stage_email(
    subject: str,
    sender: str,
    body: str,
    *,
    message_id: Optional[str] = None,
    received_at: Optional[str] = None,
    session_ref: Optional[str] = None,
) -> str:
    """Stage a Gmail message tagged ToStage → Obsidian Inbox.

    Called by the poller (scripts/poll_gmail_tostage.py) on every Gmail
    message with the 'ToStage' label. After a successful stage, the poller
    strips the ToStage label so the message is not re-processed.

    `message_id` is stored in the inbox item's source_url so a later pass
    can look up the original thread.
    """
    source_url = f"gmail://{message_id}" if message_id else ""
    title_hint = subject.strip() or f"(no subject) — {sender}"
    extras = []
    if sender:
        extras.append(f"From: {sender}")
    if received_at:
        extras.append(f"Received: {received_at}")
    if message_id:
        extras.append(f"Gmail-ID: {message_id}")
    header = "\n".join(extras)
    full_body = f"{header}\n\n{body}" if header else body

    item = InboxItem.make(
        title_hint=title_hint,
        source_type="email",
        raw_content=full_body,
        source_url=source_url,
        source_author=sender,
        captured_by="gmail-tostage-poller",
        session_ref=session_ref,
    )
    path = write_inbox(item)
    return str(path)
