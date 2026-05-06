"""Behavioral tests for #123 v2 — thread linkage + alert hook.

These tests run against an isolated DuckDB so the production CTL DB is
never touched. The existing v1 codex-stub tests still pass alongside
these — see test_ctl_extractor.py.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from ideas import ctl_extractor as ctl
from ideas import ctl_threads
from ideas.ctl_extractor import (
    Classification, ExtractedIdea, process_posts, upsert_idea,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "ctl.duckdb"
    state = tmp_path / "state.json"
    log = tmp_path / "ctl.log"
    alerts = tmp_path / "ctl-alerts.log"
    monkeypatch.setattr(ctl, "CTL_DUCKDB_PATH", db)
    monkeypatch.setattr(ctl, "CTL_STATE_FILE", state)
    monkeypatch.setattr(ctl, "CTL_LOG_FILE", log)
    monkeypatch.setattr(ctl_threads, "CTL_ALERT_LOG_FILE", alerts)
    return {"db": db, "state": state, "log": log, "alerts": alerts}


def _ts(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _post_idea(tid: str, author: str, ticker: str, *,
                direction="long", entry_text="here", entry_price=None,
                stop_text=None, stop_price=None, target_text=None,
                target_price=None, horizon=None, intent="new",
                posted_at: datetime | None = None,
                is_idea: bool = True):
    """Insert a post + thread-link it. Returns (thread_id, action, thread_state)."""
    posted_at = posted_at or datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    cls = Classification(is_trade_idea=is_idea, classify_confidence=0.9,
                           thread_intent=intent)
    idea = ExtractedIdea(ticker=ticker, direction=direction,
                          entry_text=entry_text, entry_price=entry_price,
                          stop_text=stop_text, stop_price=stop_price,
                          target_text=target_text, target_price=target_price,
                          horizon=horizon, extract_confidence=0.9) if is_idea else None
    upsert_idea(tweet_id=tid, posted_at=_ts(posted_at), author_handle=author,
                 raw_text=f"post-{tid}", cls=cls, idea=idea)
    return ctl_threads.upsert_thread_for_post(
        tweet_id=tid, posted_at=_ts(posted_at), author=author, ticker=ticker,
        cls=cls, idea=idea,
    )


# ── Threading semantics ──────────────────────────────────────────────────


def test_first_post_with_intent_new_opens_thread(isolated_db):
    tid, action, st = _post_idea("p1", "CTLFutures", "$SB_F", intent="new",
                                   stop_text="Risk 14.94", stop_price=14.94)
    assert action == "opened_new"
    assert st["state"] == "open"
    assert st["post_count"] == 1
    assert st["current_stop_price"] == 14.94


def test_update_post_appends_to_open_thread(isolated_db):
    t1 = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(hours=2)
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new",
                stop_text="Risk 14.94", stop_price=14.94, posted_at=t1)
    tid2, action, st = _post_idea("p2", "CTLFutures", "$SB_F", intent="update",
                                    target_text="trim 1/4 at 1235-40",
                                    target_price=1235.0, posted_at=t2)
    assert action == "appended_to_open"
    assert st["post_count"] == 2
    # Stop carried forward, target newly added
    assert st["current_stop_price"] == 14.94
    assert st["current_target_price"] == 1235.0


def test_new_intent_when_open_thread_exists_marks_old_stale(isolated_db):
    t1 = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(days=3)
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new",
                stop_price=14.94, posted_at=t1)
    tid2, action, st2 = _post_idea("p2", "CTLFutures", "$SB_F", intent="new",
                                     stop_price=12.94, posted_at=t2)
    assert action == "stale_then_opened_new"
    # Two thread rows now: one stale, one open
    threads = ctl_threads.list_threads(author="CTLFutures")
    assert len(threads) == 2
    states = sorted(t["state"] for t in threads)
    assert states == ["open", "stale"]


def test_close_intent_on_open_thread_closes_it(isolated_db):
    t1 = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(days=2)
    tid1, _, _ = _post_idea("p1", "CTLFutures", "$SB_F", intent="new",
                              stop_price=14.94, posted_at=t1)
    tid2, action, st = _post_idea("p2", "CTLFutures", "$SB_F", intent="close",
                                    entry_text="out", posted_at=t2)
    assert action == "closed_existing"
    assert tid2 == tid1
    assert st["state"] == "closed"
    assert st["closed_reason"] == "explicit_close"


def test_close_intent_with_no_open_thread_creates_orphan(isolated_db):
    tid, action, st = _post_idea("p1", "canuck2usa", "$AMD", intent="close",
                                   entry_text="stopped out")
    assert action == "closed_orphan"
    assert st["state"] == "closed"
    assert st["closed_reason"] == "explicit_close_no_open"


def test_unsure_intent_appends_when_open_thread_exists(isolated_db):
    t1 = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(hours=4)
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new",
                stop_price=14.94, posted_at=t1)
    tid, action, st = _post_idea("p2", "CTLFutures", "$SB_F", intent="unsure",
                                   posted_at=t2)
    assert action == "appended_to_open"
    assert st["post_count"] == 2


def test_different_authors_dont_share_threads(isolated_db):
    _post_idea("p1", "CTLFutures", "$AMD", intent="new")
    _post_idea("p2", "canuck2usa", "$AMD", intent="new")
    threads = ctl_threads.list_threads()
    assert len(threads) == 2
    assert {t["author"] for t in threads} == {"CTLFutures", "canuck2usa"}


def test_different_tickers_dont_share_threads(isolated_db):
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new")
    _post_idea("p2", "CTLFutures", "$ZS_F", intent="new")
    threads = ctl_threads.list_threads()
    assert len(threads) == 2


def test_thread_lookup_window_excludes_old_threads(isolated_db):
    """A 'new' post 90 days after the last update opens a fresh thread,
    not append to the stale one — but here we test that an OLD thread
    won't be matched even for an 'update' intent."""
    t_old = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    t_now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new", posted_at=t_old)
    tid2, action, _ = _post_idea("p2", "CTLFutures", "$SB_F",
                                   intent="update", posted_at=t_now)
    # Old thread is outside the 60-day lookup window → new one opened
    assert action == "opened_new"


