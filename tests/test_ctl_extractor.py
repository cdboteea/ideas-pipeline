"""Behavioral tests for the CTL trade-idea extractor (#123).

We never call codex for real — we inject a fake llm_call that returns
canned JSON shaped like what gpt-5 actually produces. That isolates the
test from network/subprocess flakiness while still exercising every code
path the extractor takes.
"""
from __future__ import annotations
import json
from pathlib import Path

import duckdb
import pytest

from ideas import ctl_extractor as ctl
from ideas.ctl_extractor import (
    Classification,
    ExtractedIdea,
    classify_and_extract,
    parse_llm_response,
    is_valid_idea,
    process_posts,
    upsert_idea,
    list_recent_ideas,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redirect CTL DuckDB + state file to tmp_path."""
    db = tmp_path / "ctl.duckdb"
    state = tmp_path / "ctl-state.json"
    log = tmp_path / "ctl.log"
    monkeypatch.setattr(ctl, "CTL_DUCKDB_PATH", db)
    monkeypatch.setattr(ctl, "CTL_STATE_FILE", state)
    monkeypatch.setattr(ctl, "CTL_LOG_FILE", log)
    return {"db": db, "state": state, "log": log}


def _fake_llm(canned: dict | str):
    """Return a fake llm_call that always returns this canned response."""
    if isinstance(canned, dict):
        canned = json.dumps(canned)
    def call(_prompt: str) -> str:
        return canned
    return call


# ── parse_llm_response ──────────────────────────────────────────────────


def test_parse_llm_response_handles_bare_json():
    raw = '{"is_trade_idea": true, "classify_confidence": 0.9, "classify_reason": "x", "extraction": {"ticker":"$SB_F"}}'
    out = parse_llm_response(raw)
    assert out["is_trade_idea"] is True
    assert out["extraction"]["ticker"] == "$SB_F"


def test_parse_llm_response_handles_markdown_fence():
    raw = '```json\n{"is_trade_idea": false, "classify_confidence": 0.2, "classify_reason": "commentary", "extraction": null}\n```'
    out = parse_llm_response(raw)
    assert out["is_trade_idea"] is False
    assert out["extraction"] is None


def test_parse_llm_response_finds_json_inside_prose():
    raw = "Here's my analysis. The post is a trade idea.\n\n{\"is_trade_idea\": true, \"classify_confidence\": 0.85, \"classify_reason\": \"futures call\", \"extraction\": {\"ticker\": \"$ZS_F\", \"direction\": \"long\"}}"
    out = parse_llm_response(raw)
    assert out["extraction"]["direction"] == "long"


def test_parse_llm_response_raises_on_garbage():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_llm_response("totally not json no braces here")


# ── classify_and_extract ────────────────────────────────────────────────


def test_classify_extracts_ctlfutures_format():
    """$SB_F L here Risk 14.94 H4 #Swing — the canonical CTLFutures shape."""
    resp = {
        "is_trade_idea": True,
        "classify_confidence": 0.95,
        "classify_reason": "explicit futures entry with risk + horizon",
        "extraction": {
            "ticker": "$SB_F",
            "direction": "long",
            "entry_text": "here",
            "stop_text": "Risk 14.94",
            "stop_price": 14.94,
            "horizon": "H4",
            "tags": ["Swing"],
            "extract_confidence": 0.92,
        }
    }
    cls, idea = classify_and_extract("$SB_F L here Risk 14.94 H4 #Swing  *",
                                      llm_call=_fake_llm(resp))
    assert cls.is_trade_idea is True
    assert idea.ticker == "$SB_F"
    assert idea.direction == "long"
    assert idea.stop_price == 14.94
    assert idea.tags == ["Swing"]


def test_classify_skips_canuck2usa_commentary():
    """Philosophical post → not a trade idea."""
    resp = {
        "is_trade_idea": False,
        "classify_confidence": 0.9,
        "classify_reason": "commentary, no instrument or levels",
        "extraction": None,
    }
    cls, idea = classify_and_extract(
        "When analyzing longer-term strategies or patterns, it's important not to expect immediate results …",
        llm_call=_fake_llm(resp))
    assert cls.is_trade_idea is False
    assert idea is None
    assert "commentary" in cls.classify_reason


def test_classify_handles_canuck2usa_terse_entry():
    """$AMD 408 — entry signal."""
    resp = {
        "is_trade_idea": True,
        "classify_confidence": 0.7,
        "classify_reason": "ticker + price level → likely entry",
        "extraction": {
            "ticker": "$AMD",
            "direction": "long",
            "entry_text": "408",
            "entry_price": 408.0,
            "extract_confidence": 0.6,
        }
    }
    cls, idea = classify_and_extract("$AMD 408", llm_call=_fake_llm(resp))
    assert idea.entry_price == 408.0


def test_classify_returns_none_extraction_when_idea_but_empty():
    resp = {
        "is_trade_idea": True,
        "classify_confidence": 0.6,
        "extraction": None,
    }
    cls, idea = classify_and_extract("$X buy", llm_call=_fake_llm(resp))
    assert cls.is_trade_idea is True
    assert idea is None


# ── is_valid_idea ───────────────────────────────────────────────────────


def test_is_valid_idea_passes_well_formed():
    idea = ExtractedIdea(ticker="$SB_F", direction="long", entry_text="here",
                          stop_text="Risk 14.94", stop_price=14.94)
    ok, reason = is_valid_idea(idea)
    assert ok, reason


def test_is_valid_idea_rejects_missing_ticker():
    idea = ExtractedIdea(ticker=None, direction="long", entry_text="here")
    ok, reason = is_valid_idea(idea)
    assert not ok
    assert "ticker" in reason


def test_is_valid_idea_rejects_invalid_direction():
    idea = ExtractedIdea(ticker="$AMD", direction="maybe", entry_text="408")
    ok, reason = is_valid_idea(idea)
    assert not ok
    assert "direction" in reason


def test_is_valid_idea_rejects_no_levels_no_direction():
    idea = ExtractedIdea(ticker="$AMD")  # no direction, no entry/stop/target
    ok, reason = is_valid_idea(idea)
    assert not ok


def test_is_valid_idea_accepts_direction_only():
    """A direction call without numbers is still actionable."""
    idea = ExtractedIdea(ticker="$AMD", direction="long")
    ok, _ = is_valid_idea(idea)
    assert ok


def test_is_valid_idea_rejects_garbage_ticker():
    idea = ExtractedIdea(ticker="not a ticker", direction="long")
    ok, _ = is_valid_idea(idea)
    assert not ok


# ── upsert_idea + DuckDB persistence ────────────────────────────────────


def test_upsert_idea_persists_and_is_idempotent(isolated_db):
    cls = Classification(is_trade_idea=True, classify_confidence=0.9)
    idea = ExtractedIdea(ticker="$SB_F", direction="long", entry_text="here",
                          stop_text="Risk 14.94", stop_price=14.94, horizon="H4",
                          tags=["Swing"])
    upsert_idea(tweet_id="100", posted_at="2026-05-04T18:00:00Z",
                 author_handle="CTLFutures", raw_text="$SB_F L here Risk 14.94 H4 #Swing",
                 cls=cls, idea=idea)
    # Re-upsert — should still have exactly 1 row
    upsert_idea(tweet_id="100", posted_at="2026-05-04T18:00:00Z",
                 author_handle="CTLFutures", raw_text="updated text",
                 cls=cls, idea=idea)
    con = duckdb.connect(str(isolated_db["db"]))
    rows = con.execute("SELECT tweet_id, ticker, raw_text FROM trade_ideas").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][1] == "$SB_F"
    assert rows[0][2] == "updated text"   # second upsert won


