"""Behavioral tests for #123 v2 — outcome enrichment + analytics + ctl-status CLI.

We never read real firstrate; we build small synthetic bar fixtures and
invoke compute_outcome / fetch_bars (with an in-memory DuckDB) to test
all the auto-close paths.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from ideas import ctl_extractor as ctl
from ideas import ctl_threads
from ideas import ctl_outcomes
from ideas.ctl_extractor import Classification, ExtractedIdea, upsert_idea
from ideas.cli import cli


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "ctl.duckdb"
    state = tmp_path / "state.json"
    log = tmp_path / "ctl.log"
    alerts = tmp_path / "ctl-alerts.log"
    enrich_log = tmp_path / "ctl-enrich.log"
    last_check = tmp_path / "last-check.json"
    fr = tmp_path / "firstrate.duckdb"
    monkeypatch.setattr(ctl, "CTL_DUCKDB_PATH", db)
    monkeypatch.setattr(ctl, "CTL_STATE_FILE", state)
    monkeypatch.setattr(ctl, "CTL_LOG_FILE", log)
    monkeypatch.setattr(ctl_threads, "CTL_ALERT_LOG_FILE", alerts)
    monkeypatch.setattr(ctl_threads, "CTL_LAST_CHECK_FILE", last_check)
    monkeypatch.setattr(ctl_outcomes, "ENRICH_LOG_FILE", enrich_log)
    monkeypatch.setattr(ctl_outcomes, "FIRSTRATE_DB", fr)
    return {"db": db, "state": state, "log": log, "alerts": alerts,
            "enrich_log": enrich_log, "last_check": last_check, "firstrate": fr}


def _make_firstrate(path: Path, symbol: str, bars: list[tuple]) -> None:
    """`bars` = [(datetime, open, high, low, close), …]"""
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT, datetime TIMESTAMP, timeframe TEXT,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume BIGINT
        )
    """)
    for dt, o, h, l, c in bars:
        con.execute("""INSERT INTO ohlcv (symbol, datetime, timeframe, open, high, low, close, volume)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     [symbol, dt, "day", o, h, l, c, 0])
    con.commit()
    con.close()


def _seed_thread(*, db_path: Path, ticker: str, author: str = "canuck2usa",
                  direction: str = "long",
                  stop_price: float | None = None,
                  target_price: float | None = None,
                  entry_price_actual: float | None = None,
                  opened_days_ago: int = 5) -> str:
    """Insert one thread row directly so we can test enrich_thread cleanly."""
    ctl.ensure_schema()
    con = duckdb.connect(str(db_path))
    tid = f"thr-{ticker.lstrip('$')}-{author}"
    opened_at = (datetime.now(timezone.utc) - timedelta(days=opened_days_ago)).isoformat()
    con.execute("""
        INSERT INTO trade_threads (
            thread_id, author, ticker, direction, state,
            opened_at, last_update_at,
            current_stop_price, current_target_price,
            entry_price_actual, post_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,1)
    """, [tid, author, ticker, direction, "open",
          opened_at, opened_at, stop_price, target_price, entry_price_actual])
    con.commit()
    con.close()
    return tid


# ── compute_outcome ────────────────────────────────────────────────────


def test_compute_outcome_long_open_position():
    bars = [
        {"datetime": datetime(2026, 5, 2), "open": 100, "high": 105, "low": 98,  "close": 103},
        {"datetime": datetime(2026, 5, 3), "open": 103, "high": 110, "low": 102, "close": 108},
    ]
    out = ctl_outcomes.compute_outcome(direction="long", entry_price=100.0,
                                        bars=bars)
    assert out["last_mark_price"] == 108
    assert out["mfe_pct"] == 10.0    # high of 110 → 10% MFE
    assert out["mae_pct"] == -2.0    # low of 98 → -2% MAE
    assert out["current_pnl_pct"] == 8.0
    assert "closed_at" not in out


def test_compute_outcome_long_target_hit():
    bars = [
        {"datetime": datetime(2026, 5, 2), "open": 100, "high": 102, "low": 99,  "close": 101},
        {"datetime": datetime(2026, 5, 3), "open": 101, "high": 112, "low": 100, "close": 110},
        {"datetime": datetime(2026, 5, 4), "open": 110, "high": 115, "low": 108, "close": 113},
    ]
    out = ctl_outcomes.compute_outcome(direction="long", entry_price=100.0,
                                        bars=bars, target_price=110.0)
    assert out["closed_reason"] == "target_hit"
    assert out["closed_pnl_pct"] == 10.0    # exactly target / entry - 1


def test_compute_outcome_long_stop_hit_takes_priority_over_target():
    bars = [
        # Stop hits intra-day on bar 1 (low 94 < stop 95)
        {"datetime": datetime(2026, 5, 2), "open": 100, "high": 105, "low": 94,  "close": 96},
    ]
    out = ctl_outcomes.compute_outcome(direction="long", entry_price=100.0,
                                        bars=bars, stop_price=95.0,
                                        target_price=110.0)
    assert out["closed_reason"] == "stop_hit"
    assert out["closed_pnl_pct"] == -5.0


def test_compute_outcome_short_position():
    bars = [
        # Short from 100. Price drops to 90.
        {"datetime": datetime(2026, 5, 2), "open": 100, "high": 102, "low": 88, "close": 90},
    ]
    out = ctl_outcomes.compute_outcome(direction="short", entry_price=100.0,
                                        bars=bars)
    # MFE for short: the LOW gives the best return
    assert out["mfe_pct"] == 12.0
    assert out["mae_pct"] == -2.0
    assert out["current_pnl_pct"] == 10.0


def test_compute_outcome_short_target_hit():
    bars = [
        {"datetime": datetime(2026, 5, 2), "open": 100, "high": 101, "low": 88, "close": 90},
    ]
    out = ctl_outcomes.compute_outcome(direction="short", entry_price=100.0,
                                        bars=bars, target_price=90.0)
    assert out["closed_reason"] == "target_hit"
    assert out["closed_pnl_pct"] == 10.0


def test_compute_outcome_handles_empty_bars():
    out = ctl_outcomes.compute_outcome(direction="long", entry_price=100.0, bars=[])
    assert out == {}


def test_compute_outcome_skips_zero_entry_price():
    bars = [{"datetime": datetime.now(), "open": 1, "high": 1, "low": 1, "close": 1}]
    assert ctl_outcomes.compute_outcome(direction="long", entry_price=0,
                                         bars=bars) == {}


# ── fetch_bars + enrich_thread ─────────────────────────────────────────


def test_fetch_bars_returns_empty_when_db_missing(isolated_db, tmp_path):
    # firstrate.duckdb does not exist
    out = ctl_outcomes.fetch_bars("AMD", start=datetime(2026,5,1),
                                    end=datetime(2026,5,5),
                                    splice_eodhd=False)
    assert out == []


def test_fetch_bars_returns_chronological_rows(isolated_db):
    bars = [
        (datetime(2026, 5, 1), 100.0, 102.0, 99.0, 101.0),
        (datetime(2026, 5, 2), 101.0, 103.0, 100.0, 102.5),
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)
    out = ctl_outcomes.fetch_bars("AMD", start=datetime(2026,4,30),
                                    end=datetime(2026,5,5),
                                    splice_eodhd=False)
    assert len(out) == 2
    assert out[0]["close"] == 101.0
    assert all(b["source"] == "firstrate" for b in out)


# ── EODHD splice ────────────────────────────────────────────────────────


class _FakeEodhdSession:
    """A requests-like Session stub that returns canned EODHD payloads."""

    def __init__(self, eod_rows: list[dict] | None = None,
                  quote: dict | None = None):
        self.eod_rows = eod_rows or []
        self.quote = quote
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        return _FakeResp(
            self.eod_rows if "/eod/" in url else (self.quote or {})
        )


class _FakeResp:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
    def json(self):
        return self._data


def test_fetch_bars_splices_eodhd_after_firstrate_ceiling(isolated_db, monkeypatch):
    """firstrate covers up to a ceiling; EODHD fills in newer days."""
    fr_bars = [
        (datetime(2026, 4, 1), 100.0, 102.0, 99.0, 101.0),
        (datetime(2026, 4, 2), 101.0, 103.0, 100.0, 102.0),
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", fr_bars)

    eodhd_rows = [
        {"date": "2026-04-03", "open": 102.0, "high": 105.0, "low": 101.5, "close": 104.0},
        {"date": "2026-04-04", "open": 104.0, "high": 106.0, "low": 103.0, "close": 105.5},
    ]
    fake = _FakeEodhdSession(eod_rows=eodhd_rows)
    # Pretend a key is configured (the wrapper short-circuits without one)
    monkeypatch.setenv("__EODHD_KEY_CACHE", "test-key")

    bars = ctl_outcomes.fetch_bars("AMD",
                                    start=datetime(2026, 3, 31),
                                    end=datetime(2026, 4, 5),
                                    splice_eodhd=True,
                                    eodhd_session=fake)
    assert len(bars) == 4
    assert [b["source"] for b in bars] == ["firstrate", "firstrate", "eodhd", "eodhd"]
    assert bars[-1]["close"] == 105.5
    assert "/eod/AMD.US" in fake.calls[0][0]


def test_fetch_bars_splice_disabled_skips_eodhd(isolated_db):
    fr_bars = [(datetime(2026, 4, 1), 100.0, 102.0, 99.0, 101.0)]
    _make_firstrate(isolated_db["firstrate"], "AMD", fr_bars)
    bars = ctl_outcomes.fetch_bars("AMD",
                                    start=datetime(2026, 3, 31),
                                    end=datetime(2026, 4, 5),
                                    splice_eodhd=False)
    assert len(bars) == 1
    assert bars[0]["source"] == "firstrate"


def test_fetch_bars_dedups_overlap_between_firstrate_and_eodhd(isolated_db, monkeypatch):
    """If firstrate and EODHD both have the same date, firstrate wins
    (it's the earlier source in the sort+dedup)."""
    fr_bars = [
        (datetime(2026, 4, 1), 100.0, 102.0, 99.0, 101.0),
        (datetime(2026, 4, 2), 101.0, 103.0, 100.0, 102.0),
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", fr_bars)
    # EODHD overlaps 4/2 with a different close — firstrate's must win
    fake = _FakeEodhdSession(eod_rows=[
        {"date": "2026-04-02", "open": 88.0, "high": 88.0, "low": 88.0, "close": 88.0},
        {"date": "2026-04-03", "open": 102.0, "high": 105.0, "low": 101.5, "close": 104.0},
    ])
    monkeypatch.setenv("__EODHD_KEY_CACHE", "test-key")
    bars = ctl_outcomes.fetch_bars("AMD",
                                    start=datetime(2026, 3, 31),
                                    end=datetime(2026, 4, 5),
                                    eodhd_session=fake)
    by_date = {b["datetime"].date(): b for b in bars}
    assert by_date[datetime(2026,4,2).date()]["close"] == 102.0   # firstrate kept
    assert by_date[datetime(2026,4,3).date()]["close"] == 104.0   # eodhd added


def test_fetch_quote_returns_normalized_dict(monkeypatch):
    fake = _FakeEodhdSession(quote={
        "code": "AMD.US", "timestamp": 1746547200,
        "open": 200.0, "high": 205.0, "low": 199.0, "close": 204.5,
        "previousClose": 200.0,
    })
    monkeypatch.setenv("__EODHD_KEY_CACHE", "test-key")
    out = ctl_outcomes.fetch_quote("$AMD", session=fake)
    assert out is not None
    assert out["close"] == 204.5
    assert out["source"] == "eodhd-live"


def test_fetch_quote_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("__EODHD_KEY_CACHE", raising=False)
    # Block subprocess from reading keychain too
    monkeypatch.setattr("subprocess.run",
                         lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    out = ctl_outcomes.fetch_quote("$AMD")
    assert out is None


def test_enrich_thread_skips_futures(isolated_db):
    tid = _seed_thread(db_path=isolated_db["db"], ticker="$ZS_F",
                        author="CTLFutures")
    after = ctl_outcomes.enrich_thread(tid, splice_eodhd=False, use_live_quote=False)
    # No update happened — still no last_mark_price
    assert after["last_mark_price"] is None


def test_enrich_thread_auto_closes_on_stop_hit(isolated_db):
    # AMD opened 5 days ago at 100, stop at 95. Bars show low of 94.
    tid = _seed_thread(db_path=isolated_db["db"], ticker="$AMD",
                        stop_price=95.0, entry_price_actual=100.0,
                        opened_days_ago=5)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        (today - timedelta(days=5), 100.0, 102.0, 99.0, 101.0),  # entry bar
        (today - timedelta(days=4), 101.0, 103.0, 94.0, 96.0),   # stop hit
        (today - timedelta(days=3), 96.0, 99.0, 95.5, 98.0),     # after-stop, ignored
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)

    after = ctl_outcomes.enrich_thread(tid, splice_eodhd=False, use_live_quote=False)
    assert after["state"] == "closed"
    assert after["closed_reason"] == "stop_hit"
    assert after["closed_pnl_pct"] == -5.0
    assert after["mae_pct"] == -6.0   # bar low 94 → -6%


def test_enrich_thread_auto_closes_on_target_hit(isolated_db):
    tid = _seed_thread(db_path=isolated_db["db"], ticker="$AMD",
                        target_price=110.0, entry_price_actual=100.0,
                        opened_days_ago=5)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        (today - timedelta(days=5), 100.0, 101.0, 99.0, 100.5),
        (today - timedelta(days=3), 100.5, 110.5, 100.0, 109.0),  # target hit (high 110.5 ≥ 110)
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)
    after = ctl_outcomes.enrich_thread(tid, splice_eodhd=False, use_live_quote=False)
    assert after["closed_reason"] == "target_hit"
    assert after["closed_pnl_pct"] == 10.0


def test_enrich_thread_keeps_open_when_no_stop_or_target_hit(isolated_db):
    tid = _seed_thread(db_path=isolated_db["db"], ticker="$AMD",
                        stop_price=80.0, target_price=120.0,
                        entry_price_actual=100.0, opened_days_ago=5)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        (today - timedelta(days=5), 100.0, 102.0, 99.0, 101.0),
        (today - timedelta(days=3), 101.0, 105.0, 99.0, 103.0),
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)
    after = ctl_outcomes.enrich_thread(tid, splice_eodhd=False, use_live_quote=False)
    assert after["state"] == "open"
    assert after["last_mark_price"] == 103.0


def test_enrich_all_open_summarizes_counts(isolated_db):
    tid_eq = _seed_thread(db_path=isolated_db["db"], ticker="$AMD",
                           entry_price_actual=100.0, opened_days_ago=2)
    tid_fu = _seed_thread(db_path=isolated_db["db"], ticker="$ZS_F",
                           author="CTLFutures",
                           entry_price_actual=100.0, opened_days_ago=2)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        (today - timedelta(days=2), 100.0, 102.0, 99.0, 101.0),
        (today - timedelta(days=1), 101.0, 103.0, 100.0, 102.5),
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)

    summary = ctl_outcomes.enrich_all_open(splice_eodhd=False, use_live_quote=False)
    assert summary["total_open"] == 2
    assert summary["enriched"] == 1
    assert summary["skipped_futures"] == 1
    assert summary["auto_closed"] == 0


