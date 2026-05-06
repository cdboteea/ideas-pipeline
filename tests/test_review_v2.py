"""Tests for #122 v2 — last-review tracking + inbox_status slicing.

The interactive `review interactive` CLI is exercised via Click's CliRunner.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from ideas import review as rev
from ideas import config
from ideas.cli import cli


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point all ideas paths at tmp_path so the real vault is untouched."""
    monkeypatch.setattr(config, "INBOX",   tmp_path / "Inbox")
    monkeypatch.setattr(config, "IDEAS",   tmp_path / "Ideas")
    monkeypatch.setattr(config, "ARCHIVE", tmp_path / "Archive")
    (tmp_path / "Inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Archive").mkdir(parents=True, exist_ok=True)
    # Re-bind storage.INBOX as well (it imports config.INBOX at module load)
    from ideas import storage
    monkeypatch.setattr(storage, "INBOX",   tmp_path / "Inbox")
    monkeypatch.setattr(storage, "IDEAS",   tmp_path / "Ideas")
    monkeypatch.setattr(storage, "ARCHIVE", tmp_path / "Archive")
    yield tmp_path


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect LAST_REVIEW_FILE to tmp_path."""
    state = tmp_path / "last-review.json"
    monkeypatch.setattr(rev, "LAST_REVIEW_FILE", state)
    return state


def _stage(vault: Path, slug: str, source_type: str = "x-post",
           captured_at: str | None = None,
           last_touched: str | None = None,
           defer_note: str | None = None) -> Path:
    """Write a minimal Inbox stub for testing."""
    fm = {
        "id":           slug,
        "source_type":  source_type,
        "captured_at":  captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status":       "pending",
    }
    if last_touched:
        fm["last_touched"] = last_touched
    if defer_note:
        fm["defer_note"] = defer_note
    body = f"## Raw content\n\nbody for {slug}\n"
    import yaml
    md = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body
    p = vault / "Inbox" / f"{slug}.md"
    p.write_text(md)
    return p


# ── Last-review file ──────────────────────────────────────────────────────


def test_get_last_review_at_returns_none_when_file_missing(isolated_state):
    assert rev.get_last_review_at() is None


def test_mark_review_now_persists_and_returns_iso(isolated_state):
    ts = rev.mark_review_now()
    assert isolated_state.exists()
    data = json.loads(isolated_state.read_text())
    assert data["last_review_at"].startswith(ts.isoformat(timespec="seconds")[:19])
    # Round-trip
    parsed = rev.get_last_review_at()
    assert parsed.isoformat(timespec="seconds") == ts.isoformat(timespec="seconds")


def test_get_last_review_at_handles_garbage_file(isolated_state):
    isolated_state.write_text("not json")
    assert rev.get_last_review_at() is None


# ── inbox_status (the session-start primitive) ────────────────────────────


def test_inbox_status_returns_total_when_no_last_review(isolated_vault, isolated_state):
    _stage(isolated_vault, "a")
    _stage(isolated_vault, "b")
    s = rev.inbox_status(since_last_review=True)
    # No prior review → all items counted as 'new'
    assert s["total"] == 2
    assert s["new"] == 2
    assert s["deferred"] == 0
    assert s["untouched"] == 0
    assert s["since"] is None


def test_inbox_status_slices_into_new_deferred_untouched(isolated_vault, isolated_state):
    """Item captured BEFORE last review with no defer = untouched.
    Item captured AFTER last review = new.
    Item touched AFTER last review = deferred."""
    review_t = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    before   = (review_t - timedelta(days=1)).isoformat(timespec="seconds")
    after    = (review_t + timedelta(hours=2)).isoformat(timespec="seconds")
    after_b  = (review_t + timedelta(hours=3)).isoformat(timespec="seconds")

    # untouched: captured before, never touched
    _stage(isolated_vault, "old-untouched", captured_at=before)
    # new: captured after
    _stage(isolated_vault, "new-bookmark", captured_at=after)
    # deferred: captured before BUT touched after
    _stage(isolated_vault, "deferred-one", captured_at=before, last_touched=after_b,
            defer_note="interesting but not now")
    # Set last review to review_t
    isolated_state.write_text(json.dumps({"last_review_at": review_t.isoformat(timespec="seconds")}))

    s = rev.inbox_status(since_last_review=True)
    assert s["total"] == 3
    assert s["new"] == 1
    assert s["deferred"] == 1
    assert s["untouched"] == 1
    assert s["by_source"] == {"x-post": 1}  # only the 'new' one counted in by_source


def test_inbox_status_filters_by_source_type(isolated_vault, isolated_state):
    _stage(isolated_vault, "x1", source_type="x-post")
    _stage(isolated_vault, "u1", source_type="url")
    s = rev.inbox_status(source_type="x-post")
    assert s["total"] == 1
    assert s["new"] == 1


# ── CLI smoke ──────────────────────────────────────────────────────────────


def test_cli_inbox_status_runs(isolated_vault, isolated_state):
    _stage(isolated_vault, "alpha")
    _stage(isolated_vault, "beta")
    runner = CliRunner()
    res = runner.invoke(cli, ["inbox-status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["total"] == 2


def test_cli_review_interactive_quit_immediately(isolated_vault, isolated_state):
    _stage(isolated_vault, "x")
    runner = CliRunner()
    # Simulate pressing 'q' at the first prompt
    res = runner.invoke(cli, ["review", "interactive"], input="q\n")
    assert res.exit_code == 0, res.output
    assert "Stopping review." in res.output


def test_cli_review_interactive_discard_path(isolated_vault, isolated_state):
    p = _stage(isolated_vault, "to-discard")
    runner = CliRunner()
    res = runner.invoke(cli, ["review", "interactive", "--no-mark"], input="d\nq\n")
    assert res.exit_code == 0, res.output
    # The file should have been moved out of Inbox
    assert not p.exists()
    # And ARCHIVE should now have it
    archives = list((isolated_vault / "Archive").rglob("to-discard*.md"))
    assert len(archives) == 1


def test_cli_review_interactive_defer_keeps_file(isolated_vault, isolated_state):
    p = _stage(isolated_vault, "to-defer")
    runner = CliRunner()
    res = runner.invoke(cli, ["review", "interactive", "--no-mark"],
                        input="f\ninteresting later\nq\n")
    assert res.exit_code == 0, res.output
    assert p.exists()  # still in Inbox
    text = p.read_text()
    assert "last_touched:" in text
    assert "interesting later" in text


def test_cli_review_interactive_marks_review_when_started(isolated_vault, isolated_state):
    _stage(isolated_vault, "x")
    runner = CliRunner()
    runner.invoke(cli, ["review", "interactive"], input="q\n")
    # last_review_at file should have been written
    assert isolated_state.exists()


def test_cli_review_interactive_no_mark_skips_state_write(isolated_vault, isolated_state):
    _stage(isolated_vault, "x")
    runner = CliRunner()
    runner.invoke(cli, ["review", "interactive", "--no-mark"], input="q\n")
    assert not isolated_state.exists()