def test_upsert_idea_stores_commentary_with_idea_null(isolated_db):
    cls = Classification(is_trade_idea=False, classify_confidence=0.95,
                          classify_reason="philosophy")
    upsert_idea(tweet_id="200", posted_at="2026-05-05T16:00:00Z",
                 author_handle="canuck2usa", raw_text="trading is a journey",
                 cls=cls, idea=None)
    con = duckdb.connect(str(isolated_db["db"]))
    rows = con.execute("SELECT is_trade_idea, ticker FROM trade_ideas").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][0] is False
    assert rows[0][1] is None


def test_list_recent_ideas_returns_only_actionable(isolated_db):
    cls_idea = Classification(is_trade_idea=True, classify_confidence=0.9)
    cls_skip = Classification(is_trade_idea=False, classify_confidence=0.9)
    upsert_idea(tweet_id="A", posted_at="2026-05-05T10:00:00Z",
                 author_handle="X", raw_text="x",
                 cls=cls_idea,
                 idea=ExtractedIdea(ticker="$AMD", direction="long",
                                     entry_text="408"))
    upsert_idea(tweet_id="B", posted_at="2026-05-05T11:00:00Z",
                 author_handle="X", raw_text="commentary",
                 cls=cls_skip, idea=None)
    out = list_recent_ideas()
    assert len(out) == 1
    assert out[0]["tweet_id"] == "A"