def test_commentary_post_still_links_to_open_thread(isolated_db):
    """A non-trade-idea post on the same ticker still rolls into the
    thread (so we get the 'last_update_at' bump and the text in the trace)."""
    _post_idea("p1", "CTLFutures", "$SB_F", intent="new", stop_price=14.94)
    # Now a commentary post — is_idea=False
    tid2, action, st = _post_idea(
        "p2", "CTLFutures", "$SB_F", intent="unsure", is_idea=False,
        posted_at=datetime(2026, 5, 6, 14, 0, tzinfo=timezone.utc),
    )
    assert action == "appended_to_open"
    assert st["post_count"] == 2


def test_commentary_with_no_open_thread_skips(isolated_db):
    tid, action, st = _post_idea("p1", "CTLFutures", "$SB_F", is_idea=False,
                                   intent="unsure")
    assert action == "skipped_no_thread"
    assert st == {}
    threads = ctl_threads.list_threads()
    assert len(threads) == 0


# ── Alert hook ───────────────────────────────────────────────────────────


def test_maybe_alert_writes_to_log_channel(isolated_db):
    ctl.ensure_schema()
    ev = ctl_threads.AlertEvent(
        thread_id="t1", event_type="new_thread",
        summary="🆕 $SB_F L @CTLFutures", payload={"k": "v"},
    )
    out = ctl_threads.maybe_alert(ev)
    assert out["dispatched"] is True
    assert out["channel"] == "log"
    assert isolated_db["alerts"].exists()
    line = isolated_db["alerts"].read_text()
    assert "[new_thread]" in line
    assert "thread=t1" in line


def test_maybe_alert_throttles_duplicate_events(isolated_db):
    ctl.ensure_schema()
    ev = ctl_threads.AlertEvent(
        thread_id="t1", event_type="update",
        summary="x", payload={},
    )
    out1 = ctl_threads.maybe_alert(ev)
    out2 = ctl_threads.maybe_alert(ev)
    assert out1["dispatched"] is True
    assert out2["dispatched"] is False
    assert out2["reason"] == "throttled"


