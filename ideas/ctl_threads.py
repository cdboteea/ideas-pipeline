"""CTL thread tracking — task #123 v2.

Groups individual `trade_ideas` rows (one row per post) into logical
`trade_threads` (one row per logical trade). Each thread has a state
(open/partial/closed/stale/unknown) and rolling current state
(current_entry/stop/target/horizon).

The LLM's `thread_intent` field on each post drives the thread-linking
decision:
  - "new"    → open a new thread (mark any prior open thread on same
              (author, ticker) as stale)
  - "update" → append to the most recent open thread on (author, ticker)
              within the lookup window; if none exists, open new
  - "close"  → close the most recent open thread; if none exists, open
              new (closed-immediately) for record-keeping
  - "unsure" → append to existing open thread if there is one within the
              lookup window; otherwise open new (optimistic)

Thread state transitions also fire `maybe_alert` events. Today the alert
channel is "log only" — the schema and de-dup logic live here so we can
swap in Telegram later without touching call sites.

See ~/clawd/research/workflows/ctl-pipeline-v2-2026-05-06.md for the
full spec.
"""
from __future__ import annotations
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from . import ctl_extractor as ctl


# Lookup window for thread-matching: posts on the same (author, ticker)
# updated within this window are considered candidates for the same thread.
CTL_THREAD_WINDOW_DAYS = 60

# Stale threshold: an open thread with no update in this many days is
# eligible for stale-marking by the enrich job.
CTL_STALE_DAYS = 14

# Alert throttling: minimum time between two alerts of the same event_type
# on the same thread (mobile-friendly back-pressure).
CTL_ALERT_MIN_INTERVAL = timedelta(minutes=60)

# Default alert channel today. "log" writes to ~/clawd/logs/ctl-alerts.log.
# Swap to "telegram" once the channel is wired (spec only today).
CTL_ALERT_CHANNEL_DEFAULT = "log"

CTL_ALERT_LOG_FILE = Path.home() / "clawd" / "logs" / "ctl-alerts.log"

# Per-user "last check" divider for `ideas ctl-status --since-last-check`.
# Bumped whenever the user runs ctl-status with --since-last-check (mirrors
# the inbox-status / review interactive pattern from #122 v2).
CTL_LAST_CHECK_FILE = Path.home() / "clawd" / "data" / "ctl-last-check.json"


def get_last_check_at(state_file: Path | None = None) -> datetime | None:
    target = state_file or CTL_LAST_CHECK_FILE
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
        ts = data.get("last_check_at")
        if not ts:
            return None
        return datetime.fromisoformat(ts.replace("Z", "+00:00") if ts.endswith("Z") else ts)
    except (ValueError, json.JSONDecodeError):
        return None


def mark_check_now(state_file: Path | None = None) -> datetime:
    target = state_file or CTL_LAST_CHECK_FILE
    now = datetime.now(timezone.utc)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "last_check_at": now.isoformat(timespec="seconds"),
    }, indent=2))
    return now


def ctl_status(*, since_last_check: bool = False,
                open_only: bool = False,
                author: str | None = None,
                state_file: Path | None = None) -> dict:
    """Session-start surfacing primitive — same shape as ideas inbox-status.

    Returns:
      {
        "since":              ISO timestamp of last check (or null),
        "new_threads":        list of thread dicts opened after `since`
        "updated_threads":    list of thread dicts last_update_at after `since`
                               EXCLUDING new_threads
        "open_no_recent":     list of open threads with no update >24h
        "totals":             { "new": N, "updated": N, "open_total": N }
      }
    """
    last = get_last_check_at(state_file) if since_last_check else None
    last_iso = last.isoformat(timespec="seconds") if last else None

    threads = list_threads(author=author, limit=500)
    if open_only:
        threads = [t for t in threads if t["state"] in ("open", "partial")]

    new_threads      = []
    updated_threads  = []
    open_no_recent   = []
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    for t in threads:
        opened    = str(t.get("opened_at") or "")
        updated   = str(t.get("last_update_at") or "")
        is_open   = t["state"] in ("open", "partial")

        if since_last_check and last_iso:
            if opened and opened > last_iso:
                new_threads.append(t)
            elif updated and updated > last_iso:
                updated_threads.append(t)
        else:
            # No last_check_at — everything in `open_only` mode is "current"
            if is_open:
                if opened and opened > cutoff_24h:
                    new_threads.append(t)
                else:
                    open_no_recent.append(t)

        # Open threads with no update in 24h
        if is_open and updated and updated < cutoff_24h:
            # Don't double-count
            if t not in new_threads and t not in updated_threads:
                if t not in open_no_recent:
                    open_no_recent.append(t)

    return {
        "since":           last_iso,
        "new_threads":     new_threads,
        "updated_threads": updated_threads,
        "open_no_recent":  open_no_recent,
        "totals": {
            "new":        len(new_threads),
            "updated":    len(updated_threads),
            "open_total": sum(1 for t in threads if t["state"] in ("open","partial")),
            "all":        len(threads),
        },
    }


