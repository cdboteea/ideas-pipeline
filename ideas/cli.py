"""`ideas` CLI — capture / review / promote."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import click

from . import capture as cap_mod
from . import promote as prom_mod
from . import review as rev_mod
from .config import CATEGORIES, SOURCE_TYPES, ensure_dirs


@click.group()
def cli() -> None:
    """CC idea-capture and promote pipeline."""
    ensure_dirs()


# ── capture ──────────────────────────────────────────────────────────────────


@cli.group()
def stage() -> None:
    """Write an Inbox stub (normally called by the assistant inline)."""


@stage.command("url")
@click.argument("url")
@click.option("--title", required=True, help="Short title hint")
@click.option("--preview", default="", help="Preview text / excerpt")
@click.option("--session-ref", default=None)
def stage_url_cmd(url: str, title: str, preview: str, session_ref: str | None) -> None:
    path = cap_mod.stage_url(url=url, title_hint=title, preview=preview, session_ref=session_ref)
    click.echo(f"staged: {path}")


@stage.command("x")
@click.argument("url")
@click.option("--author", required=True)
@click.option("--content", required=True)
@click.option("--session-ref", default=None)
def stage_x_cmd(url: str, author: str, content: str, session_ref: str | None) -> None:
    path = cap_mod.stage_x_post(url=url, author=author, content=content, session_ref=session_ref)
    click.echo(f"staged: {path}")


@stage.command("pdf")
@click.argument("pdf_path")
@click.option("--title", required=True)
@click.option("--text", default="", help="Extracted text")
@click.option("--session-ref", default=None)
def stage_pdf_cmd(pdf_path: str, title: str, text: str, session_ref: str | None) -> None:
    path = cap_mod.stage_pdf(pdf_path=pdf_path, title_hint=title, extracted_text=text, session_ref=session_ref)
    click.echo(f"staged: {path}")


@stage.command("thought")
@click.argument("text")
@click.option("--title", default="")
@click.option("--session-ref", default=None)
def stage_thought_cmd(text: str, title: str, session_ref: str | None) -> None:
    path = cap_mod.stage_thought(text=text, title_hint=title, session_ref=session_ref)
    click.echo(f"staged: {path}")


@stage.command("research")
@click.argument("title")
@click.option("--content-file", type=click.Path(exists=True), help="File containing the research")
@click.option("--content", default="", help="Inline content (alt to --content-file)")
@click.option("--session-ref", default=None)
def stage_research_cmd(title: str, content_file: str | None, content: str, session_ref: str | None) -> None:
    if content_file:
        content = Path(content_file).read_text(encoding="utf-8")
    if not content:
        raise click.UsageError("Provide --content-file or --content.")
    path = cap_mod.stage_research_output(title=title, content=content, session_ref=session_ref)
    click.echo(f"staged: {path}")


# ── review ───────────────────────────────────────────────────────────────────


@cli.group()
def review() -> None:
    """Review pending Inbox items."""


@review.command("inbox")
@click.option("--since", default=None, help="YYYY-MM-DD; only items captured on/after this date")
@click.option("--source", "source_type", default=None, type=click.Choice(SOURCE_TYPES))
@click.option("--tag", "tag_filter", default=None)
@click.option("--json", "as_json", is_flag=True, help="Output JSON for programmatic use")
def review_inbox_cmd(since: str | None, source_type: str | None, tag_filter: str | None, as_json: bool) -> None:
    items = list(rev_mod.iter_pending(since=since, source_type=source_type, tag_filter=tag_filter))
    if as_json:
        payload = [
            {"path": str(p), **{k: v for k, v in fm.items() if k != "raw_content"}}
            for p, fm in items
        ]
        click.echo(json.dumps(payload, indent=2))
        return
    if not items:
        click.echo("No pending items.")
        return
    click.echo(f"{len(items)} pending item(s):\n")
    for i, (path, fm) in enumerate(items, 1):
        click.echo(f"{i:>3}. [{fm.get('source_type'):<15}] {fm.get('id')}")
        if fm.get("source_url"):
            click.echo(f"      url: {fm['source_url']}")
        preview = (fm.get("preview") or "").replace("\n", " ")[:120]
        if preview:
            click.echo(f"      {preview}")
        click.echo(f"      path: {path}")
        click.echo()


@review.command("summary")
def review_summary_cmd() -> None:
    summary = rev_mod.inbox_summary()
    click.echo(json.dumps(summary, indent=2))


@review.command("discard")
@click.argument("inbox_path", type=click.Path(exists=True))
def review_discard_cmd(inbox_path: str) -> None:
    dest = rev_mod.discard(inbox_path)
    click.echo(f"archived (discarded): {dest}")


@review.command("defer")
@click.argument("inbox_path", type=click.Path(exists=True))
@click.option("--note", default="")
def review_defer_cmd(inbox_path: str, note: str) -> None:
    rev_mod.defer(inbox_path, note=note)
    click.echo(f"deferred: {inbox_path}")


@review.command("auto-archive")
def review_auto_archive_cmd() -> None:
    archived = rev_mod.auto_archive_expired()
    click.echo(f"auto-archived {len(archived)} item(s)")
    for p in archived:
        click.echo(f"  {p}")


# ── promote ──────────────────────────────────────────────────────────────────


@cli.command("promote")
@click.argument("inbox_path", type=click.Path(exists=True))
@click.option("--category", required=True, type=click.Choice(CATEGORIES))
@click.option("--title", required=True)
@click.option("--summary", required=True, help="One-line summary, <25 words")
@click.option("--tags", required=True, help="Comma-separated tag list")
@click.option("--key-points", default="", help="Pipe-separated list: 'a|b|c'")
@click.option("--why", default="")
@click.option("--action-items", default="", help="Pipe-separated")
@click.option("--related", default="", help="Comma-separated wikilink IDs")
@click.option("--source-note", default="")
@click.option("--supersedes", default=None)
def promote_cmd(
    inbox_path: str,
    category: str,
    title: str,
    summary: str,
    tags: str,
    key_points: str,
    why: str,
    action_items: str,
    related: str,
    source_note: str,
    supersedes: str | None,
) -> None:
    result = prom_mod.promote(
        inbox_path=inbox_path,
        category=category,
        title=title,
        summary=summary,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        key_points=[p.strip() for p in key_points.split("|") if p.strip()],
        why_this_matters=why,
        action_items=[a.strip() for a in action_items.split("|") if a.strip()],
        related=[r.strip() for r in related.split(",") if r.strip()],
        source_note=source_note,
        supersedes=supersedes,
    )
    click.echo(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


@cli.command("promote-direct")
@click.option("--title", required=True)
@click.option("--category", required=True, type=click.Choice(CATEGORIES))
@click.option("--summary", required=True)
@click.option("--source-type", required=True, type=click.Choice(SOURCE_TYPES))
@click.option("--tags", required=True)
@click.option("--source-url", default=None)
@click.option("--source-author", default=None)
@click.option("--raw-content", default="")
@click.option("--key-points", default="")
@click.option("--why", default="")
@click.option("--action-items", default="")
@click.option("--related", default="")
@click.option("--source-note", default="")
@click.option("--session-ref", default=None)
def promote_direct_cmd(
    title: str,
    category: str,
    summary: str,
    source_type: str,
    tags: str,
    source_url: str | None,
    source_author: str | None,
    raw_content: str,
    key_points: str,
    why: str,
    action_items: str,
    related: str,
    source_note: str,
    session_ref: str | None,
) -> None:
    """Direct promote (bypass Inbox) — when user uses !idea / !idb."""
    result = prom_mod.promote_direct(
        title=title,
        category=category,
        summary=summary,
        source_type=source_type,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        source_url=source_url,
        source_author=source_author,
        raw_content=raw_content,
        key_points=[p.strip() for p in key_points.split("|") if p.strip()],
        why_this_matters=why,
        action_items=[a.strip() for a in action_items.split("|") if a.strip()],
        related=[r.strip() for r in related.split(",") if r.strip()],
        source_note=source_note,
        session_ref=session_ref,
    )
    click.echo(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    cli()
