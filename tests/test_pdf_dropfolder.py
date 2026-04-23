"""Behavioral tests for the PDF drop-folder watcher."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _make_minimal_pdf(path: Path, text: str = "Hello world") -> None:
    """Write a minimal valid PDF with `text` on page 1."""
    import pypdf
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # Inject a BT/ET text block on page 1
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    stream = DecodedStreamObject()
    stream.set_data(content)
    writer.pages[0][NameObject("/Contents")] = stream
    with path.open("wb") as f:
        writer.write(f)


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    vault = tmp_path / "ObsidianVault"
    import ideas.storage as storage
    monkeypatch.setattr("ideas.config.VAULT", vault)
    monkeypatch.setattr("ideas.config.INBOX", vault / "Inbox")
    monkeypatch.setattr("ideas.config.IDEAS", vault / "Ideas")
    monkeypatch.setattr("ideas.config.ARCHIVE", vault / "Archive")
    monkeypatch.setattr("ideas.config.META", vault / "_meta")
    monkeypatch.setattr(storage, "INBOX", vault / "Inbox")
    monkeypatch.setattr(storage, "IDEAS", vault / "Ideas")
    monkeypatch.setattr(storage, "ARCHIVE", vault / "Archive")
    from ideas.config import ensure_dirs
    ensure_dirs()
    yield vault


@pytest.fixture
def drop_folder(tmp_path):
    folder = tmp_path / "DropZone"
    folder.mkdir()
    yield folder


# ── Tests ──────────────────────────────────────────────────────────────────

def test_poll_stages_pdfs_and_moves_them(isolated_vault, drop_folder):
    import poll_pdf_dropfolder as watcher

    pdf1 = drop_folder / "idea-one.pdf"
    pdf2 = drop_folder / "other-thought.pdf"
    _make_minimal_pdf(pdf1, "Content one")
    _make_minimal_pdf(pdf2, "Content two")

    summary = watcher.poll(folder=drop_folder, dry_run=False)

    assert summary["found"] == 2
    assert summary["staged"] == 2
    assert summary["errors"] == []
    # Both moved to _staged
    assert not pdf1.exists()
    assert not pdf2.exists()
    staged = drop_folder / "_staged"
    assert (staged / "idea-one.pdf").exists()
    assert (staged / "other-thought.pdf").exists()
    # Inbox entries written
    inbox = isolated_vault / "Inbox"
    assert len(list(inbox.glob("*.md"))) == 2


def test_poll_dry_run_doesnt_move_or_stage(isolated_vault, drop_folder):
    import poll_pdf_dropfolder as watcher

    pdf = drop_folder / "keep-me.pdf"
    _make_minimal_pdf(pdf)

    summary = watcher.poll(folder=drop_folder, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["staged"] == 1
    # File stayed in place
    assert pdf.exists()
    # No inbox file
    assert list((isolated_vault / "Inbox").glob("*.md")) == []


def test_poll_ignores_non_pdf_files(isolated_vault, drop_folder):
    import poll_pdf_dropfolder as watcher

    (drop_folder / "ignore.txt").write_text("plain text")
    (drop_folder / "also-ignore.png").write_bytes(b"fakepng")
    pdf = drop_folder / "real.pdf"
    _make_minimal_pdf(pdf)

    summary = watcher.poll(folder=drop_folder, dry_run=False)
    assert summary["found"] == 1
    # Non-PDFs still there, untouched
    assert (drop_folder / "ignore.txt").exists()
    assert (drop_folder / "also-ignore.png").exists()


def test_poll_handles_filename_collision_in_staged(isolated_vault, drop_folder):
    import poll_pdf_dropfolder as watcher

    # Pre-populate _staged with same name
    staged = drop_folder / "_staged"
    staged.mkdir()
    (staged / "dup.pdf").write_bytes(b"prior")

    pdf = drop_folder / "dup.pdf"
    _make_minimal_pdf(pdf)
    summary = watcher.poll(folder=drop_folder, dry_run=False)
    assert summary["staged"] == 1
    # Original "dup.pdf" in staged preserved
    assert (staged / "dup.pdf").exists()
    assert (staged / "dup.pdf").read_bytes() == b"prior"
    # New arrival went to a timestamped variant
    new_files = [p for p in staged.iterdir() if p.name.startswith("dup-")]
    assert len(new_files) == 1


def test_poll_error_in_one_pdf_does_not_abort(isolated_vault, drop_folder, monkeypatch):
    import poll_pdf_dropfolder as watcher

    pdf1 = drop_folder / "good.pdf"
    pdf2 = drop_folder / "bad.pdf"
    _make_minimal_pdf(pdf1)
    _make_minimal_pdf(pdf2)

    original = watcher.stage_pdf
    def flaky(pdf_path, title_hint, extracted_text):
        if "bad" in pdf_path:
            raise RuntimeError("boom")
        return original(pdf_path=pdf_path, title_hint=title_hint, extracted_text=extracted_text)
    monkeypatch.setattr(watcher, "stage_pdf", flaky)

    summary = watcher.poll(folder=drop_folder, dry_run=False)
    assert summary["staged"] == 1
    assert len(summary["errors"]) == 1
    assert "bad" in summary["errors"][0]["pdf"]
    # good was moved; bad stayed
    assert not pdf1.exists()
    assert pdf2.exists()


def test_poll_creates_folder_if_missing(isolated_vault, tmp_path):
    import poll_pdf_dropfolder as watcher
    not_yet = tmp_path / "NotCreatedYet"
    assert not not_yet.exists()
    summary = watcher.poll(folder=not_yet, dry_run=False)
    assert summary["found"] == 0
    assert not_yet.exists()
    assert (not_yet / "_staged").exists()


def test_extract_text_returns_empty_on_invalid_pdf(tmp_path):
    import poll_pdf_dropfolder as watcher
    bad = tmp_path / "not-a-real.pdf"
    bad.write_bytes(b"not actually a pdf")
    assert watcher.extract_text(bad) == ""
