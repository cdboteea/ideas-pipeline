"""Behavioral tests for the Telegram-queue drainer."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


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
def queue_file(tmp_path):
    return tmp_path / "queue.jsonl"


def _write_queue(path: Path, entries: list[dict]) -> None:
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── Tests ──────────────────────────────────────────────────────────────────

def test_drain_stages_all_entries(isolated_vault, queue_file):
    import poll_telegram_queue as poller

    _write_queue(queue_file, [
        {"text": "First idea from Telegram", "sender": "Matias", "chat_id": 100, "message_id": 1},
        {"text": "Second thought", "sender": "Matias", "chat_id": 100, "message_id": 2},
    ])
    summary = poller.drain_queue(queue_file)

    assert summary["found"] == 2
    assert summary["staged"] == 2
    assert summary["skipped_malformed"] == 0
    assert summary["errors"] == []
    # Queue file deleted after successful drain
    assert not queue_file.exists()
    # Inbox entries created
    assert len(list((isolated_vault / "Inbox").glob("*.md"))) == 2


def test_drain_handles_empty_queue(isolated_vault, queue_file):
    import poll_telegram_queue as poller
    # Queue file doesn't exist
    summary = poller.drain_queue(queue_file)
    assert summary["found"] == 0
    assert summary["staged"] == 0


def test_drain_skips_malformed_lines(isolated_vault, queue_file):
    import poll_telegram_queue as poller

    with queue_file.open("w") as f:
        f.write(json.dumps({"text": "valid", "sender": "X"}) + "\n")
        f.write("not json at all\n")
        f.write("\n")  # empty line
        f.write(json.dumps({"text": "also valid", "sender": "Y"}) + "\n")

    summary = poller.drain_queue(queue_file)
    assert summary["found"] == 2
    assert summary["staged"] == 2
    assert summary["skipped_malformed"] == 1    # the junk line (empty is just skipped silently)


def test_drain_is_atomic_leaves_clean_state_on_partial_failure(isolated_vault, queue_file, monkeypatch):
    """If one entry staging fails, others still stage AND the queue file is
    still consumed (we don't re-drive on partial failure)."""
    import poll_telegram_queue as poller

    _write_queue(queue_file, [
        {"text": "good 1", "sender": "A", "message_id": 1},
        {"text": "BAD", "sender": "B", "message_id": 2},
        {"text": "good 3", "sender": "C", "message_id": 3},
    ])

    original = poller.stage_telegram
    def flaky(**kw):
        if kw.get("message_id") == 2:
            raise RuntimeError("boom")
        return original(**kw)
    monkeypatch.setattr(poller, "stage_telegram", flaky)

    summary = poller.drain_queue(queue_file)
    assert summary["staged"] == 2
    assert len(summary["errors"]) == 1
    assert not queue_file.exists()  # queue still drained (the error is logged, not retried)


def test_drain_dry_run_preserves_queue(isolated_vault, queue_file):
    import poll_telegram_queue as poller

    _write_queue(queue_file, [{"text": "test", "sender": "X"}])
    summary = poller.drain_queue(queue_file, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["staged"] == 1
    assert queue_file.exists()   # not consumed
    assert list((isolated_vault / "Inbox").glob("*.md")) == []


def test_stage_telegram_writes_inbox_md(isolated_vault):
    from ideas.capture import stage_telegram
    path = stage_telegram(
        text="Watch SPY Friday close",
        sender="Matias",
        chat_id=8463750100,
        message_id=42,
        received_at="2026-04-23T17:20:00Z",
    )
    p = Path(path)
    assert p.exists()
    txt = p.read_text()
    assert "source_type: telegram" in txt
    assert "captured_by: telegram-queue-poller" in txt
    assert "Watch SPY Friday close" in txt
    assert "telegram://8463750100/42" in txt
