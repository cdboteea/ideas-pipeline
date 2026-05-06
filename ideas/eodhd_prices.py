"""Lightweight EODHD price fetcher for CTL outcome enrichment.

API key from macOS keychain (service `eodhd-api-key`). Request shapes match
the existing scripts in ~/clawd/scripts/. Coverage notes:

- `get_eod(symbol, from_date, to_date)` → daily bars over a window
- `get_quote(symbol)` → most-recent quote
- Equity tickers go in as `<TICKER>.US` (e.g. AMD.US). The CTL tickers
  come in with a leading `$` and no exchange suffix; this module
  normalizes both.
- Crypto: append `.CC`. Forex: `.FOREX`. Today we don't auto-detect those
  for CTL — the CTL-specific tickers are equities + futures, and we
  consciously skip futures (no EODHD coverage we trust).
- Errors are non-fatal — a network blip on one symbol shouldn't tank
  the whole enrich job. Returns an empty list / None rather than raising.
"""
from __future__ import annotations
import logging
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests


EODHD_BASE        = "https://eodhd.com/api"
EODHD_KEYCHAIN    = "eodhd-api-key"
EODHD_REQ_TIMEOUT = 15

_LOG = logging.getLogger(__name__)


def _api_key() -> str | None:
    """Pull the EODHD key from macOS keychain. Cached on first call."""
    cached = os.environ.get("__EODHD_KEY_CACHE")
    if cached:
        return cached
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", EODHD_KEYCHAIN, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            key = out.stdout.strip()
            os.environ["__EODHD_KEY_CACHE"] = key
            return key
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def normalize_symbol(ticker: str) -> str | None:
    """`$AMD` → `AMD.US`. Returns None for futures or empty.

    Crypto and forex are not auto-recognized today — extend if/when CTL
    starts posting them.
    """
    if not ticker:
        return None
    s = ticker.lstrip("$").upper()
    if not s:
        return None
    if s.endswith("_F") or s.endswith(".F"):
        return None  # futures — skip
    if "." in s:
        return s   # already qualified, leave as-is
    return f"{s}.US"


def get_eod(ticker: str, *, from_date: date | str | None = None,
             to_date: date | str | None = None,
             api_key: str | None = None,
             session: requests.Session | None = None) -> list[dict]:
    """End-of-day daily bars for `ticker`. Returns chronological list of
    `{date, open, high, low, close, adjusted_close, volume}` dicts.
    Empty list on any error (no key, no coverage, network blip)."""
    sym = normalize_symbol(ticker)
    if not sym:
        return []
    key = api_key or _api_key()
    if not key:
        return []
    if isinstance(from_date, date):
        from_date = from_date.isoformat()
    if isinstance(to_date, date):
        to_date = to_date.isoformat()

    params = {"api_token": key, "fmt": "json"}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    sess = session or requests
    try:
        r = sess.get(f"{EODHD_BASE}/eod/{sym}", params=params,
                     timeout=EODHD_REQ_TIMEOUT)
        if r.status_code != 200:
            _LOG.warning("eodhd eod %s → %d", sym, r.status_code)
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return data
    except (requests.RequestException, ValueError):
        return []


def get_quote(ticker: str, *, api_key: str | None = None,
               session: requests.Session | None = None) -> dict | None:
    """Most-recent quote for `ticker`. Returns
    `{code, timestamp, gmtoffset, open, high, low, close, volume,
       previousClose, change, change_p}` or None on error."""
    sym = normalize_symbol(ticker)
    if not sym:
        return None
    key = api_key or _api_key()
    if not key:
        return None
    sess = session or requests
    try:
        r = sess.get(f"{EODHD_BASE}/real-time/{sym}",
                     params={"api_token": key, "fmt": "json"},
                     timeout=EODHD_REQ_TIMEOUT)
        if r.status_code != 200:
            _LOG.warning("eodhd quote %s → %d", sym, r.status_code)
            return None
        data = r.json()
        if isinstance(data, dict) and "close" in data:
            return data
    except (requests.RequestException, ValueError):
        pass
    return None
