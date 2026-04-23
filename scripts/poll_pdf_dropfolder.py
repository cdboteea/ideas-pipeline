#!/usr/bin/env python3
"""
PDF drop-folder watcher → Obsidian Inbox.

Watches a folder (default ~/Downloads/ToStage/). For each PDF that lands,
extract text → stage to Inbox via capture.stage_pdf(), then move the file
to a `_staged/` sibling folder so we don't re-process.

Usage:
    poll_pdf_dropfolder.py [--folder <path>] [--dry-run] [--json]

Env:
    PDF_DROP_FOLDER   override the watched folder (default ~/Downloads/ToStage)

Design:
    - Polling (not fsevents) — launchd fires bi-hourly, no daemon to manage.
    - Text extraction via pypdf; empty/scanned PDFs fall back to stub text.
    - Move (not copy) after staging — the file is gone from the watched folder.
    - Non-PDF files are ignored (not moved).
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas.capture import stage_pdf


DEFAULT_DROP_FOLDER = Path(os.environ.get(
    "PDF_DROP_FOLDER",
    str(Path.home() / "Downloads" / "ToStage"),
)).expanduser()


def ensure_dirs(drop: Path) -> Path:
    """Create drop folder + _staged sibling if missing. Return the staged dir."""
    drop.mkdir(parents=True, exist_ok=True)
    staged = drop / "_staged"
    staged.mkdir(parents=True, exist_ok=True)
    return staged


def extract_text(pdf_path: Path, max_pages: int = 20) -> str:
    """Best-effort text extraction (returns empty string on failure).

    Caps at `max_pages` to avoid blowing up on huge PDFs — we just want
    enough preview text for the Inbox stub; the original file is linked
    via source_url so full access remains possible.
    """
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(c.strip() for c in chunks if c.strip())


def stage_and_move(pdf_path: Path, staged_dir: Path, dry_run: bool = False) -> dict:
    title_hint = pdf_path.stem.replace("_", " ").replace("-", " ")
    text = extract_text(pdf_path)
    if not text:
        text = f"(no extractable text — original PDF at {pdf_path})"

    result: dict = {
        "source_path": str(pdf_path),
        "title_hint": title_hint,
        "text_chars": len(text),
    }

    if dry_run:
        result["inbox_path"] = f"(dry-run — would stage)"
        result["moved_to"] = f"(dry-run — would move to {staged_dir})"
        return result

    inbox_path = stage_pdf(
        pdf_path=str(pdf_path),
        title_hint=title_hint,
        extracted_text=text,
    )
    result["inbox_path"] = inbox_path

    # Move — disambiguate if a same-named file is already staged
    dest = staged_dir / pdf_path.name
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = staged_dir / f"{pdf_path.stem}-{stamp}{pdf_path.suffix}"
    shutil.move(str(pdf_path), str(dest))
    result["moved_to"] = str(dest)
    return result


def poll(folder: Path = DEFAULT_DROP_FOLDER, dry_run: bool = False) -> dict:
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "folder": str(folder),
        "found": 0,
        "staged": 0,
        "errors": [],
        "staged_items": [],
        "dry_run": dry_run,
    }

    staged_dir = ensure_dirs(folder)
    # Only top-level *.pdf — don't recurse into _staged/
    pdfs = sorted([p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() == ".pdf"])
    summary["found"] = len(pdfs)

    for pdf in pdfs:
        try:
            item = stage_and_move(pdf, staged_dir, dry_run=dry_run)
            summary["staged_items"].append(item)
            summary["staged"] += 1
        except Exception as e:  # noqa: BLE001 — continue on per-file error
            summary["errors"].append({"pdf": str(pdf), "error": f"{type(e).__name__}: {e}"})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll PDF drop folder → Obsidian Inbox")
    ap.add_argument("--folder", type=Path, default=DEFAULT_DROP_FOLDER)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        summary = poll(folder=args.folder.expanduser(), dry_run=args.dry_run)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"PDF drop folder poll  [{'dry-run' if args.dry_run else 'live'}]")
    print(f"  folder:          {summary['folder']}")
    print(f"  found:           {summary['found']}")
    print(f"  staged:          {summary['staged']}")
    print(f"  errors:          {len(summary['errors'])}")
    for item in summary["staged_items"]:
        print(f"    ✓ {Path(item['source_path']).name}  ({item['text_chars']} chars extracted)")
        print(f"      → {item['inbox_path']}")
        print(f"      → moved to {item['moved_to']}")
    for err in summary["errors"]:
        print(f"    ✗ {err['pdf']}: {err['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