# ── Thread linkage ──────────────────────────────────────────────────────


def find_open_thread(*, author: str, ticker: str,
                      window_days: int = CTL_THREAD_WINDOW_DAYS,
                      con=None) -> dict | None:
    """Most-recent open|partial thread for (author, ticker) updated within
    `window_days`. Returns the row as a dict or None."""
    own_con = con is None
    if own_con:
        ctl.ensure_schema()
        con = ctl._con()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        rows = con.execute("""
            SELECT thread_id, author, ticker, direction, state,
                   opened_at, last_update_at,
                   current_entry_text, current_entry_price,
                   current_stop_text, current_stop_price,
                   current_target_text, current_target_price,
                   current_horizon, post_count, notes
            FROM trade_threads
            WHERE author = ?
              AND ticker = ?
              AND state IN ('open', 'partial')
              AND last_update_at >= ?
            ORDER BY last_update_at DESC
            LIMIT 1
        """, [author, ticker, cutoff]).fetchall()
        if not rows:
            return None
        cols = ["thread_id","author","ticker","direction","state","opened_at",
                "last_update_at","current_entry_text","current_entry_price",
                "current_stop_text","current_stop_price","current_target_text",
                "current_target_price","current_horizon","post_count","notes"]
        return dict(zip(cols, rows[0]))
    finally:
        if own_con:
            con.close()


def _new_thread_id() -> str:
    return f"ctl-{uuid.uuid4().hex[:12]}"


def _normalize_ts(ts) -> datetime:
    """Best-effort parse → UTC-aware datetime; falls back to now()."""
    if ts is None or ts == "":
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip()
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        d = datetime.fromisoformat(s2)
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _coalesce(*vals):
    """First non-None value, or None if all are None."""
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def upsert_thread_for_post(*, tweet_id: str, posted_at,
                            author: str, ticker: str,
                            cls: ctl.Classification,
                            idea: ctl.ExtractedIdea | None,
                            con=None) -> tuple[str, str, dict]:
    """Apply the post to the right thread; return (thread_id, action, thread_state).

    `action` is one of:
      - "opened_new"             — fresh thread created
      - "appended_to_open"       — existing open thread updated
      - "stale_then_opened_new"  — existing open thread marked stale,
                                   new thread opened (the LLM said
                                   "new" but a recent open thread existed)
      - "closed_existing"        — explicit close on an existing open thread
      - "closed_orphan"          — explicit close but no open thread; record
                                   one anyway for trace
    """
    own_con = con is None
    if own_con:
        ctl.ensure_schema()
        con = ctl._con()
    try:
        now_dt = _normalize_ts(posted_at)
        intent = cls.thread_intent if cls.is_trade_idea else "unsure"

        existing = find_open_thread(author=author, ticker=ticker, con=con)

        # Decide action
        if not cls.is_trade_idea:
            # Commentary still gets thread linkage if there's an open one
            if existing:
                _append_post_to_thread(con, existing["thread_id"],
                                        now_dt, idea, cls, tweet_id)
                return existing["thread_id"], "appended_to_open", \
                       _read_thread(con, existing["thread_id"])
            # No open thread for commentary → no thread row created
            return None, "skipped_no_thread", {}

        if intent == "new":
            if existing:
                _mark_thread_state(con, existing["thread_id"], "stale", now_dt,
                                    closed_reason="superseded_by_new_call")
                action = "stale_then_opened_new"
            else:
                action = "opened_new"
            tid = _create_thread(con, author=author, ticker=ticker,
                                  direction=(idea.direction if idea else None),
                                  posted_at=now_dt, idea=idea, cls=cls,
                                  tweet_id=tweet_id)
            return tid, action, _read_thread(con, tid)

        if intent == "close":
            if existing:
                _append_post_to_thread(con, existing["thread_id"],
                                        now_dt, idea, cls, tweet_id)
                _mark_thread_state(con, existing["thread_id"], "closed",
                                    now_dt, closed_reason="explicit_close")
                return existing["thread_id"], "closed_existing", \
                       _read_thread(con, existing["thread_id"])
            # Close-without-open → record an orphan thread for the trace
            tid = _create_thread(con, author=author, ticker=ticker,
                                  direction=(idea.direction if idea else None),
                                  posted_at=now_dt, idea=idea, cls=cls,
                                  tweet_id=tweet_id, initial_state="closed",
                                  closed_reason="explicit_close_no_open")
            return tid, "closed_orphan", _read_thread(con, tid)

        # intent == "update" or "unsure"
        if existing:
            _append_post_to_thread(con, existing["thread_id"],
                                    now_dt, idea, cls, tweet_id)
            return existing["thread_id"], "appended_to_open", \
                   _read_thread(con, existing["thread_id"])
        # Unsure with no open thread → open new (treat as fresh)
        tid = _create_thread(con, author=author, ticker=ticker,
                              direction=(idea.direction if idea else None),
                              posted_at=now_dt, idea=idea, cls=cls,
                              tweet_id=tweet_id)
        return tid, "opened_new", _read_thread(con, tid)
    finally:
        if own_con:
            con.close()


