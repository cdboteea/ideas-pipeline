"""Behavioral tests for the Obsidian → Graphiti ingest script.

Graphiti itself is expensive to initialize (LLM + embedder + reranker), so
these tests never actually call it — they inject a stub for `ingest_text`
and verify the ingest script's file-walking, state tracking, and error
handling behavior.
"""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _write_note(path: Path, title: str, body: str, category: str = "trading", tags=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"""---
title: {title}
category: {category}
tags: {tags or []}
captured_at: 2026-04-22T10:00:00-04:00
promoted_at: 2026-04-22T15:00:00-04:00
status: active
---

{body}
"""
    path.write_text(frontmatter)


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    vault = tmp_path / "ObsidianVault"
    ideas_dir = vault / "Ideas"
    ideas_dir.mkdir(parents=True)
    import ingest_obsidian_to_graphiti as ingest
    monkeypatch.setattr(ingest, "VAULT", vault)
    monkeypatch.setattr(ingest, "IDEAS_DIR", ideas_dir)
    state_file = tmp_path / "graphiti-state.json"
    monkeypatch.setattr(ingest, "STATE_FILE", state_file)
    yield ideas_dir


# ── Tests ──────────────────────────────────────────────────────────────────

def test_iter_idea_notes_walks_subdirs(isolated_vault):
    import ingest_obsidian_to_graphiti as ingest

    (isolated_vault / "trading").mkdir()
    (isolated_vault / "research").mkdir()
    _write_note(isolated_vault / "trading" / "note1.md", "Trading 1", "body 1")
    _write_note(isolated_vault / "research" / "note2.md", "Research 2", "body 2")
    _write_note(isolated_vault / "trading" / "note3.md", "Trading 3", "body 3")

    notes = ingest.iter_idea_notes()
    assert len(notes) == 3
    names = sorted(n.name for n in notes)
    assert names == ["note1.md", "note2.md", "note3.md"]


def test_parse_note_extracts_frontmatter_and_body(isolated_vault):
    import ingest_obsidian_to_graphiti as ingest

    p = isolated_vault / "test.md"
    _write_note(p, "Title", "Hello body\n\nMore body", category="ai-infra", tags=["alpha", "beta"])
    parsed = ingest.parse_note(p)
    assert parsed["frontmatter"]["title"] == "Title"
    assert parsed["frontmatter"]["category"] == "ai-infra"
    assert parsed["frontmatter"]["tags"] == ["alpha", "beta"]
    assert "Hello body" in parsed["body"]
    assert "More body" in parsed["body"]


def test_dry_run_skips_graphiti_and_state(isolated_vault):
    import ingest_obsidian_to_graphiti as ingest

    _write_note(isolated_vault / "a.md", "A", "body a")
    _write_note(isolated_vault / "b.md", "B", "body b")

    summary = asyncio.run(ingest._run_ingest(
        ingest.iter_idea_notes(),
        state=ingest.load_state(),
        dry_run=True,
    ))
    assert summary["found"] == 2
    assert summary["ingested"] == 2
    assert summary["errors"] == []
    # State NOT saved in dry-run
    assert not ingest.STATE_FILE.exists()


def test_live_ingest_calls_graphiti_and_tracks_state(isolated_vault, monkeypatch):
    import ingest_obsidian_to_graphiti as ingest

    _write_note(isolated_vault / "alpha.md", "Alpha Idea", "interesting body")

    calls = []
    async def fake_ingest_text(g, name, body, description, ref_time):
        calls.append({"name": name, "body": body, "description": description})
        return {"episode_uuid": "ep-123", "entities": 2, "edges": 1}

    async def fake_init_graphiti():
        return object()   # sentinel — closed at end

    # Inject into graphiti_setup module namespace that the ingest script will import
    import types
    fake_mod = types.SimpleNamespace(
        ingest_text=fake_ingest_text,
        init_graphiti=fake_init_graphiti,
    )
    monkeypatch.setitem(sys.modules, "graphiti_setup", fake_mod)

    summary = asyncio.run(ingest._run_ingest(
        ingest.iter_idea_notes(),
        state=ingest.load_state(),
        dry_run=False,
    ))

    assert summary["ingested"] == 1
    assert summary["errors"] == []
    assert len(calls) == 1
    # State file persisted
    assert ingest.STATE_FILE.exists()
    state = json.loads(ingest.STATE_FILE.read_text())
    assert len(state["ingested_files"]) == 1


def test_skip_already_ingested_on_second_run(isolated_vault, monkeypatch):
    import ingest_obsidian_to_graphiti as ingest

    _write_note(isolated_vault / "a.md", "A", "body")

    import types
    call_count = [0]
    async def fake_ingest_text(*a, **kw):
        call_count[0] += 1
        return {"episode_uuid": "ep", "entities": 1, "edges": 0}
    async def fake_init_graphiti():
        return object()

    fake_mod = types.SimpleNamespace(
        ingest_text=fake_ingest_text,
        init_graphiti=fake_init_graphiti,
    )
    monkeypatch.setitem(sys.modules, "graphiti_setup", fake_mod)

    # First run: ingest
    asyncio.run(ingest._run_ingest(
        ingest.iter_idea_notes(), state=ingest.load_state(), dry_run=False,
    ))
    assert call_count[0] == 1

    # Second run with unchanged file: skip
    summary = asyncio.run(ingest._run_ingest(
        ingest.iter_idea_notes(), state=ingest.load_state(), dry_run=False,
    ))
    assert call_count[0] == 1    # NOT called again
    assert summary["skipped_already_ingested"] == 1
    assert summary["ingested"] == 0


def test_error_in_one_note_does_not_abort_rest(isolated_vault, monkeypatch):
    import ingest_obsidian_to_graphiti as ingest

    _write_note(isolated_vault / "good1.md", "G1", "body")
    _write_note(isolated_vault / "bad.md", "B", "boom")
    _write_note(isolated_vault / "good2.md", "G2", "body")

    import types
    async def fake_ingest_text(g, name, body, description, ref_time):
        if "bad" in name:
            raise RuntimeError("ingest failed")
        return {"episode_uuid": "ep", "entities": 1, "edges": 0}
    async def fake_init_graphiti():
        return object()

    fake_mod = types.SimpleNamespace(
        ingest_text=fake_ingest_text,
        init_graphiti=fake_init_graphiti,
    )
    monkeypatch.setitem(sys.modules, "graphiti_setup", fake_mod)

    summary = asyncio.run(ingest._run_ingest(
        ingest.iter_idea_notes(), state=ingest.load_state(), dry_run=False,
    ))
    assert summary["ingested"] == 2
    assert len(summary["errors"]) == 1
    assert "bad.md" in summary["errors"][0]["path"]