def test_enrich_thread_uses_live_quote_for_last_mark(isolated_db, monkeypatch):
    """When splice_eodhd + use_live_quote are on, last_mark_price comes
    from the live quote (current intraday) not the most recent bar close."""
    tid = _seed_thread(db_path=isolated_db["db"], ticker="$AMD",
                        entry_price_actual=100.0, opened_days_ago=2)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        (today - timedelta(days=2), 100.0, 101.0, 99.0, 100.0),  # entry
        (today - timedelta(days=1), 100.0, 102.0, 100.0, 101.5), # yesterday's close
    ]
    _make_firstrate(isolated_db["firstrate"], "AMD", bars)
    # The live quote says current price is 110 — significantly higher
    fake = _FakeEodhdSession(
        eod_rows=[],
        quote={"timestamp": int(today.timestamp()), "close": 110.0},
    )
    monkeypatch.setenv("__EODHD_KEY_CACHE", "test-key")
    after = ctl_outcomes.enrich_thread(tid, splice_eodhd=True,
                                        use_live_quote=True,
                                        eodhd_session=fake)
    # last_mark_price came from the live quote, not the 101.5 bar close
    assert after["last_mark_price"] == 110.0
    assert after["current_pnl_pct"] == 10.0  # from entry 100 → 110


# ── CLI: ctl-status, ctl-summary, ctl-enrich ───────────────────────────


