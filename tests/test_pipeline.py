"""Minimal behavioral tests — no source-code inspection. Run via pytest."""
from __future__ import annotations
import os
from pathlib import Path
import pytest

# Point to a temp vault for tests
@pytest.fixture(autouse=True)
def tmp_vault(tmp_path, monkeypatch):
    from ideas import config
    monkeypatch.setattr(config, "VAULT", tmp_path)
    monkeypatch.setattr(config, "INBOX", tmp_path / "Inbox")
    monkeypatch.setattr(config, "IDEAS", tmp_path / "Ideas")
    monkeypatch.setattr(config, "ARCHIVE", tmp_path / "Archive")
    monkeypatch.setattr(config, "META", tmp_path / "_meta")
    # Also patch the already-imported module-level bindings in storage
    from ideas import storage
    monkeypatch.setattr(storage, "INBOX", tmp_path / "Inbox")
    monkeypatch.setattr(storage, "IDEAS", tmp_path / "Ideas")
    monkeypatch.setattr(storage, "ARCHIVE", tmp_path / "Archive")
    from ideas import review as review_mod
    monkeypatch.setattr(review_mod, "INBOX", tmp_path / "Inbox")
    yield tmp_path


def test_stage_url_creates_inbox_stub(tmp_vault):
    from ideas.capture import stage_url
    path_str = stage_url(
        url="https://example.com/article",
        title_hint="Example Article",
        preview="This is a preview of the article content",
    )
    path = Path(path_str)
    assert path.exists()
    content = path.read_text()
    assert "status: pending" in content
    assert "https://example.com/article" in content
    assert "source_type: url" in content


def test_stage_and_promote_round_trip(tmp_vault):
    from ideas.capture import stage_thought
    from ideas.promote import promote
    from ideas.config import IDEAS, ARCHIVE

    inbox_path = Path(stage_thought(
        text="We should use DuckDB for everything in the strategy engine.",
        title_hint="Use DuckDB for strategy engine",
    ))
    assert inbox_path.exists()

    result = promote(
        inbox_path=inbox_path,
        category="decisions",
        title="DuckDB for strategy-engine storage",
        summary="Use DuckDB as single storage layer; SQLite only if contention measured.",
        tags=["architecture", "storage", "duckdb"],
        key_points=["Single DuckDB per purpose", "Each file has one writer process"],
        why_this_matters="Removes premature optimization; matches existing stack.",
    )
    assert result["success"], result
    idea_path = Path(result["path"])
    assert idea_path.exists()
    assert idea_path.parent.name == "decisions"
    # Original inbox stub moved to archive
    assert not inbox_path.exists()
    # Content checks
    body = idea_path.read_text()
    assert "DuckDB for strategy-engine storage" in body
    assert "duckdb" in body


def test_promote_rejects_invalid_category(tmp_vault):
    from ideas.capture import stage_thought
    from ideas.promote import promote
    inbox_path = Path(stage_thought("Test", "test"))
    result = promote(
        inbox_path=inbox_path,
        category="not-a-real-category",
        title="Test",
        summary="Test",
        tags=["test"],
    )
    assert not result["success"]
    assert "Invalid category" in result["message"]


def test_dedup_by_url(tmp_vault):
    """Second promote of same URL should fail as duplicate."""
    from ideas.capture import stage_url
    from ideas.promote import promote

    # First promote succeeds
    p1 = Path(stage_url("https://example.com/duplicate", "First", "content A"))
    r1 = promote(p1, category="tools", title="First", summary="First", tags=["test"])
    assert r1["success"]

    # Second identical URL → duplicate detected
    p2 = Path(stage_url("https://example.com/duplicate", "Second", "content B"))
    r2 = promote(p2, category="tools", title="Second", summary="Second", tags=["test"])
    assert not r2["success"]
    assert "Duplicate" in r2["message"]


def test_discard_moves_to_archive(tmp_vault):
    from ideas.capture import stage_thought
    from ideas.review import discard
    from ideas.config import ARCHIVE
    path = Path(stage_thought("ephemeral thought", "eph"))
    assert path.exists()
    dest = discard(path)
    assert not path.exists()
    assert dest.exists()
    assert str(ARCHIVE) in str(dest)
    assert "status: discarded" in dest.read_text()


def test_review_summary(tmp_vault):
    from ideas.capture import stage_url, stage_thought
    from ideas.review import inbox_summary

    stage_url("https://a.com", "A")
    stage_url("https://b.com", "B")
    stage_thought("a thought", "t")

    s = inbox_summary()
    assert s["total_pending"] == 3
    assert s["by_source_type"]["url"] == 2
    assert s["by_source_type"]["thought"] == 1


def test_auto_archive_skips_fresh_items(tmp_vault):
    from ideas.capture import stage_thought
    from ideas.review import auto_archive_expired
    stage_thought("fresh", "fresh")
    archived = auto_archive_expired()
    assert archived == []
