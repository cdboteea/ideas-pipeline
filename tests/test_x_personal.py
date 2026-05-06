"""Behavioral tests for personal-X bookmarks (Chrome-MCP capture path).

Mirror of test_x_bookmarks.py but for the Claude-in-Chrome side. We never
drive Chrome for real — we feed in fixture capture payloads.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from ideas import x_personal


@pytest.fixture
def isolated_state(tmp_path):
    """Provide a state file path inside tmp_path; nothing leaks."""
    return tmp_path / "personal-state.json"


def _stub_stage_fn():
    """Returns a (calls_list, stage_fn) pair where stage_fn just records calls."""
    calls = []

    def stage(*, url, author, content, **kw):
        calls.append({"url": url, "author": author, "content": content[:40]})
        return f"/fake/inbox/{url.rsplit('/', 1)[-1]}.md"

    return calls, stage


# ── normalize_capture ────────────────────────────────────────────────────


def test_normalize_capture_handles_dict_with_bookmarks_key():
    raw = {"count": 1, "bookmarks": [{
        "id": "12345", "handle": "alice", "display": "Alice Bob",
        "created_at": "2026-05-05T08:00:00.000Z", "text": "hello world",
        "media": {"photos": 0, "videos": 0},
    }]}
    out = x_personal.normalize_capture(raw)
    assert len(out) == 1
    t = out[0]
    assert t["id"] == "12345"
    assert t["author"]["username"] == "alice"
    assert t["author"]["name"] == "Alice Bob"
    assert t["text"] == "hello world"
    assert t["media"] == {"photos": 0, "videos": 0}


def test_normalize_capture_strips_at_prefix_from_handle():
    """The capture JS strips @ to bypass MCP filter, but tolerate either form."""
    raw = [{"id": "1", "handle": "@bob", "display": "Bob",
            "created_at": "2026-01-01T00:00:00.000Z", "text": "hi"}]
    out = x_personal.normalize_capture(raw)
    assert out[0]["author"]["username"] == "bob"


def test_normalize_capture_trims_handle_and_time_from_display():
    """X DOM emits 'DisplayName@handle·time' as one string — strip the tail."""
    raw = [{"id": "1", "handle": "alice", "display": "Alice Bob@alice·2h",
            "created_at": "2026-01-01T00:00:00.000Z", "text": "x"}]
    out = x_personal.normalize_capture(raw)
    assert out[0]["author"]["name"] == "Alice Bob"


def test_normalize_capture_trims_only_handle_when_no_time_suffix():
    raw = [{"id": "1", "handle": "alice", "display": "Alice Bob@alice",
            "created_at": "2026-01-01T00:00:00.000Z", "text": "x"}]
    out = x_personal.normalize_capture(raw)
    assert out[0]["author"]["name"] == "Alice Bob"


def test_normalize_capture_skips_items_with_missing_id():
    raw = [
        {"id": "", "handle": "x", "display": "X", "text": "skip me"},
        {"id": "999", "handle": "y", "display": "Y", "text": "keep me"},
    ]
    out = x_personal.normalize_capture(raw)
    assert len(out) == 1
    assert out[0]["id"] == "999"


def test_normalize_capture_accepts_json_string():
    payload = json.dumps([{"id": "1", "handle": "z", "display": "Z", "text": "t"}])
    out = x_personal.normalize_capture(payload)
    assert len(out) == 1


# ── poll_personal ────────────────────────────────────────────────────────


def test_poll_personal_stages_new_bookmarks(isolated_state):
    calls, stage = _stub_stage_fn()
    capture = [
        {"id": "100", "handle": "alice", "display": "Alice", "text": "first tweet"},
        {"id": "101", "handle": "bob",   "display": "Bob",   "text": "second tweet"},
    ]
    summary = x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage)
    assert summary["fetched"] == 2
    assert summary["staged"] == 2
    assert summary["skipped_duplicate"] == 0
    assert len(calls) == 2
    assert calls[0]["url"] == "https://x.com/alice/status/100"
    assert calls[0]["author"] == "@alice (Alice)"
    # State persisted
    state = json.loads(isolated_state.read_text())
    assert sorted(state["processed_ids"]) == ["100", "101"]


def test_poll_personal_skips_already_seen(isolated_state):
    """Re-running on the same capture stages nothing the second time."""
    capture = [{"id": "200", "handle": "x", "display": "X", "text": "once"}]
    calls1, stage1 = _stub_stage_fn()
    s1 = x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage1)
    assert s1["staged"] == 1

    calls2, stage2 = _stub_stage_fn()
    s2 = x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage2)
    assert s2["staged"] == 0
    assert s2["skipped_duplicate"] == 1
    assert len(calls2) == 0


def test_poll_personal_dry_run_does_not_persist_state(isolated_state):
    capture = [{"id": "300", "handle": "y", "display": "Y", "text": "dry"}]
    _, stage = _stub_stage_fn()
    summary = x_personal.poll_personal(capture, state_file=isolated_state,
                                        stage_fn=stage, dry_run=True)
    assert summary["staged"] == 1
    assert summary["dry_run"] is True
    # State file should NOT have been written
    assert not isolated_state.exists()


def test_poll_personal_isolates_state_from_bird_path(isolated_state, tmp_path):
    """Personal state file is distinct from the bird path's state file —
    cross-account dedup contamination would be a serious bug."""
    bird_state = tmp_path / "bird-state.json"
    bird_state.write_text(json.dumps({"processed_ids": ["999"]}))

    # Tweet ID 999 was bookmarked on the bot account but Matias just bookmarked
    # the same tweet on his personal account — should still stage.
    capture = [{"id": "999", "handle": "z", "display": "Z", "text": "shared"}]
    _, stage = _stub_stage_fn()
    summary = x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage)
    assert summary["staged"] == 1   # Personal state didn't see "999"


def test_poll_personal_handles_capture_file_path(isolated_state, tmp_path):
    """Pass a Path to a JSON file → it gets loaded + processed."""
    fp = tmp_path / "capture.json"
    fp.write_text(json.dumps({"bookmarks": [
        {"id": "400", "handle": "p", "display": "P", "text": "from file"}
    ]}))
    _, stage = _stub_stage_fn()
    summary = x_personal.poll_personal(fp, state_file=isolated_state, stage_fn=stage)
    assert summary["staged"] == 1


def test_poll_personal_records_url_and_text_preview(isolated_state):
    capture = [{"id": "500", "handle": "alice", "display": "Alice",
                "text": "x" * 200}]
    _, stage = _stub_stage_fn()
    summary = x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage)
    item = summary["staged_items"][0]
    assert item["url"] == "https://x.com/alice/status/500"
    assert item["text_preview"] == "x" * 80   # truncated to 80 chars
    assert item["tweet_id"] == "500"


def test_poll_personal_keeps_state_bounded(isolated_state):
    """State trims to last 2000 IDs."""
    huge_state = {"processed_ids": [str(i) for i in range(5000)]}
    isolated_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_state.write_text(json.dumps(huge_state))

    capture = [{"id": "9999", "handle": "k", "display": "K", "text": "new"}]
    _, stage = _stub_stage_fn()
    x_personal.poll_personal(capture, state_file=isolated_state, stage_fn=stage)
    after = json.loads(isolated_state.read_text())
    assert len(after["processed_ids"]) == 2000


def test_capture_js_constant_is_present_and_self_contained():
    """Sanity: the JS the agent runs in Chrome is exposed + non-empty."""
    js = x_personal.BOOKMARKS_CAPTURE_JS
    assert "article[data-testid=\"tweet\"]" in js
    assert "/status/" in js
    # Returns an object — not a side-effect call
    assert "return" in js
    assert "count" in js and "bookmarks" in js