def test_maybe_alert_lets_different_event_types_through(isolated_db):
    ctl.ensure_schema()
    e_new = ctl_threads.AlertEvent(thread_id="t1", event_type="new_thread",
                                     summary="x", payload={})
    e_upd = ctl_threads.AlertEvent(thread_id="t1", event_type="update",
                                     summary="y", payload={})
    out_new = ctl_threads.maybe_alert(e_new)
    out_upd = ctl_threads.maybe_alert(e_upd)
    assert out_new["dispatched"] and out_upd["dispatched"]


def test_maybe_alert_telegram_channel_is_unwired(isolated_db):
    ctl.ensure_schema()
    ev = ctl_threads.AlertEvent(thread_id="t1", event_type="new_thread",
                                  summary="x", payload={})
    with pytest.raises(NotImplementedError):
        ctl_threads.maybe_alert(ev, channel="telegram")


def test_maybe_alert_records_to_ctl_alert_log_table(isolated_db):
    ctl.ensure_schema()
    ev = ctl_threads.AlertEvent(thread_id="t99", event_type="target_hit",
                                  summary="🎯 $AMD +5.9%", payload={"pnl": 5.9})
    ctl_threads.maybe_alert(ev)
    con = duckdb.connect(str(isolated_db["db"]))
    rows = con.execute("SELECT thread_id, event_type, channel, payload "
                        "FROM ctl_alert_log").fetchall()
    con.close()
    assert len(rows) == 1
    assert rows[0][0] == "t99"
    assert rows[0][1] == "target_hit"
    assert rows[0][2] == "log"
    parsed = json.loads(rows[0][3])
    assert parsed["pnl"] == 5.9


# ── End-to-end integration via process_posts ────────────────────────────


def test_process_posts_creates_threads_and_alerts(isolated_db):
    """Full pipeline: 3 posts (2 ideas, 1 commentary) → 1 thread, 1 new_thread alert."""
    posts = [
        {"id": "1", "text": "$SB_F L here Risk 14.94 H4 #Swing",
         "created_at": "2026-05-06T12:00:00Z",
         "author": {"username": "CTLFutures", "name": "CTLFutures"}},
        {"id": "2", "text": "$SB_F nice move so far",
         "created_at": "2026-05-06T15:00:00Z",
         "author": {"username": "CTLFutures", "name": "CTLFutures"}},
        {"id": "3", "text": "$SB_F trim 1/4 at 1235-40",
         "created_at": "2026-05-06T18:00:00Z",
         "author": {"username": "CTLFutures", "name": "CTLFutures"}},
    ]

    # Per the v2 prompt: position updates referencing a specific instrument
    # ARE trade ideas (with thread_intent=update), not pure commentary.
    # "$SB_F nice move so far" is a position update, so the LLM returns
    # is_trade_idea=True + thread_intent=update.
    def fake_llm(prompt: str) -> str:
        body = prompt.split("--- POST ---", 1)[-1].split("--- END POST ---", 1)[0]
        if "Risk 14.94" in body:
            return json.dumps({
                "is_trade_idea": True, "classify_confidence": 0.95,
                "thread_intent": "new",
                "extraction": {"ticker": "$SB_F", "direction": "long",
                                "entry_text": "here", "stop_text": "Risk 14.94",
                                "stop_price": 14.94, "horizon": "H4",
                                "tags": ["Swing"], "extract_confidence": 0.97},
            })
        if "trim 1/4" in body:
            return json.dumps({
                "is_trade_idea": True, "classify_confidence": 0.85,
                "thread_intent": "update",
                "extraction": {"ticker": "$SB_F", "direction": "long",
                                "target_text": "trim 1/4 at 1235-40",
                                "target_price": 1235.0,
                                "extract_confidence": 0.7},
            })
        # "nice move so far" — position-update commentary on $SB_F
        return json.dumps({
            "is_trade_idea": True, "classify_confidence": 0.7,
            "thread_intent": "update",
            "extraction": {"ticker": "$SB_F", "direction": "long",
                            "extract_confidence": 0.4},
        })

    summary = process_posts(posts, llm_call=fake_llm,
                             state_file=isolated_db["state"])
    assert summary["fetched"] == 3
    assert summary["classified_idea"] == 3     # all three are ideas (one update is light)
    assert summary["classified_skip"] == 0
    # Validation drops the "nice move" one because no entry/stop/target/direction-only
    # — wait, direction IS present (long), so it passes
    assert summary["extracted_valid"] == 3

    threads = ctl_threads.list_threads()
    assert len(threads) == 1
    t = threads[0]
    assert t["state"] == "open"
    assert t["author"] == "CTLFutures"
    assert t["ticker"] == "$SB_F"
    assert t["post_count"] == 3   # all three linked
    assert t["current_stop_price"] == 14.94    # carried from post 1
    assert t["current_target_price"] == 1235.0 # added in post 3

    # Two log-channel alerts: new_thread for post 1, update for post 3
    con = duckdb.connect(str(isolated_db["db"]))
    alerts = con.execute("SELECT event_type FROM ctl_alert_log "
                          "ORDER BY alerted_at").fetchall()
    con.close()
    types = [a[0] for a in alerts]
    assert "new_thread" in types
    assert "update" in types


