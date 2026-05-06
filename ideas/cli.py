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


@review.command("interactive")
@click.option("--source-type", default=None, type=click.Choice(SOURCE_TYPES),
              help="Filter to one source type (x-post, url, pdf, …).")
@click.option("--since-last-review", is_flag=True,
              help="Skip items captured before, or already deferred during, the last review session.")
@click.option("--limit", default=None, type=int, help="Stop after N items.")
@click.option("--no-mark", is_flag=True,
              help="Don't bump last_review_at on session start (use for dry-runs).")
def review_interactive_cmd(source_type: str | None, since_last_review: bool,
                            limit: int | None, no_mark: bool) -> None:
    """Interactive loop over pending Inbox items: [P]/[D]/[F]/[S]/[Q]."""
    from .storage import read_inbox_item

    last = rev_mod.get_last_review_at() if since_last_review else None
    last_iso = last.isoformat(timespec="seconds") if last else None

    items = []
    for path, fm in rev_mod.iter_pending(source_type=source_type):
        if since_last_review and last_iso:
            cap = fm.get("captured_at") or ""
            touched = fm.get("last_touched") or ""
            if (touched and touched > last_iso) or (cap and cap <= last_iso):
                continue
        items.append((path, fm))
        if limit and len(items) >= limit:
            break

    if not items:
        click.echo("Nothing to review. ✓")
        return

    if not no_mark:
        rev_mod.mark_review_now()

    click.echo(f"# Reviewing {len(items)} item(s)\n")
    for i, (path, fm) in enumerate(items, 1):
        click.echo("─" * 70)
        click.echo(f"[{i}/{len(items)}]  {path.name}")
        click.echo(f"  source: {fm.get('source_type','?')}  |  captured: {fm.get('captured_at','?')}")
        if fm.get("source_url"):
            click.echo(f"  url:    {fm['source_url']}")
        if fm.get("source_author"):
            click.echo(f"  author: {fm['source_author']}")
        if fm.get("title_hint"):
            click.echo(f"  title:  {fm['title_hint']}")
        if fm.get("defer_note"):
            click.echo(f"  prior defer note: {fm['defer_note']}")
        try:
            _, body = read_inbox_item(path)
        except Exception:
            body = ""
        preview = (body or "").strip().replace("\n", " ")[:200]
        if preview:
            click.echo(f"  preview: {preview}")
        click.echo()
        action = click.prompt("  [P]romote / [D]iscard / [F]efer / [S]kip / [Q]uit",
                              default="s", show_default=False).strip().lower()
        if action.startswith("q"):
            click.echo("\nStopping review.")
            return
        if action.startswith("p"):
            click.echo("  → To promote, run:")
            click.echo(f"     ideas promote {path} --category <cat> --title \"…\" \\")
            click.echo("       --summary \"…\" --tags \"a,b,c\"")
            click.echo("    (the assistant should now classify + run the promote command)")
        elif action.startswith("d"):
            dest = rev_mod.discard(path)
            click.echo(f"  ✓ discarded → {dest}")
        elif action.startswith("f"):
            note = click.prompt("    defer note (optional)", default="", show_default=False)
            rev_mod.defer(path, note=note)
            click.echo("  ✓ deferred (last_touched bumped)")
        else:
            click.echo("  ⊙ skipped")
    click.echo("\nReview session complete.")


@cli.command("ctl-status")
@click.option("--since-last-check", is_flag=True,
              help="Slice to threads opened/updated after the last ctl-status call.")
