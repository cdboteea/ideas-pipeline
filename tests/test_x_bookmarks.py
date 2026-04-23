"""Behavioral tests for the X-bookmarks poller.

We never invoke bird for real — we inject a fake subprocess runner.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_runner(bookmarks_json: str = "[]", returncode: int = 0, stderr: str = ""):
    """Return a fake `subprocess.run` that scripts can call."""
    calls = []

    def runner(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        # Respond to different subcommands
        if "bookmarks" in cmd:
            return _FakeResult(stdout=bookmarks_json, returncode=returncode, stderr=stderr)
        if "unbookmark" in cmd:
            return _FakeResult(returncode=0)
        return _FakeResult(stdout="", returncode=0)

    runner.calls = calls
    return runner


def _tweet(tid: str, username: str, text: str):
    return {
        "id": tid,
        "text": text,
        "createdAt": "Thu Apr 23 09:00:00 +0000 2026",
        "author": {"username": username, "name": username.title()},
    }


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    vault = tmp_path / "ObsidianVault"
    # Patch both config AND storage — storage imports INBOX at module load, so
    # patching only config would leak test1's cached path into test2.
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
def isolated_state(tmp_path, monkeypatch):
    import poll_x_bookmarks as poller
    state = tmp_path / "xstate.json"
    monkeypatch.setattr(poller, "STATE_FILE", state)
    yield state


# ── Tests ──────────────────────────────────────────────────────────────────

def test_fetches_and_stages_all_new_bookmarks(isolated_vault, isolated_state):
    import poll_x_bookmarks as poller

    tweets = [
        _tweet("1", "alice", "First interesting idea"),
        _tweet("2", "bob", "Second idea worth remembering"),
    ]
    runner = _make_runner(bookmarks_json=json.dumps(tweets))
    summary = poller.poll(count=20, runner=runner)

    assert summary["fetched"] == 2
    assert summary["staged"] == 2
    assert summary["skipped_duplicate"] == 0
    assert summary["errors"] == []
    # 2 inbox files written
    assert len(list((isolated_vault / "Inbox").glob("*.md"))) == 2
    # Only 1 subprocess call (bookmarks); no unbookmark since --unbookmark was off
    assert runner.calls == [[
        str(poller.BIRD_BIN), "bookmarks", "--count", "20", "--json",
    ]]


def test_skips_already_processed_ids(isolated_vault, isolated_state):
    import poll_x_bookmarks as poller

    isolated_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_state.write_text(json.dumps({"processed_ids": ["1"]}))

    tweets = [_tweet("1", "a", "dup"), _tweet("2", "b", "new")]
    runner = _make_runner(bookmarks_json=json.dumps(tweets))
    summary = poller.poll(count=10, runner=runner)

    assert summary["staged"] == 1
    assert summary["skipped_duplicate"] == 1
    assert len(list((isolated_vault / "Inbox").glob("*.md"))) == 1


def test_unbookmark_flag_triggers_extra_subprocess_call(isolated_vault, isolated_state):
    import poll_x_bookmarks as poller

    tweets = [_tweet("99", "charlie", "to be unbookmarked")]
    runner = _make_runner(bookmarks_json=json.dumps(tweets))

    summary = poller.poll(count=10, unbookmark_after=True, runner=runner)
    assert summary["staged"] == 1
    assert summary["unbookmarked"] == 1
    # bookmarks + unbookmark calls
    cmds = [" ".join(c) for c in runner.calls]
    assert any("bookmarks" in c for c in cmds)
    assert any("unbookmark 99" in c for c in cmds)


def test_dry_run_makes_no_changes(isolated_vault, isolated_state):
    import poll_x_bookmarks as poller

    tweets = [_tweet("1", "x", "hello")]
    runner = _make_runner(bookmarks_json=json.dumps(tweets))

    summary = poller.poll(count=5, dry_run=True, runner=runner)
    assert summary["dry_run"] is True
    assert summary["staged"] == 1
    # No inbox file, no state write
    assert list((isolated_vault / "Inbox").glob("*.md")) == []
    assert not isolated_state.exists()


def test_bird_failure_raises_runtime_error(isolated_vault, isolated_state):
    import poll_x_bookmarks as poller

    runner = _make_runner(returncode=1, stderr="rate limited")
    with pytest.raises(RuntimeError) as exc:
        poller.poll(count=5, runner=runner)
    assert "bird bookmarks failed" in str(exc.value)
    assert "rate limited" in str(exc.value)


def test_per_tweet_failure_does_not_abort(isolated_vault, isolated_state, monkeypatch):
    import poll_x_bookmarks as poller

    tweets = [_tweet("a", "x", "ok"), _tweet("b", "y", "break-me")]
    runner = _make_runner(bookmarks_json=json.dumps(tweets))

    # Break stage_tweet for tweet b only
    original = poller.stage_tweet
    def flaky(tweet):
        if tweet.get("id") == "b":
            raise RuntimeError("boom")
        return original(tweet)
    monkeypatch.setattr(poller, "stage_tweet", flaky)

    summary = poller.poll(count=10, runner=runner)
    assert summary["staged"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["tweet_id"] == "b"
