"""CTL outcome enrichment — task #123 v2.

Walks every open|partial trade thread, splices price history from two
sources (matching the strategy-engine convention), computes MFE / MAE /
current_pnl, and auto-closes threads when the price hits the extracted
stop or target.

Data sources:
  - **firstrate.duckdb** — historical bars (monthly refresh; the right
    source for the MFE/MAE walk over the BACK part of a thread's
    history, which is what it's designed for).
  - **EODHD** — live equity quotes + recent EOD bars for any ticker the
    keychain has access to. Used to fill in the FRONT (recent) part
    of a thread's history that firstrate hasn't ingested yet, plus the
    `last_mark_price` snapshot.
  - Futures ($SB_F, $ZS_F, $ES_F) — neither source carries them at the
    level we need. Threads still get post-lifecycle tracking; no MTM
    numerics. Skip futures cleanly.
  - Crypto — would need hyperliquid.duckdb / EODHD `.CC` suffix; not
    auto-detected in v2.

CLI: scripts/enrich_ctl_outcomes.py
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from . import ctl_extractor as ctl
from . import ctl_threads
from . import eodhd_prices


FIRSTRATE_DB = Path.home() / "clawd" / "data" / "firstrate.duckdb"
ENRICH_LOG_FILE = Path.home() / "clawd" / "logs" / "ctl-enrich.log"


def _strip_dollar(ticker: str | None) -> str | None:
    """`$AMD` → `AMD`. None passes through."""
    if not ticker:
        return None
    return ticker.lstrip("$").upper()


def _is_futures_ticker(ticker: str | None) -> bool:
    """Returns True for futures contract codes (`$SB_F`, `$ZS_F`, `$ES_F`).

    DECISION 2026-05-06 — futures MTM is PERMANENTLY skipped, not a TODO.
    Evaluated UW (`mcp__unusual-whales__get_futures_indices`) and rejected:
      - keyed by descriptive name ("US Sugar #11"), not contract code → needs
        fragile manual mapping
      - snapshot-only, no historical bars → can't walk MFE/MAE
      - no contract-month resolution → can't reconcile to the trader's post

    Lifecycle tracking on the posts themselves (entry → adds → trim → explicit
    close, with extracted Risk levels + targets + horizon as structured text)
    is enough for the user's actual goal — timely awareness of what was
    posted. Numeric MTM was nice-to-have, not required. See
    `~/clawd/research/workflows/ctl-pipeline-v2-2026-05-06.md` §Source coverage.
    """
    if not ticker:
        return False
    return ticker.upper().endswith("_F")


def fetch_bars(symbol: str, *, start: datetime, end: datetime,
                con=None,
                splice_eodhd: bool = True,
                eodhd_session=None) -> list[dict]:
    """Fetch daily bars for `symbol` between `start` and `end` (inclusive).

    Splices two sources, in this order:
      1. **firstrate.duckdb** — covers the historical span. Returns rows up
         to firstrate's ceiling (~3-5 weeks behind today, by design).
      2. **EODHD** EOD endpoint — fills in any rows AFTER the firstrate
         ceiling, up to `end`. Skipped when `splice_eodhd=False` (test
         isolation) or when no EODHD key is configured.

    Returns [{datetime, open, high, low, close}, …] chronologically.
    Empty list on no data anywhere.
    """
    bars: list[dict] = []
    own_con = con is None
    fr_ceiling = None

    if own_con:
        if FIRSTRATE_DB.exists():
            con = duckdb.connect(str(FIRSTRATE_DB), read_only=True)
        else:
            con = None
    try:
        if con is not None:
            rows = con.execute("""
                SELECT datetime, open, high, low, close
                FROM ohlcv
                WHERE symbol = ?
                  AND timeframe = 'day'
                  AND datetime BETWEEN ? AND ?
                ORDER BY datetime
            """, [symbol, start, end]).fetchall()
            bars = [
                {"datetime": r[0], "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4], "source": "firstrate"}
                for r in rows
            ]
            if bars:
                fr_ceiling = bars[-1]["datetime"]
    finally:
        if own_con and con is not None:
            con.close()

    if not splice_eodhd:
        return bars

    # Splice EODHD for any window beyond firstrate's coverage. We pull
    # from (firstrate-last-bar + 1 day) through `end`. If firstrate had
    # no rows at all, pull the whole window from EODHD.
    eodhd_start = (fr_ceiling + timedelta(days=1)).date() if fr_ceiling else (
        start.date() if isinstance(start, datetime) else start
    )
    eodhd_end = end.date() if isinstance(end, datetime) else end
    if eodhd_start <= eodhd_end:
        # Original ticker (with $) → normalized inside eodhd_prices
        recent = eodhd_prices.get_eod(
            f"${symbol}",
            from_date=eodhd_start,
            to_date=eodhd_end,
            session=eodhd_session,
        )
        for r in recent:
            try:
                dt = datetime.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            bars.append({
                "datetime": dt,
                "open":     float(r.get("open")  or 0),
                "high":     float(r.get("high")  or 0),
                "low":      float(r.get("low")   or 0),
                "close":    float(r.get("close") or 0),
                "source":   "eodhd",
            })
    # Re-sort just in case EODHD overlapped firstrate by a day
    bars.sort(key=lambda b: b["datetime"])
    # De-dup on date
    seen = set()
    deduped = []
    for b in bars:
        d = b["datetime"].date() if isinstance(b["datetime"], datetime) else b["datetime"]
        if d in seen:
            continue
        seen.add(d)
        deduped.append(b)
    return deduped


def fetch_quote(ticker: str, *, session=None) -> dict | None:
    """Most-recent live quote via EODHD. Returns
    `{datetime, close, ...}` or None on error / no key / no coverage."""
    q = eodhd_prices.get_quote(ticker, session=session)
    if not q:
        return None
    # EODHD timestamp is unix seconds
    ts = q.get("timestamp")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
    return {
        "datetime":   dt,
        "open":       q.get("open"),
        "high":       q.get("high"),
        "low":        q.get("low"),
        "close":      q.get("close"),
        "previousClose": q.get("previousClose"),
        "source":     "eodhd-live",
    }


def compute_outcome(*, direction: str | None,
                     entry_price: float,
                     bars: list[dict],
                     stop_price: float | None = None,
                     target_price: float | None = None) -> dict:
    """Walk bars from after entry; track MFE/MAE/auto-close.

    Returns:
      {
        last_mark_price, last_mark_at,
        mfe_pct, mae_pct, current_pnl_pct, days_held,
        closed_at, closed_pnl_pct, closed_reason   (the last 3 only on close)
      }

    `direction` defaults to "long" if None — most CTL signals are long.
    """
    if not bars or entry_price is None or entry_price <= 0:
        return {}
    direction = (direction or "long").lower()
    sign = 1.0 if direction == "long" else -1.0

    mfe = 0.0   # max favorable excursion (signed return)
    mae = 0.0   # max adverse (signed return)
    last_close = None
    last_dt = None
    closed_dt = None
    closed_pnl = None
    closed_reason = None

    for bar in bars:
        last_close = bar["close"]
        last_dt    = bar["datetime"]
        # For long: high is best, low is worst. Reverse for short.
        best_price  = bar["high"] if sign > 0 else bar["low"]
        worst_price = bar["low"]  if sign > 0 else bar["high"]
        ret_best  = sign * (best_price  - entry_price) / entry_price
        ret_worst = sign * (worst_price - entry_price) / entry_price
        mfe = max(mfe, ret_best)
        mae = min(mae, ret_worst)

        # Auto-close on stop hit
        if stop_price is not None:
            if sign > 0 and bar["low"] <= stop_price:
                closed_dt    = bar["datetime"]
                closed_pnl   = (stop_price - entry_price) / entry_price
                closed_reason = "stop_hit"
                break
            if sign < 0 and bar["high"] >= stop_price:
                closed_dt    = bar["datetime"]
                closed_pnl   = (entry_price - stop_price) / entry_price
                closed_reason = "stop_hit"
                break
        # Auto-close on target hit (after stop — stops always win)
        if target_price is not None:
            if sign > 0 and bar["high"] >= target_price:
                closed_dt    = bar["datetime"]
                closed_pnl   = (target_price - entry_price) / entry_price
                closed_reason = "target_hit"
                break
            if sign < 0 and bar["low"] <= target_price:
                closed_dt    = bar["datetime"]
                closed_pnl   = (entry_price - target_price) / entry_price
                closed_reason = "target_hit"
                break

    out = {
        "last_mark_price":  last_close,
        "last_mark_at":     last_dt,
        "mfe_pct":          round(100 * mfe, 4),
        "mae_pct":          round(100 * mae, 4),
        "current_pnl_pct":  round(100 * sign * ((last_close - entry_price) / entry_price), 4) if last_close else None,
    }
    if closed_dt is not None:
        out["closed_at"]      = closed_dt
        out["closed_pnl_pct"] = round(100 * closed_pnl, 4)
        out["closed_reason"]  = closed_reason
    return out


def enrich_thread(thread_id: str, *, con=None,
                   firstrate_con=None,
                   splice_eodhd: bool = True,
                   eodhd_session=None,
                   use_live_quote: bool = True) -> dict:
    """Update one thread's outcome fields. Returns the updated row dict.

    `splice_eodhd` controls whether to call out to EODHD for the recent
    portion of the bar history (after firstrate's ceiling). `use_live_quote`
    further uses EODHD's real-time endpoint to overwrite the last_mark_price
    with the most-current close (vs the most recent bar's close, which is
    yesterday's at best).

    Both default True; tests inject a stub session to avoid network calls.
    """
    own_con = con is None
    if own_con:
        ctl.ensure_schema()
        con = ctl._con()
    try:
        thread = ctl_threads._read_thread(con, thread_id)
        if not thread:
            return {}
        if thread["state"] not in ("open", "partial"):
            return thread

        ticker = thread["ticker"]
        symbol = _strip_dollar(ticker)
        if _is_futures_ticker(ticker) or not symbol:
            return thread  # Skip futures + bad tickers

        opened_at = thread["opened_at"]
        if isinstance(opened_at, str):
            try:
                opened_at = datetime.fromisoformat(
                    opened_at.replace("Z", "+00:00") if opened_at.endswith("Z") else opened_at
                )
            except ValueError:
                return thread
        # firstrate stores naive datetimes; align by stripping tz
        opened_naive = opened_at.replace(tzinfo=None) if opened_at.tzinfo else opened_at
        end = datetime.now()
        bars = fetch_bars(symbol, start=opened_naive, end=end,
                           con=firstrate_con,
                           splice_eodhd=splice_eodhd,
                           eodhd_session=eodhd_session)
        if not bars:
            return thread  # no coverage → leave as-is

        # Entry price priority:
        #   1. entry_price_actual (already set by a previous enrich pass)
        #   2. current_entry_price (LLM extracted from post text — most
        #      faithful to "what the author called as the entry")
        #   3. bars[0].open (firstrate/EODHD open of the post-bar — a
        #      reasonable proxy when the post said "L here")
        entry_price = (
            thread.get("entry_price_actual")
            or thread.get("current_entry_price")
            or bars[0]["open"]
        )
        # current_stop_price / current_target_price drive auto-close
        stop_price   = thread.get("current_stop_price")
        target_price = thread.get("current_target_price")
        direction    = thread.get("direction") or "long"

        outcome = compute_outcome(
            direction=direction, entry_price=entry_price,
            bars=bars[1:],   # skip the entry bar itself for MFE/MAE walk
            stop_price=stop_price, target_price=target_price,
        )
        if not outcome:
            return thread

        # Optionally overwrite last_mark with a live quote (more current
        # than the most recent EOD bar)
        if use_live_quote and "closed_at" not in outcome:
            live = fetch_quote(ticker, session=eodhd_session)
            if live and live.get("close"):
                sign = 1.0 if direction == "long" else -1.0
                outcome["last_mark_price"] = live["close"]
                outcome["last_mark_at"]    = live["datetime"] or outcome.get("last_mark_at")
                outcome["current_pnl_pct"] = round(
                    100 * sign * (live["close"] - entry_price) / entry_price, 4
                )

        days_held = None
        if outcome.get("last_mark_at") and bars:
            try:
                lm = outcome["last_mark_at"]
                first = bars[0]["datetime"]
                # Strip tz to compare apples-to-apples
                if hasattr(lm, "tzinfo") and lm.tzinfo:
                    lm = lm.replace(tzinfo=None)
                if hasattr(first, "tzinfo") and first.tzinfo:
                    first = first.replace(tzinfo=None)
                days_held = (lm - first).days
            except (TypeError, AttributeError):
                pass

        # Persist
        if "closed_at" in outcome:
            con.execute("""
                UPDATE trade_threads
                SET entry_price_actual = ?,
                    last_mark_price    = ?,
                    last_mark_at       = ?,
                    mfe_pct            = ?,
                    mae_pct            = ?,
                    current_pnl_pct    = ?,
                    days_held          = ?,
                    state              = 'closed',
                    closed_at          = ?,
                    closed_pnl_pct     = ?,
                    closed_reason      = ?
                WHERE thread_id = ?
            """, [entry_price, outcome["last_mark_price"], outcome["last_mark_at"],
                  outcome["mfe_pct"], outcome["mae_pct"],
                  outcome["current_pnl_pct"], days_held,
                  outcome["closed_at"], outcome["closed_pnl_pct"],
                  outcome["closed_reason"], thread_id])
        else:
            con.execute("""
                UPDATE trade_threads
                SET entry_price_actual = ?,
                    last_mark_price    = ?,
                    last_mark_at       = ?,
                    mfe_pct            = ?,
                    mae_pct            = ?,
                    current_pnl_pct    = ?,
                    days_held          = ?
                WHERE thread_id = ?
            """, [entry_price, outcome["last_mark_price"], outcome["last_mark_at"],
                  outcome["mfe_pct"], outcome["mae_pct"],
                  outcome["current_pnl_pct"], days_held, thread_id])
        con.commit()
        return ctl_threads._read_thread(con, thread_id)
    finally:
        if own_con:
            con.close()


def enrich_all_open(*, stale_days: int = ctl_threads.CTL_STALE_DAYS,
                     splice_eodhd: bool = True,
                     use_live_quote: bool = True,
                     eodhd_session=None) -> dict:
    """Run enrich_thread over all open|partial threads. Auto-mark stale.

    `splice_eodhd=False` and `use_live_quote=False` together turn this
    into a firstrate-only pass (no network), useful for offline runs."""
    ctl.ensure_schema()
    con = ctl._con()
    firstrate_con = duckdb.connect(str(FIRSTRATE_DB), read_only=True) \
        if FIRSTRATE_DB.exists() else None
    summary = {
        "total_open":     0,
        "enriched":       0,
        "auto_closed":    0,
        "marked_stale":   0,
        "skipped_futures": 0,
        "no_data":        0,
        "errors":         [],
    }
    try:
        rows = con.execute("""
            SELECT thread_id, ticker, last_update_at
            FROM trade_threads
            WHERE state IN ('open', 'partial')
        """).fetchall()
        summary["total_open"] = len(rows)
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()

        for thread_id, ticker, last_update_at in rows:
            try:
                if _is_futures_ticker(ticker):
                    summary["skipped_futures"] += 1
                    continue
                before = ctl_threads._read_thread(con, thread_id)
                after = enrich_thread(thread_id, con=con,
                                       firstrate_con=firstrate_con,
                                       splice_eodhd=splice_eodhd,
                                       use_live_quote=use_live_quote,
                                       eodhd_session=eodhd_session)
                if not after.get("last_mark_price"):
                    summary["no_data"] += 1
                else:
                    summary["enriched"] += 1
                    if after.get("state") == "closed" and before.get("state") != "closed":
                        summary["auto_closed"] += 1
                # Stale check
                if after.get("state") in ("open", "partial"):
                    if str(after.get("last_update_at") or "") < stale_cutoff:
                        ctl_threads._mark_thread_state(
                            con, thread_id, "stale",
                            datetime.now(timezone.utc),
                            closed_reason="stale_no_update",
                        )
                        summary["marked_stale"] += 1
            except Exception as e:  # noqa: BLE001
                summary["errors"].append({
                    "thread_id": thread_id,
                    "error":     f"{type(e).__name__}: {e}",
                })
    finally:
        if firstrate_con is not None:
            firstrate_con.close()
        con.close()

    # Append summary line to log
    try:
        ENRICH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ENRICH_LOG_FILE.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} "
                    f"open={summary['total_open']} "
                    f"enriched={summary['enriched']} "
                    f"auto_closed={summary['auto_closed']} "
                    f"stale={summary['marked_stale']} "
                    f"futures_skipped={summary['skipped_futures']} "
                    f"errors={len(summary['errors'])}\n")
    except OSError:
        pass

    return summary