@click.option("--open-only", is_flag=True, help="Only open|partial threads.")
@click.option("--author", default=None, help="Filter to one author handle.")
@click.option("--no-mark", is_flag=True, help="Don't bump last_check_at.")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
def ctl_status_cmd(since_last_check: bool, open_only: bool, author: str | None,
                    no_mark: bool, as_json: bool) -> None:
    """CTL trade-thread status — what's new + updated + stale-open?"""
    from ideas import ctl_threads as ct
    out = ct.ctl_status(since_last_check=since_last_check,
                         open_only=open_only, author=author)
    if since_last_check and not no_mark:
        ct.mark_check_now()
    if as_json:
        click.echo(json.dumps(out, indent=2, default=str))
        return
    if since_last_check and out["since"]:
        click.echo(f"# CTL — since last check at {out['since']}")
    else:
        click.echo("# CTL — current status")
    t = out["totals"]
    click.echo(f"  totals: {t['new']} new / {t['updated']} updated / "
               f"{t['open_total']} open_total / {t['all']} all\n")

    def _fmt(thr):
        bits = [
            f"@{thr['author']:14s}",
            (thr['ticker'] or '?').ljust(7),
            (thr['direction'] or '?')[:1].upper(),
            f"E:{thr['current_entry_text'] or '—'}",
            f"S:{thr['current_stop_text'] or '—'}",
            f"T:{thr['current_target_text'] or '—'}",
        ]
        if thr.get("current_horizon"):
            bits.append(f"H:{thr['current_horizon']}")
        return "  ".join(bits)

    if out["new_threads"]:
        click.echo("NEW")
        for thr in out["new_threads"]:
            click.echo(f"  {_fmt(thr)}")
        click.echo()
    if out["updated_threads"]:
        click.echo("UPDATES (existing threads)")
        for thr in out["updated_threads"]:
            click.echo(f"  {_fmt(thr)}  ({thr['post_count']} posts)")
        click.echo()
    if out["open_no_recent"]:
        click.echo("OPEN, no update >24h")
        for thr in out["open_no_recent"]:
            click.echo(f"  {_fmt(thr)}  (last: {thr['last_update_at']})")