# ── End-to-end pipeline (process_posts) ────────────────────────────────


def test_process_posts_handles_mixed_batch(isolated_db):
    """Batch of 3 posts: 1 idea, 1 commentary, 1 already-seen."""
    posts = [
        {"id": "1", "text": "$SB_F L here Risk 14.94 H4 #Swing",
         "created_at": "2026-05-04T18:00:00Z",
         "author": {"username": "CTLFutures", "name": "CTLFutures"}},
        {"id": "2", "text": "trading is mostly waiting",
         "created_at": "2026-05-04T18:30:00Z",
         "author": {"username": "canuck2usa", "name": "CtheLight"}},
        {"id": "3", "text": "$ZS_F L here Risk 1164 Hard",
         "created_at": "2026-05-04T19:00:00Z",
         "author": {"username": "CTLFutures", "name": "CTLFutures"}},
    ]

    # Pre-mark id "3" as seen
    isolated_db["state"].write_text(json.dumps({"processed_ids": ["3"]}))

    # Inject a fake LLM that varies its response by the post body. The
    # extractor prompt embeds the body between "--- POST ---" markers, so we
    # extract THAT — not the full prompt — to avoid false matches against
    # examples in the extractor template.
    def fake_llm(prompt: str) -> str:
        body = prompt.split("--- POST ---", 1)[-1].split("--- END POST ---", 1)[0]
        if "Risk 14.94" in body:
            return json.dumps({
                "is_trade_idea": True, "classify_confidence": 0.95,
                "classify_reason": "structured futures",
                "extraction": {
                    "ticker": "$SB_F", "direction": "long",
                    "entry_text": "here", "stop_text": "Risk 14.94",
                    "stop_price": 14.94, "horizon": "H4", "tags": ["Swing"],
                    "extract_confidence": 0.9,
                }
            })
        if "Risk 1164" in body:
            return json.dumps({
                "is_trade_idea": True, "classify_confidence": 0.9,
                "extraction": {"ticker": "$ZS_F", "direction": "long",
                                "entry_text": "here", "stop_text": "Risk 1164",
                                "extract_confidence": 0.85},
            })
        return json.dumps({
            "is_trade_idea": False, "classify_confidence": 0.95,
            "classify_reason": "commentary", "extraction": None,
        })

    summary = process_posts(posts, llm_call=fake_llm, model="gpt-5",
                             state_file=isolated_db["state"])
    assert summary["fetched"] == 3
    assert summary["skipped_seen"] == 1
    assert summary["classified_idea"] == 1   # $SB_F (id 3 was skipped before classify)
    assert summary["classified_skip"] == 1   # commentary
    assert summary["extracted_valid"] == 1
    assert summary["extracted_dropped"] == 0

    # State updated
    state2 = json.loads(isolated_db["state"].read_text())
    assert "1" in state2["processed_ids"]
    assert "2" in state2["processed_ids"]   # commentary persists too
    assert "3" in state2["processed_ids"]