def test_process_posts_thread_link_off_skips_threads(isolated_db):
    posts = [{"id": "1", "text": "$AMD 408",
              "created_at": "2026-05-06T12:00:00Z",
              "author": {"username": "x"}}]
    fake = lambda p: json.dumps({
        "is_trade_idea": True, "classify_confidence": 0.7,
        "thread_intent": "new",
        "extraction": {"ticker": "$AMD", "direction": "long",
                        "entry_text": "408", "extract_confidence": 0.6},
    })
    process_posts(posts, llm_call=fake, state_file=isolated_db["state"],
                   thread_link=False)
    assert len(ctl_threads.list_threads()) == 0


def test_thread_intent_persists_to_trade_ideas_row(isolated_db):
    posts = [{"id": "T1", "text": "$AMD 408",
              "created_at": "2026-05-06T12:00:00Z",
              "author": {"username": "x"}}]
    fake = lambda p: json.dumps({
        "is_trade_idea": True, "classify_confidence": 0.7,
        "thread_intent": "new",
        "extraction": {"ticker": "$AMD", "direction": "long",
                        "entry_text": "408", "extract_confidence": 0.6},
    })
    process_posts(posts, llm_call=fake, state_file=isolated_db["state"])
    con = duckdb.connect(str(isolated_db["db"]))
    row = con.execute("SELECT thread_id, thread_intent FROM trade_ideas "
                       "WHERE tweet_id = 'T1'").fetchone()
    con.close()
    assert row[0] is not None      # thread linked
    assert row[1] == "new"


# ── LLM JSON parsing for thread_intent ──────────────────────────────────


def test_classify_extracts_thread_intent_field():
    from ideas.ctl_extractor import classify_and_extract
    fake = lambda p: json.dumps({
        "is_trade_idea": True, "classify_confidence": 0.9,
        "thread_intent": "update",
        "extraction": {"ticker": "$SB_F", "direction": "long",
                        "entry_text": "here"},
    })
    cls, idea = classify_and_extract("test", llm_call=fake)
    assert cls.thread_intent == "update"


def test_classify_normalizes_unknown_thread_intent_to_unsure():
    from ideas.ctl_extractor import classify_and_extract
    fake = lambda p: json.dumps({
        "is_trade_idea": True, "classify_confidence": 0.5,
        "thread_intent": "GIBBERISH",
        "extraction": {"ticker": "$X", "direction": "long",
                        "entry_text": "here"},
    })
    cls, _ = classify_and_extract("test", llm_call=fake)
    assert cls.thread_intent == "unsure"


def test_classify_defaults_thread_intent_when_missing():
    from ideas.ctl_extractor import classify_and_extract
    fake = lambda p: json.dumps({
        "is_trade_idea": False, "classify_confidence": 0.9,
        "extraction": None,
    })
    cls, _ = classify_and_extract("test", llm_call=fake)
    assert cls.thread_intent == "unsure"
