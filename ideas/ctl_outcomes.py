"""CTL outcome enrichment — task #123 v2.

Walks every open|partial trade thread, fetches price history from
~/clawd/data/firstrate.duckdb between `opened_at` and now, computes
MFE / MAE / current_pnl, and auto-closes threads when the price hits
the extracted stop or target.

Coverage:
  - Equities & ETFs (e.g. $AMD, $SPY, $XLK) — full coverage from firstrate
  - Futures (e.g. $SB_F, $ZS_F, $ES_F) — NOT covered; entry_price_actual
    stays NULL. Document as a v2 limitation; futures threads still get
    last_update_at + state tracking, just no MTM numerics.
  - Crypto — would need a different source (hyperliquid.duckdb); not
    implemented in v2.

Recent-data gap:
  firstrate is monthly-refreshed and runs ~5 weeks behind today. For
  trades opened in that window, last_mark_price is the most recent
  available daily close. If/when we wire EODHD intraday splice (per
  citrini-pipeline's pattern), drop it here too.

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


FIRSTRATE_DB = Path.home() / "clawd" / "data" / "firstrate.duckdb"
ENRICH_LOG_FILE = Path.home() / "clawd" / "logs" / "ctl-enrich.log"


def _strip_dollar(ticker: str | None) -> str | None:
    """`$AMD` → `AMD`. None passes through."""
    if not ticker:
        return None
    return ticker.lstrip("$").upper()


def _is_futures_ticker(ticker: str | None) -> bool:
    """Crude futures detector. firstrate has equities/ETFs only."""
    if not ticker:
        return False
    return ticker.upper().endswith("_F")


def fetch_bars(symbol: str, *, start: datetime, end: datetime,
                con=None) -> list[dict]:
    """Fetch daily bars for `symbol` between `start` and `end` (inclusive).

    Returns [{datetime, open, high, low, close}, …] in chronological order.
    Empty list on no data.
    """
    own_con = con is None
    if own_con:
        if not FIRSTRATE_DB.exists():
            return []
        con = duckdb.connect(str(FIRSTRATE_DB), read_only=True)
    try:
        rows = con.execute("""
            SELECT datetime, open, high, low, close
            FROM ohlcv
            WHERE symbol = ?
              AND timeframe = 'day'
              AND datetime BETWEEN ? AND ?
            ORDER BY datetime
        """, [symbol, start, end]).fetchall()
        return [
            {"datetime": r[0], "open": r[1], "high": r[2],
             "low": r[3], "close": r[4]}
            for r in rows
        ]
    finally:
        if own_con:
            con.close()


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
                   firstrate_con=None) -> dict:
    """Update one thread's outcome fields. Returns the updated row dict."""
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
        bars = fetch_bars(symbol, start=opened_naive, end=end, con=firstrate_con)
        if not bars:
            return thread  # no coverage → leave as-is

        # Use entry_price_actual if already known, else first bar's open
        entry_price = thread.get("entry_price_actual")
        if entry_price is None:
            # Pull first bar; entry_price_actual = open of that bar
            entry_price = bars[0]["open"]
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

        days_held = None
        if outcome.get("last_mark_at") and bars:
            try:
                days_held = (outcome["last_mark_at"] - bars[0]["datetime"]).days
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


def enrich_all_open(*, stale_days: int = ctl_threads.CTL_STALE_DAYS) -> dict:
    """Run enrich_thread over all open|partial threads. Auto-mark stale."""
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
                after = enrich_thread(thread_id, con=con, firstrate_con=firstrate_con)
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