def test_process_posts_dry_run_does_not_persist(isolated_db):
    posts = [{"id": "x1", "text": "$AMD 408", "created_at": "2026-05-05T15:00:00Z",
              "author": {"username": "canuck2usa", "name": "CtheLight"}}]
    fake = _fake_llm({"is_trade_idea": True, "classify_confidence": 0.7,
                      "extraction": {"ticker": "$AMD", "direction": "long",
                                      "entry_text": "408", "entry_price": 408.0,
                                      "extract_confidence": 0.6}})
    summary = process_posts(posts, dry_run=True, llm_call=fake,
                             state_file=isolated_db["state"])
    assert summary["extracted_valid"] == 1
    # State should NOT have been written
    assert not isolated_db["state"].exists()
    # DB should not have the row
    if isolated_db["db"].exists():
        con = duckdb.connect(str(isolated_db["db"]))
        n = con.execute("SELECT COUNT(*) FROM trade_ideas").fetchone()[0]
        con.close()
        assert n == 0


def test_process_posts_records_llm_errors(isolated_db):
    posts = [{"id": "boom", "text": "anything", "created_at": "2026-05-05T15:00:00Z",
              "author": {"username": "X"}}]
    def angry_llm(prompt: str) -> str:
        raise RuntimeError("codex blew up")
    summary = process_posts(posts, llm_call=angry_llm,
                             state_file=isolated_db["state"])
    assert summary["fetched"] == 1
    assert len(summary["errors"]) == 1
    assert "codex blew up" in summary["errors"][0]["error"]


def test_process_posts_validation_drops_low_quality(isolated_db):
    """LLM thinks it's an idea but extraction missing required fields."""
    posts = [{"id": "wreck", "text": "$AMD nice", "created_at": "2026-05-05T15:00:00Z",
              "author": {"username": "x"}}]
    fake = _fake_llm({
        "is_trade_idea": True, "classify_confidence": 0.5,
        "extraction": {"ticker": "$AMD"},   # no direction/entry/stop/target → invalid
    })
    summary = process_posts(posts, llm_call=fake,
                             state_file=isolated_db["state"])
    assert summary["classified_idea"] == 1
    assert summary["extracted_valid"] == 0
    assert summary["extracted_dropped"] == 1
    assert any("validation" in e["error"] for e in summary["errors"])


def test_normalize_timestamp_handles_unparseable_gracefully(isolated_db):
    """Bad timestamps shouldn't kill the upsert — they get stored as NULL."""
    cls = Classification(is_trade_idea=True, classify_confidence=0.9)
    idea = ExtractedIdea(ticker="$AMD", direction="long")
    upsert_idea(tweet_id="bad-ts", posted_at="not-a-date",
                 author_handle="x", raw_text="x", cls=cls, idea=idea)
    con = duckdb.connect(str(isolated_db["db"]))
    rows = con.execute("SELECT tweet_id, posted_at FROM trade_ideas").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][0] == "bad-ts"
    assert rows[0][1] is None   # bad timestamp stored as NULL


def test_normalize_timestamp_accepts_z_suffix(isolated_db):
    cls = Classification(is_trade_idea=True, classify_confidence=0.9)
    idea = ExtractedIdea(ticker="$AMD", direction="long")
    upsert_idea(tweet_id="ts-z", posted_at="2026-05-05T08:13:14.000Z",
                 author_handle="x", raw_text="x", cls=cls, idea=idea)
    con = duckdb.connect(str(isolated_db["db"]))
    rows = con.execute("SELECT posted_at FROM trade_ideas").fetchall()
    con.close()
    assert rows[0][0] is not None  # stored


def test_process_posts_writes_log_line(isolated_db):
    posts = [{"id": "L1", "text": "$AMD 408", "created_at": "2026-05-05T15:00:00Z",
              "author": {"username": "x"}}]
    fake = _fake_llm({"is_trade_idea": True, "classify_confidence": 0.7,
                      "extraction": {"ticker": "$AMD", "direction": "long",
                                      "entry_text": "408", "extract_confidence": 0.6}})
    process_posts(posts, llm_call=fake, state_file=isolated_db["state"])
    assert isolated_db["log"].exists()
    line = isolated_db["log"].read_text()
    assert "fetched=1" in line
    assert "new_ideas=1" in line