def _create_thread(con, *, author: str, ticker: str, direction: str | None,
                    posted_at: datetime, idea: ctl.ExtractedIdea | None,
                    cls: ctl.Classification, tweet_id: str,
                    initial_state: str = "open",
                    closed_reason: str | None = None) -> str:
    tid = _new_thread_id()
    ts = posted_at.isoformat()
    closed_at = ts if initial_state == "closed" else None
    con.execute("""
        INSERT INTO trade_threads (
            thread_id, author, ticker, direction, state,
            opened_at, last_update_at, closed_at,
            current_entry_text, current_entry_price,
            current_stop_text, current_stop_price,
            current_target_text, current_target_price,
            current_horizon, post_count, closed_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, [
        tid, author, ticker, direction, initial_state,
        ts, ts, closed_at,
        idea.entry_text if idea else None,
        idea.entry_price if idea else None,
        idea.stop_text if idea else None,
        idea.stop_price if idea else None,
        idea.target_text if idea else None,
        idea.target_price if idea else None,
        idea.horizon if idea else None,
        closed_reason,
    ])
    # Link the post to this thread
    con.execute("UPDATE trade_ideas SET thread_id = ?, thread_intent = ? "
                 "WHERE tweet_id = ?",
                 [tid, cls.thread_intent, tweet_id])
    con.commit()
    return tid


def _append_post_to_thread(con, thread_id: str, posted_at: datetime,
                             idea: ctl.ExtractedIdea | None,
                             cls: ctl.Classification, tweet_id: str) -> None:
    """Roll forward the thread's current_* fields with non-null values
    from this post. Bump last_update_at + post_count."""
    ts = posted_at.isoformat()
    # Pull current state to compute coalesced updates
    row = con.execute("""
        SELECT current_entry_text, current_entry_price, current_stop_text,
               current_stop_price, current_target_text, current_target_price,
               current_horizon
        FROM trade_threads WHERE thread_id = ?
    """, [thread_id]).fetchone()
    if not row:
        return
    cet, cep, cst, csp, ctt, ctp, chz = row
    new_cet = _coalesce(idea.entry_text if idea else None, cet)
    new_cep = _coalesce(idea.entry_price if idea else None, cep)
    new_cst = _coalesce(idea.stop_text if idea else None, cst)
    new_csp = _coalesce(idea.stop_price if idea else None, csp)
    new_ctt = _coalesce(idea.target_text if idea else None, ctt)
    new_ctp = _coalesce(idea.target_price if idea else None, ctp)
    new_chz = _coalesce(idea.horizon if idea else None, chz)

    con.execute("""
        UPDATE trade_threads
        SET last_update_at      = ?,
            current_entry_text  = ?,
            current_entry_price = ?,
            current_stop_text   = ?,
            current_stop_price  = ?,
            current_target_text = ?,
            current_target_price = ?,
            current_horizon     = ?,
            post_count          = post_count + 1
        WHERE thread_id = ?
    """, [ts, new_cet, new_cep, new_cst, new_csp, new_ctt, new_ctp, new_chz, thread_id])
    con.execute("UPDATE trade_ideas SET thread_id = ?, thread_intent = ? "
                 "WHERE tweet_id = ?",
                 [thread_id, cls.thread_intent, tweet_id])
    con.commit()


def _mark_thread_state(con, thread_id: str, state: str, at: datetime,
                        closed_reason: str | None = None) -> None:
    if state == "closed":
        con.execute("""
            UPDATE trade_threads
            SET state = ?, closed_at = ?, last_update_at = ?, closed_reason = ?
            WHERE thread_id = ?
        """, [state, at.isoformat(), at.isoformat(), closed_reason, thread_id])
    else:
        con.execute("""
            UPDATE trade_threads
            SET state = ?, last_update_at = ?, closed_reason = ?
            WHERE thread_id = ?
        """, [state, at.isoformat(), closed_reason, thread_id])
    con.commit()


def _read_thread(con, thread_id: str) -> dict:
    row = con.execute("""
        SELECT thread_id, author, ticker, direction, state,
               opened_at, last_update_at, closed_at,
               current_entry_text, current_entry_price,
               current_stop_text, current_stop_price,
               current_target_text, current_target_price,
               current_horizon, post_count,
               entry_price_actual, last_mark_price, last_mark_at,
               mfe_pct, mae_pct, current_pnl_pct, days_held,
               closed_pnl_pct, closed_reason, notes
        FROM trade_threads WHERE thread_id = ?
    """, [thread_id]).fetchone()
    if not row:
        return {}
    cols = ["thread_id","author","ticker","direction","state","opened_at",
            "last_update_at","closed_at","current_entry_text","current_entry_price",
            "current_stop_text","current_stop_price","current_target_text",
            "current_target_price","current_horizon","post_count",
            "entry_price_actual","last_mark_price","last_mark_at",
            "mfe_pct","mae_pct","current_pnl_pct","days_held",
            "closed_pnl_pct","closed_reason","notes"]
    return dict(zip(cols, row))


def list_threads(*, state: str | None = None, author: str | None = None,
                  limit: int = 100) -> list[dict]:
    ctl.ensure_schema()
    con = ctl._con()
    try:
        wheres = []
        params = []
        if state:
            wheres.append("state = ?")
            params.append(state)
        if author:
            wheres.append("author = ?")
            params.append(author)
        sql = "SELECT * FROM trade_threads"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY last_update_at DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        cols = [desc[0] for desc in con.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


# ── Alert hook (LOG channel today; Telegram wiring deferred) ───────────


@dataclass
class AlertEvent:
    thread_id:    str
    event_type:   str    # new_thread | update | stop_warning | target_hit |
                          # stop_hit | thread_closed | thread_stale
    summary:      str    # short human-readable line
    payload:      dict   # full structured detail


def maybe_alert(event: AlertEvent, *, channel: str = CTL_ALERT_CHANNEL_DEFAULT,
                 min_interval: timedelta = CTL_ALERT_MIN_INTERVAL,
                 con=None) -> dict:
    """Single funnel for all alert dispatch. Today writes to
    ~/clawd/logs/ctl-alerts.log; the same call site will route to Telegram
    once that's wired.

    De-dup rule: drop if an alert with same (thread_id, event_type) was
    sent within `min_interval`. Records the dispatch in `ctl_alert_log`.
    """
    own_con = con is None
    if own_con:
        ctl.ensure_schema()
        con = ctl._con()
    try:
        now = datetime.now(timezone.utc)
        cutoff = (now - min_interval).isoformat()
        recent = con.execute("""
            SELECT 1 FROM ctl_alert_log
            WHERE thread_id = ? AND event_type = ? AND alerted_at >= ?
            LIMIT 1
        """, [event.thread_id, event.event_type, cutoff]).fetchone()
        if recent:
            return {"dispatched": False, "reason": "throttled",
                    "thread_id": event.thread_id,
                    "event_type": event.event_type}

        alert_id = f"alert-{uuid.uuid4().hex[:12]}"
        payload_str = json.dumps(event.payload, default=str)

        # Dispatch on channel
        if channel == "log":
            CTL_ALERT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CTL_ALERT_LOG_FILE.open("a") as f:
                f.write(f"{now.isoformat()} [{event.event_type}] "
                        f"thread={event.thread_id} {event.summary}\n")
        elif channel == "telegram":
            # Spec'd, not wired today — see ctl-pipeline-v2-2026-05-06.md.
            # Concrete plan: subprocess.run(["~/clawd/scripts/tg-mirror.sh",
            #   "--target", "8463750100", "--text", event.summary])
            raise NotImplementedError("Telegram channel not wired yet — see "
                                       "ctl-pipeline-v2-2026-05-06.md §Alerts")
        else:
            raise ValueError(f"unknown alert channel: {channel!r}")

        con.execute("""
            INSERT INTO ctl_alert_log (alert_id, thread_id, event_type,
                                         alerted_at, channel, payload)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [alert_id, event.thread_id, event.event_type,
              now.isoformat(), channel, payload_str])
        con.commit()
        return {"dispatched": True, "alert_id": alert_id,
                "thread_id": event.thread_id,
                "event_type": event.event_type, "channel": channel}
    finally:
        if own_con:
            con.close()
