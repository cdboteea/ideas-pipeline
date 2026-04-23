#!/usr/bin/env python3
"""
Obsidian Ideas → Graphiti knowledge graph ingest.

Design:
  - Walks ~/Documents/ObsidianVault/Ideas/**/*.md (PROMOTED notes only;
    Inbox is unclassified noise by design)
  - Tracks what's already ingested in ~/clawd/data/graphiti-obsidian-state.json
  - For each new note: strip frontmatter, extract body + title + tags,
    call graphiti_setup.ingest_text()
  - Runs via launchd nightly (03:00) so the KG stays in sync with what
    the user has promoted during the day

Usage:
    ingest_obsidian_to_graphiti.py [--dry-run] [--json] [--limit N]
    ingest_obsidian_to_graphiti.py --since 2026-04-01

Depends on ~/clawd/scripts/graphiti_setup.py (the system's canonical
Graphiti client — we import from it so there's a single source of truth
for driver / LLM / embedder configuration).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# Make the existing graphiti_setup module importable
GRAPHITI_SCRIPTS = Path.home() / "clawd" / "scripts"
if str(GRAPHITI_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(GRAPHITI_SCRIPTS))

VAULT = Path.home() / "Documents" / "ObsidianVault"
IDEAS_DIR = VAULT / "Ideas"
STATE_FILE = Path.home() / "clawd" / "data" / "graphiti-obsidian-state.json"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


# ── State ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"ingested_files": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"ingested_files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Note parsing ─────────────────────────────────────────────────────────

def parse_note(path: Path) -> dict:
    """Read a promoted idea note → dict with frontmatter + body."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {"frontmatter": {}, "body": text, "path": path}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return {"frontmatter": fm, "body": m.group(2).strip(), "path": path}


def note_signature(path: Path) -> str:
    """Mtime + size combo — cheap way to detect "this note changed since last ingest"."""
    st = path.stat()
    return f"{int(st.st_mtime)}-{st.st_size}"


def iter_idea_notes(since: Optional[datetime] = None) -> list[Path]:
    """Yield all *.md under Ideas/**/. Optionally filter by mtime >= since."""
    if not IDEAS_DIR.exists():
        return []
    notes = sorted(IDEAS_DIR.rglob("*.md"))
    if since is None:
        return notes
    since_ts = since.timestamp()
    return [n for n in notes if n.stat().st_mtime >= since_ts]


# ── Ingest ───────────────────────────────────────────────────────────────

async def _ingest_one(graphiti, note: dict) -> dict:
    """Call graphiti_setup.ingest_text on one note."""
    # Lazy import — graphiti_setup has heavyweight LLM/embedder init
    from graphiti_setup import ingest_text  # type: ignore

    fm = note["frontmatter"]
    body = note["body"]
    path: Path = note["path"]

    title = fm.get("title") or path.stem.replace("-", " ")
    category = fm.get("category") or "uncategorized"
    # Reference time: promoted_at if present, else file mtime
    ref_str = fm.get("promoted_at") or fm.get("captured_at")
    if ref_str:
        try:
            ref_time = datetime.fromisoformat(str(ref_str).replace("Z", "+00:00"))
        except Exception:
            ref_time = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    else:
        ref_time = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    # Episode body: include title + tags so entity extraction can link them
    tags = fm.get("tags") or []
    header = f"# {title}\n\nCategory: {category}\n"
    if tags:
        header += f"Tags: {', '.join(tags)}\n"
    episode_body = header + "\n" + body

    result = await ingest_text(
        graphiti,
        name=f"idea:{path.name}",
        body=episode_body,
        description=f"Promoted idea note from {path.relative_to(VAULT)}",
        ref_time=ref_time,
    )
    return {
        "path": str(path),
        "title": title,
        "category": category,
        "episode_uuid": result["episode_uuid"],
        "entities": result["entities"],
        "edges": result["edges"],
    }


async def _run_ingest(
    notes: list[Path],
    *,
    state: dict,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    summary: dict = {
        "found": len(notes),
        "skipped_already_ingested": 0,
        "ingested": 0,
        "errors": [],
        "ingested_items": [],
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ingested = dict(state.get("ingested_files", {}))

    # Filter to new-or-changed notes
    candidates: list[tuple[Path, str]] = []
    for note_path in notes:
        sig = note_signature(note_path)
        if ingested.get(str(note_path)) == sig:
            summary["skipped_already_ingested"] += 1
            continue
        candidates.append((note_path, sig))

    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return summary

    graphiti = None
    if not dry_run:
        from graphiti_setup import init_graphiti  # type: ignore
        graphiti = await init_graphiti()

    for note_path, sig in candidates:
        parsed = parse_note(note_path)
        try:
            if dry_run:
                item = {
                    "path": str(note_path),
                    "title": parsed["frontmatter"].get("title") or note_path.stem,
                    "category": parsed["frontmatter"].get("category") or "uncategorized",
                    "dry_run": True,
                }
            else:
                item = await _ingest_one(graphiti, parsed)
            summary["ingested_items"].append(item)
            summary["ingested"] += 1
            ingested[str(note_path)] = sig
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "path": str(note_path),
                "error": f"{type(e).__name__}: {e}",
            })

    if graphiti is not None:
        try:
            await graphiti.close()
        except Exception:
            pass

    if not dry_run:
        state["ingested_files"] = ingested
        save_state(state)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return summary


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest Obsidian Ideas → Graphiti KG")
    ap.add_argument("--since", type=str, default=None,
                     help="Only ingest notes modified on/after this date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, default=None,
                     help="Cap number of notes to ingest per run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"bad --since value: {args.since!r}", file=sys.stderr)
            return 2

    notes = iter_idea_notes(since=since_dt)
    state = load_state()

    try:
        summary = asyncio.run(_run_ingest(
            notes, state=state, dry_run=args.dry_run, limit=args.limit,
        ))
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"Obsidian → Graphiti ingest  [{'dry-run' if args.dry_run else 'live'}]")
    print(f"  vault path:       {IDEAS_DIR}")
    print(f"  found:            {summary['found']}")
    print(f"  skipped (dup):    {summary['skipped_already_ingested']}")
    print(f"  ingested:         {summary['ingested']}")
    print(f"  errors:           {len(summary['errors'])}")
    for item in summary["ingested_items"][:10]:
        tag = "(dry)" if item.get("dry_run") else f"{item.get('entities',0)} ent / {item.get('edges',0)} edge"
        print(f"    ✓ {item.get('title','?')}  [{item.get('category','?')}]  {tag}")
    for err in summary["errors"]:
        print(f"    ✗ {err['path']}: {err['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