@cli.command("ctl-summary")
@click.option("--days", default=None, type=int, help="Look-back window (default: all-time).")
@click.option("--author", default=None, help="Filter to one author.")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
def ctl_summary_cmd(days: int | None, author: str | None, as_json: bool) -> None:
    """Per-author + per-ticker hit-rate / pnl analytics from trade_threads."""
    import duckdb
    from ideas import ctl_extractor as cext
    cext.ensure_schema()
    con = cext._con()
    try:
        wheres = []
        params = []
        if days:
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            wheres.append("opened_at >= ?")
            params.append(cutoff)
        if author:
            wheres.append("author = ?")
            params.append(author)
        where_clause = (" WHERE " + " AND ".join(wheres)) if wheres else ""

        # Recompute view-equivalents in code so we can apply the date+author filters
        rows_author = con.execute(f"""
            SELECT
                author,
                COUNT(*) AS thread_count,
                SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END) AS closed_threads,
                SUM(CASE WHEN state IN ('open','partial') THEN 1 ELSE 0 END) AS open_threads,
                ROUND(100.0 * SUM(CASE WHEN state = 'closed' AND closed_pnl_pct > 0 THEN 1 ELSE 0 END)
                              / NULLIF(SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END), 0), 1) AS win_pct,
                ROUND(AVG(CASE WHEN state = 'closed' THEN closed_pnl_pct END), 3) AS avg_pnl,
                ROUND(AVG(CASE WHEN state = 'closed' THEN days_held END), 2) AS avg_days_held
            FROM trade_threads
            {where_clause}
            GROUP BY author
            ORDER BY thread_count DESC
        """, params).fetchall()

        rows_ticker = con.execute(f"""
            SELECT ticker, direction,
                   COUNT(*) AS n,
                   SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END) AS closed_n,
                   ROUND(AVG(CASE WHEN state = 'closed' THEN closed_pnl_pct END), 3) AS avg_pnl
            FROM trade_threads
            {where_clause}
            GROUP BY ticker, direction
            HAVING COUNT(*) > 0
            ORDER BY n DESC LIMIT 25
        """, params).fetchall()
    finally:
        con.close()

    payload = {
        "filter": {"days": days, "author": author},
        "by_author": [
            {"author": r[0], "threads": r[1], "closed": r[2], "open": r[3],
             "win_pct": r[4], "avg_pnl_pct": r[5], "avg_days_held": r[6]}
            for r in rows_author
        ],
        "by_ticker": [
            {"ticker": r[0], "direction": r[1], "threads": r[2],
             "closed": r[3], "avg_pnl_pct": r[4]}
            for r in rows_ticker
        ],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    click.echo(f"# CTL summary  filter={payload['filter']}\n")
    click.echo("By author:")
    click.echo(f"  {'author':16s} {'threads':>8s} {'closed':>8s} {'open':>6s} {'win%':>6s} {'avg_pnl%':>10s} {'avg_days':>10s}")
    for r in payload["by_author"]:
        click.echo(f"  {r['author']:16s} {r['threads']:>8d} {r['closed']:>8d} {r['open']:>6d} "
                    f"{(r['win_pct'] or 0):>5.1f}% {(r['avg_pnl_pct'] or 0):>9.3f}% {(r['avg_days_held'] or 0):>9.2f}")
    click.echo("\nTop 25 tickers (by thread count):")
    click.echo(f"  {'ticker':10s} {'dir':6s} {'threads':>8s} {'closed':>8s} {'avg_pnl%':>10s}")
    for r in payload["by_ticker"]:
        click.echo(f"  {r['ticker']:10s} {(r['direction'] or '?'):6s} {r['threads']:>8d} "
                    f"{r['closed']:>8d} {(r['avg_pnl_pct'] or 0):>9.3f}%")


@cli.command("ctl-enrich")
@click.option("--json", "as_json", is_flag=True, help="JSON output.")
def ctl_enrich_cmd(as_json: bool) -> None:
    """Walk all open|partial threads, compute MFE/MAE/pnl from firstrate,
    auto-close on stop/target hit, mark stale ones."""
    from ideas import ctl_outcomes
    summary = ctl_outcomes.enrich_all_open()
    if as_json:
        click.echo(json.dumps(summary, indent=2, default=str))
        return
    click.echo("# CTL outcome enrichment")
    click.echo(f"  open threads:      {summary['total_open']}")
    click.echo(f"  enriched (got data): {summary['enriched']}")
    click.echo(f"  auto-closed:       {summary['auto_closed']}")
    click.echo(f"  marked stale:      {summary['marked_stale']}")
    click.echo(f"  futures (skipped): {summary['skipped_futures']}")
    click.echo(f"  no_data:           {summary['no_data']}")
    if summary["errors"]:
        click.echo(f"  errors:            {len(summary['errors'])}")


@cli.command("inbox-status")
@click.option("--since-last-review", is_flag=True,
              help="Slice into 'new since last review' / 'deferred' / 'untouched'.")
@click.option("--source-type", default=None, type=click.Choice(SOURCE_TYPES),
              help="Filter to one source type (x-post, url, pdf, …).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def inbox_status_cmd(since_last_review: bool, source_type: str | None, as_json: bool) -> None:
    """Session-start surfacing — 'you have N new bookmarks since last review …'.

    Designed for the session-start protocol in CLAUDE.md to call. JSON output
    is stable for scripting.
    """
    s = rev_mod.inbox_status(since_last_review=since_last_review,
                              source_type=source_type)
    if as_json:
        click.echo(json.dumps(s, indent=2))
        return
    if since_last_review:
        if s["since"]:
            click.echo(f"# Since last review at {s['since']}")
            click.echo(f"  new:        {s['new']}")
            click.echo(f"  deferred:   {s['deferred']}  (touched but not promoted)")
            click.echo(f"  untouched:  {s['untouched']}")
            click.echo(f"  total:      {s['total']}")
        else:
            click.echo("# No prior review session recorded.")
            click.echo(f"  total pending: {s['total']}")
    else:
        click.echo(f"# Inbox status — {s['total']} pending")
    if s["by_source"]:
        click.echo("  by source (new):")
        for st, n in sorted(s["by_source"].items(), key=lambda x: -x[1]):
            click.echo(f"    {st:12s}  {n}")


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