def test_cli_ctl_status_handles_empty_db(isolated_db):
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-status", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["totals"]["all"] == 0


def test_cli_ctl_status_marks_check_at(isolated_db):
    _seed_thread(db_path=isolated_db["db"], ticker="$AMD")
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-status", "--since-last-check", "--json"])
    assert res.exit_code == 0
    assert isolated_db["last_check"].exists()


def test_cli_ctl_status_no_mark_skips_state_write(isolated_db):
    _seed_thread(db_path=isolated_db["db"], ticker="$AMD")
    runner = CliRunner()
    runner.invoke(cli, ["ctl-status", "--since-last-check", "--no-mark", "--json"])
    assert not isolated_db["last_check"].exists()


def test_cli_ctl_status_filters_by_author(isolated_db):
    _seed_thread(db_path=isolated_db["db"], ticker="$AMD", author="canuck2usa")
    _seed_thread(db_path=isolated_db["db"], ticker="$ES_F", author="CTLFutures")
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-status", "--author", "canuck2usa", "--json"])
    out = json.loads(res.output)
    assert out["totals"]["all"] == 1


def test_cli_ctl_summary_runs_on_empty_db(isolated_db):
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-summary", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["by_author"] == []
    assert out["by_ticker"] == []


def test_cli_ctl_summary_reports_open_threads(isolated_db):
    _seed_thread(db_path=isolated_db["db"], ticker="$AMD", author="canuck2usa")
    _seed_thread(db_path=isolated_db["db"], ticker="$NVDA", author="canuck2usa")
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-summary", "--json"])
    out = json.loads(res.output)
    assert len(out["by_author"]) == 1
    assert out["by_author"][0]["author"] == "canuck2usa"
    assert out["by_author"][0]["threads"] == 2


def test_cli_ctl_enrich_runs_on_empty_db(isolated_db):
    runner = CliRunner()
    res = runner.invoke(cli, ["ctl-enrich", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["total_open"] == 0
