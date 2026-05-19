"""CTL trade-idea extractor — task #123.

Companion to ideas/x_personal.py (which captures the raw posts). This module
takes a list of normalized post dicts (same shape as x_personal.normalize_capture
returns), runs each through an LLM-backed extractor that pulls structured
trade-idea fields (ticker, direction, entry, stop, target, horizon,
confidence), stores valid extractions in a DuckDB table, and writes a
per-run log line to ~/clawd/logs/ctl-trade-ideas.log.

The CTL list (4 members @ x.com/i/lists/936040010809307136):
  • @CTLFutures   — highly-structured futures format, ~regex-friendly:
                     "$SB_F L here Risk 14.94 H4 #Swing"
                     "$ZS_F L here Risk 1164 Hard"
                     "$SB_F Re enter IMO new Risk 12.94 Hard"
                     "$SB_F nice move so far"   ← position update, not a fresh idea
  • @canuck2usa   — mixed: ~30% ticker calls, ~70% commentary. Extractor
                     must classify "is this an actionable trade idea?" first.
                     "$SNDK my max target area is 1400-ish"   ← target update
                     "$AMD 408"                              ← entry signal
  • @DK1Platinum, @KarlvKtrading — sample format unknown until first batch.

Extraction strategy:
  1. classify(post.text) → {"is_trade_idea": bool, "confidence": 0..1, "reason": ...}
  2. if is_trade_idea: extract(post.text) → {ticker, direction, entry, stop,
                                             target, horizon, tags}
  3. validate: ticker matches ^\\$?[A-Z]{1,8}(_F)?$, direction in {long, short},
               at least one of {entry, stop, target} present
  4. persist: INSERT into ctl-trade-ideas.duckdb::trade_ideas

Output: ~/clawd/logs/ctl-trade-ideas.log gets a one-line summary per fire.
Telegram delivery deferred per design doc (`feeds-and-monitoring-2026-05-05.md` §3).

LLM provider: codex CLI (gpt-5.5 via OpenClaw subscription — see CLAUDE.md
LESSONS for why not the OpenAI API). Subprocess pattern matches
~/projects/knowledge-os/kos/llm/codex.py.
"""
from __future__ import annotations
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

# Shared Gemini client (post-codex migration, 2026-05-18). Lazy import so the
# legacy codex path can still be exercised by tests that inject `llm_call`.
import sys as _sys
_sys.path.insert(0, os.path.expanduser("~/clawd/scripts"))
try:
    from lib import gemini_client  # type: ignore[import-not-found]
except ImportError:
    gemini_client = None


# ── Paths ───────────────────────────────────────────────────────────────────

CTL_DUCKDB_PATH       = Path.home() / "clawd" / "data" / "ctl-trade-ideas.duckdb"
CTL_STATE_FILE        = Path.home() / "clawd" / "data" / "ctl-state.json"
CTL_LOG_FILE          = Path.home() / "clawd" / "logs" / "ctl-trade-ideas.log"
CTL_LIST_URL          = "https://x.com/i/lists/936040010809307136"
CTL_LIST_ID           = "936040010809307136"
CTL_MEMBERS = ["CTLFutures", "DK1Platinum", "canuck2usa", "KarlvKtrading"]

CODEX_BIN = "codex"


# ── DuckDB schema ───────────────────────────────────────────────────────────

CTL_SCHEMA_SQL = """
-- v3 schema (2026-05-18): PK is `idea_id` (synthetic `<tweet_id>:<ticker>`)
-- so a single tweet can produce N rows when it references multiple tickers
-- (e.g. "$INTC $AMD $GOOGL took some profits on these"). `tweet_id` is
-- retained as an indexed regular column. Legacy v2 DBs with tweet_id PK
-- are migrated in-place by `_migrate_v2_to_v3`.
CREATE TABLE IF NOT EXISTS trade_ideas (
    -- identity
    idea_id            TEXT PRIMARY KEY,  -- `<tweet_id>:<TICKER>` or `<tweet_id>:_` for commentary
    tweet_id           TEXT NOT NULL,     -- original post id (now NON-unique)
    posted_at          TIMESTAMP,
    author_handle      TEXT,              -- e.g. "CTLFutures"
    raw_text           TEXT,

    -- classifier output
    is_trade_idea      BOOLEAN,
    classify_confidence DOUBLE,           -- 0.0–1.0
    classify_reason    TEXT,

    -- extraction
    ticker             TEXT,              -- "$SB_F", "$AMD" (preserves $ prefix). Single value per row.
    direction          TEXT,              -- "long" / "short" / NULL
    entry_text         TEXT,              -- raw entry phrase ("here", "@408", "1235-40 area")
    entry_price        DOUBLE,            -- parsed numeric if extractable
    stop_text          TEXT,              -- raw stop phrase ("Risk 14.94", "Hard 1164")
    stop_price         DOUBLE,            -- parsed numeric
    target_text        TEXT,
    target_price       DOUBLE,
    horizon            TEXT,              -- "H4", "Swing", "intraday", "long-term"
    tags               TEXT,              -- JSON array of #tags
    extract_confidence DOUBLE,

    -- thread linkage (v2)
    thread_id          TEXT,
    thread_intent      TEXT,              -- new | update | close | unsure

    -- ops
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extractor_version  TEXT,              -- "v1"
    llm_model          TEXT               -- e.g. "gemini-2.5-flash"
);

CREATE INDEX IF NOT EXISTS idx_trade_ideas_tweet     ON trade_ideas(tweet_id);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_posted_at ON trade_ideas(posted_at);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_author    ON trade_ideas(author_handle);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_ticker    ON trade_ideas(ticker);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_thread    ON trade_ideas(thread_id);

-- v2: thread linkage (added by ALTER if upgrading from v1)
CREATE TABLE IF NOT EXISTS trade_threads (
    thread_id            TEXT PRIMARY KEY,
    author               TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    direction            TEXT,                 -- long | short | unknown
    state                TEXT NOT NULL,        -- open | partial | closed | stale | unknown
    opened_at            TIMESTAMP NOT NULL,
    last_update_at       TIMESTAMP NOT NULL,
    closed_at            TIMESTAMP,

    -- Rolling current-known state (last non-null wins as posts roll in)
    current_entry_text   TEXT,
    current_entry_price  DOUBLE,
    current_stop_text    TEXT,
    current_stop_price   DOUBLE,
    current_target_text  TEXT,
    current_target_price DOUBLE,
    current_horizon      TEXT,
    post_count           INTEGER DEFAULT 0,

    -- Outcome (populated by enrich_outcomes)
    entry_price_actual   DOUBLE,
    last_mark_price      DOUBLE,
    last_mark_at         TIMESTAMP,
    mfe_pct              DOUBLE,
    mae_pct              DOUBLE,
    current_pnl_pct      DOUBLE,
    days_held            DOUBLE,
    closed_pnl_pct       DOUBLE,
    closed_reason        TEXT,                 -- stop_hit | target_hit | explicit_close | stale | unknown

    notes                TEXT
);

CREATE INDEX IF NOT EXISTS idx_threads_author     ON trade_threads(author);
CREATE INDEX IF NOT EXISTS idx_threads_ticker     ON trade_threads(ticker);
CREATE INDEX IF NOT EXISTS idx_threads_state      ON trade_threads(state);
CREATE INDEX IF NOT EXISTS idx_threads_open_lookup ON trade_threads(author, ticker, state);

-- v2: alert de-dup log
CREATE TABLE IF NOT EXISTS ctl_alert_log (
    alert_id   TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    event_type TEXT NOT NULL,                  -- new_thread | update | stop_warning | target_hit | stop_hit | thread_closed | thread_stale
    alerted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    channel    TEXT,                           -- telegram | log
    payload    TEXT                            -- JSON of what was sent
);

CREATE INDEX IF NOT EXISTS idx_alert_log_thread ON ctl_alert_log(thread_id);
CREATE INDEX IF NOT EXISTS idx_alert_log_event  ON ctl_alert_log(event_type, alerted_at);

-- v2: analytics views (idempotent)
CREATE OR REPLACE VIEW ctl_author_hit_rate AS
SELECT
    author,
    COUNT(*)                                                   AS thread_count,
    SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END)          AS closed_threads,
    SUM(CASE WHEN state IN ('open','partial') THEN 1 ELSE 0 END) AS open_threads,
    SUM(CASE WHEN state = 'closed' AND closed_pnl_pct > 0 THEN 1 ELSE 0 END) AS winners,
    SUM(CASE WHEN state = 'closed' AND closed_pnl_pct <= 0 THEN 1 ELSE 0 END) AS losers,
    ROUND(AVG(CASE WHEN state = 'closed' THEN closed_pnl_pct END), 3) AS avg_closed_pnl_pct,
    ROUND(MEDIAN(CASE WHEN state = 'closed' THEN closed_pnl_pct END), 3) AS median_closed_pnl_pct,
    ROUND(AVG(CASE WHEN state = 'closed' THEN days_held END), 2)        AS avg_days_held,
    ROUND(100.0 * SUM(CASE WHEN state = 'closed' AND closed_pnl_pct > 0 THEN 1 ELSE 0 END)
                  / NULLIF(SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END), 0), 1) AS win_pct
FROM trade_threads
GROUP BY author;

CREATE OR REPLACE VIEW ctl_ticker_outcomes AS
SELECT
    ticker,
    direction,
    COUNT(*)                                                   AS thread_count,
    SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END)          AS closed_threads,
    ROUND(AVG(CASE WHEN state = 'closed' THEN closed_pnl_pct END), 3) AS avg_closed_pnl_pct,
    ROUND(AVG(CASE WHEN state = 'closed' THEN mfe_pct END), 3)        AS avg_mfe_pct,
    ROUND(AVG(CASE WHEN state = 'closed' THEN mae_pct END), 3)        AS avg_mae_pct,
    ROUND(AVG(CASE WHEN state = 'closed' THEN days_held END), 2)      AS avg_days_held
FROM trade_threads
GROUP BY ticker, direction
HAVING COUNT(*) > 0;

CREATE OR REPLACE VIEW ctl_recent_winners AS
SELECT thread_id, author, ticker, direction, opened_at, closed_at,
       closed_pnl_pct, mfe_pct, mae_pct, days_held, closed_reason
FROM trade_threads
WHERE state = 'closed' AND closed_pnl_pct > 0
ORDER BY closed_at DESC;

CREATE OR REPLACE VIEW ctl_recent_losers AS
SELECT thread_id, author, ticker, direction, opened_at, closed_at,
       closed_pnl_pct, mfe_pct, mae_pct, days_held, closed_reason
FROM trade_threads
WHERE state = 'closed' AND closed_pnl_pct <= 0
ORDER BY closed_at DESC;
"""

# Idempotent migration: add thread_id to trade_ideas if missing
_MIGRATION_V1_TO_V2 = """
ALTER TABLE trade_ideas ADD COLUMN IF NOT EXISTS thread_id TEXT;
ALTER TABLE trade_ideas ADD COLUMN IF NOT EXISTS thread_intent TEXT;
CREATE INDEX IF NOT EXISTS idx_trade_ideas_thread ON trade_ideas(thread_id);
"""

# v3 migration: multi-ticker support.
# Old PK was `tweet_id`, which couldn't represent a single post that
# references multiple tickers (e.g. `$INTC $AMD $GOOGL took some profits`).
# New PK is `idea_id` (synthetic `<tweet_id>:<ticker_or_underscore>`),
# with `tweet_id` retained as an indexed regular column.
#
# Migration approach (idempotent): rebuild the table only if the legacy PK
# is still on tweet_id. Detected via DESCRIBE → "key=PRI" on tweet_id.
_TRADE_IDEAS_V3_COLS = """
    idea_id            TEXT PRIMARY KEY,
    tweet_id           TEXT NOT NULL,
    posted_at          TIMESTAMP,
    author_handle      TEXT,
    raw_text           TEXT,
    is_trade_idea      BOOLEAN,
    classify_confidence DOUBLE,
    classify_reason    TEXT,
    ticker             TEXT,
    direction          TEXT,
    entry_text         TEXT,
    entry_price        DOUBLE,
    stop_text          TEXT,
    stop_price         DOUBLE,
    target_text        TEXT,
    target_price       DOUBLE,
    horizon            TEXT,
    tags               TEXT,
    extract_confidence DOUBLE,
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extractor_version  TEXT,
    llm_model          TEXT,
    thread_id          TEXT,
    thread_intent      TEXT
"""


def _con():
    CTL_DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(CTL_DUCKDB_PATH))


def _build_idea_id(tweet_id: str, ticker: str | None) -> str:
    """Synthesize an idea_id from (tweet_id, ticker).

    Convention:
      - `<tweet_id>:<TICKER>` for actual trade ideas. TICKER preserves $ prefix
        and futures suffix.
      - `<tweet_id>:_` for non-idea rows (commentary, classify_skip).

    The combination is stable + unique per (tweet, ticker), so re-runs over
    the same capture are idempotent and a single tweet that mentions N tickers
    gets N distinct rows.
    """
    return f"{tweet_id}:{ticker or '_'}"


def _needs_v3_migration(con) -> bool:
    """Return True iff trade_ideas exists with PK on tweet_id (pre-v3 shape).
    Returns False if table doesn't exist yet (CREATE will use v3) or if
    already migrated (idea_id present)."""
    try:
        rows = con.execute("DESCRIBE trade_ideas").fetchall()
    except Exception:
        return False  # table doesn't exist; CREATE will use v3 directly
    cols = {r[0]: r for r in rows}
    if "idea_id" in cols:
        return False  # already v3
    if "tweet_id" in cols and cols["tweet_id"][3] == "PRI":
        return True
    return False


def _migrate_v2_to_v3(con) -> None:
    """Rebuild trade_ideas with idea_id PK. Idempotent — skips if already
    migrated. Preserves existing rows; assigns each a derived idea_id."""
    if not _needs_v3_migration(con):
        return
    logging.getLogger(__name__).info("CTL v3 migration: rebuilding trade_ideas with idea_id PK")
    con.execute("BEGIN")
    try:
        # Drop indexes first — DuckDB blocks ALTER TABLE RENAME if any
        # index references the table. List them via the system catalog so
        # we drop whatever's actually present (set may have grown over time).
        idx_rows = con.execute("""
            SELECT index_name FROM duckdb_indexes()
            WHERE table_name = 'trade_ideas'
        """).fetchall()
        for (idx_name,) in idx_rows:
            con.execute(f'DROP INDEX IF EXISTS "{idx_name}"')

        # Stage existing rows
        con.execute("ALTER TABLE trade_ideas RENAME TO trade_ideas_v2")
        # Create v3 with the new PK
        con.execute(f"CREATE TABLE trade_ideas ({_TRADE_IDEAS_V3_COLS})")
        # Copy with derived idea_id. NULLIF guards against empty-string tickers.
        con.execute("""
            INSERT INTO trade_ideas (
                idea_id, tweet_id, posted_at, author_handle, raw_text,
                is_trade_idea, classify_confidence, classify_reason,
                ticker, direction, entry_text, entry_price,
                stop_text, stop_price, target_text, target_price,
                horizon, tags, extract_confidence,
                fetched_at, extractor_version, llm_model,
                thread_id, thread_intent
            )
            SELECT
                tweet_id || ':' || COALESCE(NULLIF(ticker, ''), '_'),
                tweet_id, posted_at, author_handle, raw_text,
                is_trade_idea, classify_confidence, classify_reason,
                ticker, direction, entry_text, entry_price,
                stop_text, stop_price, target_text, target_price,
                horizon, tags, extract_confidence,
                fetched_at, extractor_version, llm_model,
                thread_id, thread_intent
            FROM trade_ideas_v2
        """)
        con.execute("DROP TABLE trade_ideas_v2")
        con.execute("CREATE INDEX idx_trade_ideas_tweet      ON trade_ideas(tweet_id)")
        con.execute("CREATE INDEX idx_trade_ideas_posted_at  ON trade_ideas(posted_at)")
        con.execute("CREATE INDEX idx_trade_ideas_author     ON trade_ideas(author_handle)")
        con.execute("CREATE INDEX idx_trade_ideas_ticker     ON trade_ideas(ticker)")
        con.execute("CREATE INDEX idx_trade_ideas_thread     ON trade_ideas(thread_id)")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def ensure_schema():
    """Idempotent: creates v1 + v2 tables, runs the v1→v2 + v2→v3 migrations.

    Safe to call repeatedly. v3 migration (tweet_id PK → idea_id PK for
    multi-ticker support) only fires if the legacy structure is detected.
    """
    con = _con()
    try:
        con.execute(CTL_SCHEMA_SQL)
        for stmt in _MIGRATION_V1_TO_V2.strip().split(";\n"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        _migrate_v2_to_v3(con)
    finally:
        con.close()


# ── State (seen tweet IDs) ──────────────────────────────────────────────────


def load_state(state_file: Path = CTL_STATE_FILE) -> dict:
    if not state_file.exists():
        return {"processed_ids": []}
    try:
        return json.loads(state_file.read_text())
    except json.JSONDecodeError:
        return {"processed_ids": []}


def save_state(state: dict, state_file: Path = CTL_STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2))


# ── LLM provider (codex subprocess) ─────────────────────────────────────────


@dataclass
class ExtractedIdea:
    ticker:        str | None = None
    direction:     str | None = None      # "long" / "short"
    entry_text:    str | None = None
    entry_price:   float | None = None
    stop_text:     str | None = None
    stop_price:    float | None = None
    target_text:   str | None = None
    target_price:  float | None = None
    horizon:       str | None = None
    tags:          list[str] | None = None
    extract_confidence: float = 0.0


@dataclass
class Classification:
    is_trade_idea:        bool
    classify_confidence:  float
    classify_reason:      str = ""
    thread_intent:        str = "unsure"   # new | update | close | unsure


EXTRACTOR_PROMPT = """\
You are extracting structured trade ideas from financial X (Twitter) posts.

INPUT: a single post (text + author + timestamp).

TASK 1 — CLASSIFY: is this post an actionable trade idea?
A trade idea identifies a specific instrument and at least ONE of:
  - an entry trigger (price level, "here", "now", "@<price>", a price range)
  - a stop / risk level
  - a target / profit-take level
  - an explicit direction (long, short, "L", "S")

NOT trade ideas: market commentary, philosophy, sector views without
specifics, reply jokes, charts without entries.

POSITION UPDATES count as trade ideas if they reference a specific instrument
("$SB_F nice move so far", "$AMD trim 1/4 here", "stopped out of $XYZ").
Set thread_intent accordingly (see TASK 2.5).

A SINGLE post may reference MULTIPLE tickers — e.g.
"$INTC $AMD $GOOGL took some profits on these this AM FWIW".
In that case, list ALL tickers in the `tickers` array; the other fields
(direction, entry, stop, target, horizon, tags, intent) are understood
to apply to every ticker in the list. Single-ticker posts still use a
1-element `tickers` array.

TASK 2 — IF a trade idea, EXTRACT these fields (omit any that aren't stated):
  - tickers: ARRAY of tickers mentioned in this post. PRESERVE the dollar
    prefix and futures suffix exactly as written. One-element array if the
    post only references a single instrument. Examples are illustrative
    only; do NOT hallucinate tickers.
  - direction: "long" or "short". "L" / "buy" / "long" → "long".
                                 "S" / "sell" / "short" → "short".
  - entry_text: the raw phrase ("here", "@408", "1235-40 area", "on dip").
  - entry_price: numeric float if a single price (or middle of range).
  - stop_text: raw phrase ("Risk 14.94", "stop 1164", "Hard 1164").
  - stop_price: numeric float.
  - target_text: raw phrase.
  - target_price: numeric float.
  - horizon: "H4", "swing", "intraday", "scalp", "long-term", "position",
             or unspecified.
  - tags: array of any hashtags in the post (no leading #).
  - extract_confidence: 0.0–1.0 reflecting how clear the fields are.

TASK 2.5 — thread_intent: one of "new" | "update" | "close" | "unsure".
  - "new":    a fresh trade idea (initial entry call). Fields like ticker
              + direction + entry are typically all present.
  - "update": commentary on an already-open position ("still long",
              "nice move", "trim 1/4", "took some profits"). The post
              REFERENCES an instrument but isn't an initial entry.
  - "close":  explicit exit / stop-out / fully out / target hit
              ("stopped out", "out", "done", "fully exited").
  - "unsure": cannot tell from text alone. Default to "unsure" rather
              than guessing.

If is_trade_idea is false, set thread_intent to "unsure".

OUTPUT FORMAT — STRICT JSON ONLY, no prose around it:
{
  "is_trade_idea":      <bool>,
  "classify_confidence": <0.0-1.0>,
  "classify_reason":    "<short reason>",
  "thread_intent":      "new" | "update" | "close" | "unsure",
  "extraction":         { ... } | null
}

Set "extraction" to null when is_trade_idea is false.
"""


def call_codex(prompt: str, *, model: str | None = None,
               timeout_sec: int = 180) -> str:
    """Subprocess wrapper for `codex exec --json --skip-git-repo-check`.

    The current codex CLI (v0.101.0) doesn't have --quiet; it streams typed
    JSON events on stdout when --json is set. We parse for the agent_message
    item and return its text.
    """
    if shutil.which(CODEX_BIN) is None:
        raise RuntimeError(f"`{CODEX_BIN}` not found in PATH")
    cmd = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout_sec,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codex exit={proc.returncode}: {proc.stderr.strip()[-400:]}")
    # Parse the JSON event stream for the final agent_message
    msg_text = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = evt.get("item") or {}
        if evt.get("type") == "item.completed" and item.get("type") == "agent_message":
            msg_text = item.get("text") or msg_text
    if msg_text is None:
        raise RuntimeError(f"codex returned no agent_message; stdout tail: "
                           f"{proc.stdout[-300:]!r}")
    return msg_text


def _strip_json_fence(s: str) -> str:
    """LLMs often wrap JSON in ```json … ``` fences. Peel them off."""
    s = s.strip()
    if s.startswith("```"):
        # remove first ``` line
        lines = s.split("\n")
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def parse_llm_response(raw: str) -> dict:
    """Parse the codex response into the standard {is_trade_idea, ...} shape."""
    raw = _strip_json_fence(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Some models put JSON inside their reasoning. Find the first {...} block.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON object found in LLM response: {raw[:200]!r}")
        data = json.loads(m.group(0))
    return data


# JSON schema for a single trade-idea extraction. Used by both the
# single-post and batch paths.
#
# `tickers` (array) is the canonical field for multi-ticker support
# (e.g. "$INTC $AMD $GOOGL took profits"). For backward compatibility with
# older LLM outputs / cached responses, we also tolerate `ticker` (string)
# in `_parse_extraction_dict`. New prompts ask the model to emit `tickers`.
_GEMINI_IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "is_trade_idea": {"type": "boolean"},
        "classify_confidence": {"type": "number"},
        "classify_reason": {"type": "string"},
        "thread_intent": {"type": "string", "enum": ["new", "update", "close", "unsure"]},
        "extraction": {
            "type": "object",
            "properties": {
                "tickers":      {"type": "array", "items": {"type": "string"}},
                "ticker":       {"type": "string"},  # backward-compat fallback
                "direction":    {"type": "string", "enum": ["long", "short"]},
                "entry_text":   {"type": "string"},
                "entry_price":  {"type": "number"},
                "stop_text":    {"type": "string"},
                "stop_price":   {"type": "number"},
                "target_text":  {"type": "string"},
                "target_price": {"type": "number"},
                "horizon":      {"type": "string"},
                "tags":         {"type": "array", "items": {"type": "string"}},
                "extract_confidence": {"type": "number"},
            },
        },
    },
    "required": ["is_trade_idea", "classify_confidence", "thread_intent"],
}


def _gemini_extract(post_text: str, model: str | None = None) -> dict:
    """Default LLM path post-2026-05-18 migration: Gemini Flash with structured
    output. Returns the same {is_trade_idea, classify_confidence, classify_reason,
    thread_intent, extraction} dict shape the codex path produced.

    Single-post variant — kept for callers that want one-off extraction.
    For bulk runs, use `_gemini_extract_batch` (one Gemini call per ~15
    posts instead of one per post — orders-of-magnitude faster under the
    free-tier RPM ceiling).
    """
    if gemini_client is None:
        raise RuntimeError("gemini_client unavailable — install google-genai or "
                           "verify ~/clawd/scripts/lib/gemini_client.py on path")

    prompt = (EXTRACTOR_PROMPT + "\n\n--- POST ---\n" + post_text + "\n--- END POST ---\n")
    result = gemini_client.extract_structured(
        prompt, _GEMINI_IDEA_SCHEMA,
        model=model or gemini_client.DEFAULT_MODEL,
    )
    if result is None:
        raise RuntimeError("gemini extract_structured returned None")
    return result


# Per-batch cap. The CTL idea schema is ~3x the size of the x-general
# classify schema (nested extraction block), so we use a slightly smaller
# chunk to keep response tokens under max_output_tokens=2048. 15 fits
# comfortably and leaves headroom for verbose reasons / longer extractions.
_GEMINI_BATCH_CHUNK = 15


# Batch envelope schema — used by both the Gemini batch path AND the new
# external-LLM (CC subagent) path. Exposed at module level so callers can
# pass the schema to a subagent for structured-output validation.
BATCH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":                  {"type": "string"},
                    "is_trade_idea":       {"type": "boolean"},
                    "classify_confidence": {"type": "number"},
                    "classify_reason":     {"type": "string"},
                    "thread_intent": {
                        "type": "string",
                        "enum": ["new", "update", "close", "unsure"],
                    },
                    "extraction": _GEMINI_IDEA_SCHEMA["properties"]["extraction"],
                },
                "required": ["id", "is_trade_idea", "classify_confidence", "thread_intent"],
            },
        },
    },
    "required": ["results"],
}


def _build_batch_input_block(items: list[dict]) -> str:
    """Format a list of {id, text} dicts as the BATCH INPUT body the
    classifier prompt expects. Shared between Gemini and CC-subagent paths
    so the wire format can't drift."""
    return "\n\n".join(
        f'ID: {it["id"]}\nText: """{(it.get("text") or "")[:1500]}"""'
        for it in items
    )


def build_batch_prompt(
    posts: list[dict],
    *,
    state_file: Path = CTL_STATE_FILE,
) -> dict:
    """Build the full classification prompt + JSON schema for unseen posts.

    Use this when the LLM call happens OUTSIDE this Python process —
    e.g. when `/ctl-poll` (a CC slash command) delegates classification
    to a CC subagent via the Agent tool. Pair with `apply_classifications`
    to persist what the subagent returns.

    Args:
        posts: list of normalized post dicts (shape from
            `ideas.x_personal.normalize_capture`).
        state_file: state file containing already-processed tweet IDs.

    Returns:
        {
          "prompt":        <full prompt string with EXTRACTOR_PROMPT + items>,
          "schema":        <JSON schema for the {results: [...]} response>,
          "item_ids":      <ordered list of tweet_ids being classified>,
          "skipped_seen":  <int — how many posts were filtered out as already seen>,
          "total_unseen":  <int — len(item_ids)>,
        }

        When `total_unseen == 0`, `prompt` is "" — caller should skip the
        LLM step entirely and just run `apply_classifications` with an empty
        classifications dict to bump the state counter.
    """
    state = load_state(state_file)
    seen = set(state.get("processed_ids", []))
    unseen: list[dict] = []
    skipped_seen = 0
    for p in posts:
        tid = str(p.get("id") or "")
        if not tid:
            continue
        if tid in seen:
            skipped_seen += 1
            continue
        unseen.append(p)

    if not unseen:
        return {
            "prompt": "",
            "schema": BATCH_RESULT_SCHEMA,
            "item_ids": [],
            "skipped_seen": skipped_seen,
            "total_unseen": 0,
        }

    items_block = _build_batch_input_block(
        [{"id": str(p["id"]), "text": p.get("text") or ""} for p in unseen]
    )
    prompt = (
        EXTRACTOR_PROMPT
        + "\n\n--- BATCH INPUT ---\n"
          "You will receive MULTIPLE posts below. For EACH post, return the\n"
          "same per-post object shape described above, but in a top-level\n"
          "`results` array. Echo each post's ID exactly in the `id` field.\n"
          "Do NOT merge, summarize, or skip posts — one result entry per ID.\n\n"
        + items_block
        + "\n--- END BATCH ---\n"
    )

    return {
        "prompt": prompt,
        "schema": BATCH_RESULT_SCHEMA,
        "item_ids": [str(p["id"]) for p in unseen],
        "skipped_seen": skipped_seen,
        "total_unseen": len(unseen),
    }


def apply_classifications(
    posts: list[dict],
    classifications: dict,
    *,
    dry_run: bool = False,
    state_file: Path = CTL_STATE_FILE,
    thread_link: bool = True,
    emit_alerts: bool = True,
    llm_model: str = "cc-subagent",
    extractor_version: str = "v1",
) -> dict:
    """Persist classifications produced by an external LLM call.

    Companion to `build_batch_prompt`. Bypasses the internal Gemini path
    entirely. Validation, threading, alerting, state-file bump, and log
    line are unchanged from `process_posts`.

    Args:
        posts: list of normalized post dicts (full set, not just unseen —
            we re-filter here so this remains the source-of-truth idempotency
            boundary).
        classifications: the LLM's response, shape:
            {"results": [{id, is_trade_idea, classify_confidence,
                          classify_reason, thread_intent, extraction}, ...]}
            (matches `BATCH_RESULT_SCHEMA`). Extra keys ignored; missing
            entries logged as `extracted_dropped`.
        llm_model: tag persisted to `trade_ideas.llm_model` for every row
            (default 'cc-subagent' for CC-orchestrated runs).

    Returns:
        Same shape as `process_posts.summary`.
    """
    # Import here to avoid module-load cycle
    from . import ctl_threads
    state = load_state(state_file)
    seen = set(state.get("processed_ids", []))

    summary = {
        "started_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched":     len(posts),
        "skipped_seen":      0,
        "classified_idea":   0,
        "classified_skip":   0,
        "extracted_valid":   0,
        "extracted_dropped": 0,
        "errors":            [],
        "ideas":             [],
        "dry_run":           dry_run,
        "model":             llm_model,
    }

    # Index classifications by id for O(1) lookup
    by_id: dict[str, dict] = {}
    if isinstance(classifications, dict) and isinstance(classifications.get("results"), list):
        for r in classifications["results"]:
            if isinstance(r, dict):
                rid = str(r.get("id") or "").strip()
                if rid:
                    by_id[rid] = r

    for p in posts:
        tid = str(p.get("id") or "")
        if not tid:
            summary["errors"].append({"tweet_id": None, "error": "missing id"})
            continue
        if tid in seen:
            summary["skipped_seen"] += 1
            continue

        text   = p.get("text") or ""
        author = (p.get("author") or {}).get("username") or p.get("handle") or ""
        ts     = p.get("created_at")

        data = by_id.get(tid)
        if data is None:
            summary["errors"].append({
                "tweet_id": tid,
                "error":    "no classification returned for this id",
            })
            continue

        try:
            cls, ideas = _parse_extraction_dict_all(data)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "tweet_id": tid,
                "error":    f"parse: {type(e).__name__}: {e}",
            })
            continue

        if not cls.is_trade_idea:
            summary["classified_skip"] += 1
            if not dry_run:
                clear_tweet_rows(tid)
                upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                             raw_text=text, cls=cls, idea=None,
                             llm_model=llm_model,
                             extractor_version=extractor_version)
                seen.add(tid)
            continue

        summary["classified_idea"] += 1
        if not ideas:
            summary["extracted_dropped"] += 1
            summary["errors"].append({
                "tweet_id": tid,
                "error": "validation: missing ticker (LLM returned empty tickers list)",
                "raw_text": text[:140],
            })
            continue

        valid_ideas: list[ExtractedIdea] = []
        for idea in ideas:
            ok, reason = is_valid_idea(idea)
            if ok:
                valid_ideas.append(idea)
            else:
                summary["errors"].append({
                    "tweet_id": tid,
                    "error":    f"validation ({idea.ticker!r}): {reason}",
                })
        if not valid_ideas:
            summary["extracted_dropped"] += 1
            continue

        if not dry_run:
            clear_tweet_rows(tid)

        for idea in valid_ideas:
            summary["extracted_valid"] += 1
            if not dry_run:
                upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                             raw_text=text, cls=cls, idea=idea,
                             llm_model=llm_model,
                             extractor_version=extractor_version)
                if thread_link and idea.ticker:
                    try:
                        thread_id, action, thread_state = \
                            ctl_threads.upsert_thread_for_post(
                                tweet_id=tid, posted_at=ts,
                                author=author, ticker=idea.ticker,
                                cls=cls, idea=idea,
                            )
                        if emit_alerts and thread_id:
                            _emit_thread_alert(action, thread_id, thread_state,
                                                cls, idea, ctl_threads)
                    except Exception as e:  # noqa: BLE001
                        summary["errors"].append({
                            "tweet_id": tid,
                            "error":    f"thread/alert ({idea.ticker!r}): {type(e).__name__}: {e}",
                        })
            summary["ideas"].append({
                "tweet_id": tid, "author": author, "ticker": idea.ticker,
                "direction": idea.direction, "entry": idea.entry_text,
                "stop": idea.stop_text, "target": idea.target_text,
                "horizon": idea.horizon, "confidence": idea.extract_confidence,
                "thread_intent": cls.thread_intent,
            })
        if not dry_run:
            seen.add(tid)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        state["processed_ids"] = sorted(seen)[-2000:]
        save_state(state, state_file)
        try:
            CTL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CTL_LOG_FILE.open("a") as f:
                f.write(f"{summary['finished_at']} fetched={summary['fetched']} "
                        f"new_ideas={summary['extracted_valid']} "
                        f"skip_commentary={summary['classified_skip']} "
                        f"errors={len(summary['errors'])} model={llm_model}\n")
        except OSError:
            pass

    return summary


def _gemini_extract_batch(
    items: list[dict],
    *,
    model: str | None = None,
    chunk_size: int = _GEMINI_BATCH_CHUNK,
) -> dict[str, dict]:
    """Batch trade-idea extraction.

    Args:
        items: list of {"id": str, "text": str} dicts.
        model: Gemini model id (defaults to gemini_client.DEFAULT_MODEL).
        chunk_size: posts per Gemini call.

    Returns:
        {tweet_id: {is_trade_idea, classify_confidence, classify_reason,
                    thread_intent, extraction}}

        Missing entries (Gemini didn't return a row for that id, OR the call
        failed entirely) are simply absent from the dict. Caller should
        treat absence as "skip this post / log an error".
    """
    if gemini_client is None:
        raise RuntimeError("gemini_client unavailable — install google-genai or "
                           "verify ~/clawd/scripts/lib/gemini_client.py on path")
    if not items:
        return {}

    # Shared schema with the CC-subagent path (BATCH_RESULT_SCHEMA).
    batch_schema = BATCH_RESULT_SCHEMA

    out: dict[str, dict] = {}
    eff_model = model or gemini_client.DEFAULT_MODEL

    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        posts_block = _build_batch_input_block(chunk)
        prompt = (
            EXTRACTOR_PROMPT
            + "\n\n--- BATCH INPUT ---\n"
              "You will receive MULTIPLE posts below. For EACH post, return the\n"
              "same per-post object shape described above, but in a top-level\n"
              "`results` array. Echo each post's ID exactly in the `id` field.\n"
              "Do NOT merge, summarize, or skip posts — one result entry per ID.\n\n"
            + posts_block
            + "\n--- END BATCH ---\n"
        )
        # Higher max_output_tokens for batch — 15 results × ~250 tokens each
        # comfortably fits under 8192. We also need to override the default
        # 2048 ceiling baked into extract_structured for the per-post case.
        raw = _gemini_call_batch(prompt, batch_schema, model=eff_model)
        if not raw or not isinstance(raw.get("results"), list):
            continue
        for r in raw["results"]:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            out[rid] = r

    return out


def _gemini_call_batch(prompt: str, schema: dict, *, model: str) -> dict | None:
    """Variant of gemini_client.extract_structured that uses a bigger output
    budget for batched calls. We can't pass max_output_tokens through the
    public client API yet, so we duplicate the small slice of logic here.

    Falls back to the public extract_structured() on any import / setup
    issue, so a failure here doesn't take the pipeline down.

    Honors the soft free-tier-equivalent RPD limit (see
    `gemini_client.check_rpd`) — refuses the call BEFORE hitting the API
    if today's per-model budget is depleted. Set GEMINI_RPD_LIMIT=0 in env
    to disable.
    """
    try:
        gemini_client.check_rpd(model)
    except gemini_client.GeminiRPDExceeded as e:
        logging.getLogger(__name__).warning("%s", e)
        return None

    # Pace under the free-tier RPM ceiling. Matches describe-frames-gemini.py
    # cadence pattern (proven on free tier for ~5K daily frame descriptions).
    gemini_client._pace_call(model)  # pylint: disable=protected-access

    try:
        # Inline the call — same retry behavior as the shared client, but
        # with max_output_tokens widened to 8192.
        from google.genai import types as gtypes  # type: ignore[import-not-found]
        import json as _json
        import re as _re
        import time as _time

        client = gemini_client._client()  # pylint: disable=protected-access
        config = gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=8192,
        )
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                result = client.models.generate_content(
                    model=model, contents=prompt, config=config,
                )
                gemini_client._increment_rpd(model)  # pylint: disable=protected-access
                text = (result.text or "").strip()
                if not text:
                    return None
                return _json.loads(text)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                is_429 = "RESOURCE_EXHAUSTED" in msg or "429" in msg or "quota" in msg.lower()
                is_503 = "UNAVAILABLE" in msg or "503" in msg or "high demand" in msg.lower()
                is_500 = "INTERNAL" in msg or "500" in msg
                retryable = is_429 or is_503 or is_500
                if retryable and attempt < max_retries:
                    if is_429:
                        # Daily-quota exhaustion can't be retried same-day.
                        if "PerDay" in msg or "RequestsPerDay" in msg:
                            logging.getLogger(__name__).warning(
                                "gemini daily quota exhausted — not retrying",
                            )
                            return None
                        m  = _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", msg)
                        m2 = _re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+)s", msg)
                        delay = float(m.group(1)) if m else (float(m2.group(1)) if m2 else 15.0)
                        # Respect server-requested delay; cap at 90s to bound
                        # the worst per-attempt wait.
                        delay = min(delay + 1.0, 90.0)
                    else:
                        delay = min(2.0 * (2 ** attempt), 20.0)
                    _time.sleep(delay)
                    continue
                logging.getLogger(__name__).warning(
                    "gemini batch call failed: %s", e,
                )
                return None
    except ImportError:
        # google.genai SDK absent — fall back to the public path (smaller
        # token budget). Better to truncate than to crash.
        return gemini_client.extract_structured(prompt, schema, model=model)
    return None


def _normalize_tickers(ext: dict) -> list[str]:
    """Extract the ticker list from an LLM extraction object, supporting
    both the new `tickers: array` schema and the legacy `ticker: string`
    fallback. De-dups, preserves order, drops empty/whitespace entries.
    """
    out: list[str] = []
    seen = set()
    arr = ext.get("tickers")
    if isinstance(arr, list):
        for t in arr:
            if isinstance(t, str):
                t = t.strip()
                if t and t not in seen:
                    out.append(t)
                    seen.add(t)
    single = ext.get("ticker")
    if isinstance(single, str):
        single = single.strip()
        if single and single not in seen:
            out.append(single)
            seen.add(single)
    return out


def _parse_extraction_dict_all(data: dict) -> tuple[Classification, list[ExtractedIdea]]:
    """Convert a raw LLM-output dict into (Classification, [ExtractedIdea, ...]).

    For multi-ticker posts, returns one ExtractedIdea per ticker — all sharing
    the same direction / entry / stop / target / horizon / tags / confidence
    fields (those describe the action, which applies uniformly to every
    ticker the post references).

    Returns an empty list when the post isn't a trade idea, or when it's
    classified as a trade idea but the extraction block is missing or has
    no tickers (caller treats as `extracted_dropped`).
    """
    intent = (data.get("thread_intent") or "unsure").lower().strip()
    if intent not in {"new", "update", "close", "unsure"}:
        intent = "unsure"
    cls = Classification(
        is_trade_idea=bool(data.get("is_trade_idea", False)),
        classify_confidence=float(data.get("classify_confidence", 0.0)),
        classify_reason=str(data.get("classify_reason", "")),
        thread_intent=intent,
    )
    if not cls.is_trade_idea:
        return cls, []
    ext = data.get("extraction") or {}
    if not ext:
        return cls, []
    tickers = _normalize_tickers(ext)
    if not tickers:
        return cls, []
    # All non-ticker fields are shared across tickers in the same post.
    shared = dict(
        direction=ext.get("direction"),
        entry_text=ext.get("entry_text"),
        entry_price=_to_float(ext.get("entry_price")),
        stop_text=ext.get("stop_text"),
        stop_price=_to_float(ext.get("stop_price")),
        target_text=ext.get("target_text"),
        target_price=_to_float(ext.get("target_price")),
        horizon=ext.get("horizon"),
        tags=list(ext.get("tags") or []),
        extract_confidence=float(ext.get("extract_confidence", 0.0)),
    )
    return cls, [ExtractedIdea(ticker=t, **shared) for t in tickers]


def _parse_extraction_dict(data: dict) -> tuple[Classification, ExtractedIdea | None]:
    """Single-idea backward-compatible parse. Returns the FIRST ticker's
    ExtractedIdea (or None) — used by `classify_and_extract` so test stubs
    that inject `llm_call` with the legacy single-ticker response shape
    keep working unchanged.

    New callers (process_posts) should prefer `_parse_extraction_dict_all`
    so multi-ticker posts produce N rows.
    """
    cls, ideas = _parse_extraction_dict_all(data)
    return cls, (ideas[0] if ideas else None)


def classify_and_extract(post_text: str, *, llm_call=None,
                          model: str | None = None) -> tuple[Classification, ExtractedIdea | None]:
    """One LLM round-trip per post. `llm_call` is injected for tests.

    Default path (2026-05-18+): Gemini Flash via the shared client.
    Legacy path (pre-2026-05-18): codex CLI / gpt-5. Still available via
    `llm_call=lambda p: call_codex(p)` for testing/comparison.

    Returns the FIRST extracted idea only — for multi-ticker support use
    `classify_and_extract_all` below.

    Note on `model`: when llm_call is None and model is None, the Gemini
    client's DEFAULT_MODEL (gemini-2.5-flash-lite) is used. Callers can
    pass `model="gemini-2.5-flash"` for higher quality at the cost of
    lower free-tier RPM.
    """
    cls, ideas = classify_and_extract_all(post_text, llm_call=llm_call, model=model)
    return cls, (ideas[0] if ideas else None)


def classify_and_extract_all(post_text: str, *, llm_call=None,
                              model: str | None = None) -> tuple[Classification, list[ExtractedIdea]]:
    """Multi-ticker variant of classify_and_extract.

    For posts that reference multiple tickers (e.g. "$INTC $AMD took profits"),
    returns one ExtractedIdea per ticker — all sharing the post's direction,
    entry/stop/target text, horizon, tags. Commentary / non-idea posts return
    an empty list.

    NB: For bulk runs, `process_posts` uses `_gemini_extract_batch` instead
    of looping over this function — single-post calls are orders of magnitude
    slower under the free-tier RPM ceiling.
    """
    if llm_call is None:
        effective_model = model or (gemini_client.DEFAULT_MODEL if gemini_client else None)
        data = _gemini_extract(post_text, model=effective_model)
    else:
        prompt = (EXTRACTOR_PROMPT + "\n\n--- POST ---\n" + post_text + "\n--- END POST ---\n"
                  "Reply with JSON only.")
        raw = llm_call(prompt)
        data = parse_llm_response(raw)
    return _parse_extraction_dict_all(data)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Validation ──────────────────────────────────────────────────────────────


_TICKER_RE = re.compile(r"^\$?[A-Z][A-Z0-9._]{0,8}(_F)?$")


def is_valid_idea(idea: ExtractedIdea) -> tuple[bool, str]:
    """Drop low-quality extractions before persisting."""
    if not idea.ticker:
        return False, "missing ticker"
    if not _TICKER_RE.match(idea.ticker):
        return False, f"invalid ticker: {idea.ticker!r}"
    if idea.direction and idea.direction not in {"long", "short"}:
        return False, f"invalid direction: {idea.direction!r}"
    if not (idea.entry_text or idea.stop_text or idea.target_text or idea.direction):
        return False, "no entry/stop/target/direction"
    return True, ""


# ── Persistence ─────────────────────────────────────────────────────────────


def _normalize_timestamp(ts: Any) -> str | None:
    """Best-effort ISO-8601 normalize. Returns None for unparseable values
    so a bad timestamp on one post doesn't tank the whole batch."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    s = str(ts).strip()
    # Common shapes: "2026-05-05T08:13:14.000Z", "2026-05-05T08:13:14Z"
    try:
        # Replace trailing Z with explicit UTC and parse
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        datetime.fromisoformat(s2)
        return s2
    except (ValueError, AttributeError):
        return None


def clear_tweet_rows(tweet_id: str) -> int:
    """Remove ALL rows in trade_ideas for the given tweet_id. Used before
    re-persisting a multi-ticker post so a previous run's set is replaced
    cleanly. Returns the count deleted (best-effort)."""
    ensure_schema()
    con = _con()
    try:
        rows = con.execute("SELECT COUNT(*) FROM trade_ideas WHERE tweet_id = ?", [tweet_id]).fetchone()[0]
        con.execute("DELETE FROM trade_ideas WHERE tweet_id = ?", [tweet_id])
        con.commit()
        return rows
    finally:
        con.close()


def upsert_idea(*, tweet_id: str, posted_at, author_handle: str, raw_text: str,
                 cls: Classification, idea: ExtractedIdea | None,
                 extractor_version: str = "v1", llm_model: str | None = None) -> None:
    """Idempotent single-row insert keyed on idea_id (= `<tweet_id>:<ticker>`).

    Called once per (tweet, ticker) pair. For multi-ticker posts the caller
    drives the loop — invoke `clear_tweet_rows(tweet_id)` first to wipe a
    prior run's state, then call `upsert_idea` once per ticker.

    For backward compat with the v2 behavior (single-row-per-tweet), this
    function STILL wipes any prior row at the same idea_id before inserting
    — so a re-run on the same (tweet, ticker) pair stays a clean replace.
    """
    ensure_schema()
    posted_at_norm = _normalize_timestamp(posted_at)
    idea_id = _build_idea_id(tweet_id, idea.ticker if idea else None)
    con = _con()
    try:
        con.execute("DELETE FROM trade_ideas WHERE idea_id = ?", [idea_id])
        tags_json = json.dumps(idea.tags) if (idea and idea.tags) else None
        con.execute("""
            INSERT INTO trade_ideas (
                idea_id, tweet_id, posted_at, author_handle, raw_text,
                is_trade_idea, classify_confidence, classify_reason,
                ticker, direction, entry_text, entry_price,
                stop_text, stop_price, target_text, target_price,
                horizon, tags, extract_confidence,
                extractor_version, llm_model
            ) VALUES (?,?,?,?,?, ?,?,?, ?,?,?,?,?,?,?,?, ?,?,?, ?,?)
        """, [
            idea_id,
            tweet_id, posted_at_norm, author_handle, raw_text,
            cls.is_trade_idea, cls.classify_confidence, cls.classify_reason,
            idea.ticker if idea else None,
            idea.direction if idea else None,
            idea.entry_text if idea else None,
            idea.entry_price if idea else None,
            idea.stop_text if idea else None,
            idea.stop_price if idea else None,
            idea.target_text if idea else None,
            idea.target_price if idea else None,
            idea.horizon if idea else None,
            tags_json,
            idea.extract_confidence if idea else None,
            extractor_version,
            llm_model,
        ])
        con.commit()
    finally:
        con.close()


def _emit_thread_alert(action: str, thread_id: str, thread_state: dict,
                         cls: "Classification", idea: "ExtractedIdea",
                         ctl_threads_mod) -> None:
    """Map a (action, idea, thread_state) tuple to the right alert event,
    if any. action is the return from upsert_thread_for_post:
        opened_new | stale_then_opened_new | appended_to_open
        | closed_existing | closed_orphan | skipped_no_thread

    Only fires alerts for fresh ideas + significant updates; commentary
    that just rolled into an open thread without changing key fields
    doesn't.
    """
    direction_label = (idea.direction or "?")[:1].upper()
    base_summary = (f"{idea.ticker} {direction_label} @{thread_state.get('author','?')}"
                     f" — {idea.entry_text or '?'}"
                     f" / S:{idea.stop_text or '—'}"
                     f" / T:{idea.target_text or '—'}")

    if action in ("opened_new", "stale_then_opened_new"):
        event_type = "new_thread"
        summary = "🆕 " + base_summary
    elif action in ("closed_existing", "closed_orphan"):
        event_type = "thread_closed"
        summary = "🚪 close — " + base_summary
    elif action == "appended_to_open":
        # Only alert on substantive updates: a NEW stop/target/entry value
        # shows up vs prior state. (Commentary "nice move" doesn't change
        # any field — it just bumps post_count + last_update_at.)
        prior = (thread_state.get("post_count") or 1) - 1
        substantive = bool(
            idea.entry_text or idea.stop_text or idea.target_text or idea.direction
        )
        if prior == 0 or not substantive:
            return  # no alert for trivial updates
        event_type = "update"
        summary = "📌 update — " + base_summary
    else:
        return

    payload = {
        "thread_id":   thread_id,
        "ticker":      idea.ticker,
        "direction":   idea.direction,
        "entry_text":  idea.entry_text,
        "stop_text":   idea.stop_text,
        "target_text": idea.target_text,
        "horizon":     idea.horizon,
        "confidence":  idea.extract_confidence,
        "action":      action,
    }
    ctl_threads_mod.maybe_alert(
        ctl_threads_mod.AlertEvent(
            thread_id=thread_id, event_type=event_type,
            summary=summary, payload=payload,
        )
    )


def list_recent_ideas(limit: int = 20) -> list[dict]:
    ensure_schema()
    con = _con()
    try:
        rows = con.execute("""
            SELECT tweet_id, posted_at, author_handle, ticker, direction,
                   entry_text, stop_text, target_text, horizon,
                   classify_confidence, extract_confidence
            FROM trade_ideas
            WHERE is_trade_idea = TRUE
            ORDER BY posted_at DESC
            LIMIT ?
        """, [limit]).fetchall()
        cols = ["tweet_id","posted_at","author_handle","ticker","direction",
                "entry_text","stop_text","target_text","horizon",
                "classify_confidence","extract_confidence"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


# ── Top-level pipeline ──────────────────────────────────────────────────────


def process_posts(posts: list[dict], *, dry_run: bool = False,
                   llm_call=None, model: str | None = None,
                   state_file: Path = CTL_STATE_FILE,
                   thread_link: bool = True,
                   emit_alerts: bool = True) -> dict:
    """Run capture → classify → extract → persist → thread-link → alert
    over a list of normalized post dicts (shape from
    ideas.x_personal.normalize_capture).

    `thread_link=True` (default) populates `trade_ideas.thread_id` and
    creates/updates `trade_threads`.
    `emit_alerts=True` (default) routes new-thread/update events to the
    log-only alert channel via ideas.ctl_threads.maybe_alert.
    """
    # Import here to avoid module-load cycle (ctl_threads imports ctl_extractor)
    from . import ctl_threads
    state = load_state(state_file)
    seen = set(state.get("processed_ids", []))

    # Resolve the model tag we'll persist with each row. When the caller
    # injects llm_call (legacy codex path or test stub), default to
    # "codex" — there's no Gemini model in play. When using the default
    # path, fall back to the Gemini client's DEFAULT_MODEL.
    if llm_call is not None:
        effective_model = model or "codex"
    else:
        effective_model = model or (
            gemini_client.DEFAULT_MODEL if gemini_client else "unknown"
        )

    summary = {
        "started_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched":     len(posts),
        "skipped_seen":      0,
        "classified_idea":   0,
        "classified_skip":   0,
        "extracted_valid":   0,
        "extracted_dropped": 0,
        "errors":            [],
        "ideas":             [],
        "dry_run":           dry_run,
        "model":             effective_model,
    }

    # Pre-filter to unseen posts so we don't waste Gemini tokens on dupes.
    unseen: list[dict] = []
    for p in posts:
        tid = str(p.get("id") or "")
        if not tid:
            summary["errors"].append({"tweet_id": None, "error": "missing id"})
            continue
        if tid in seen:
            summary["skipped_seen"] += 1
            continue
        unseen.append(p)

    # Default (Gemini) path: one batched call per ~15 posts. This brings
    # 108 tweets from "5+ minutes serial w/ retries" down to "~30 seconds".
    # The legacy path (when caller injects llm_call) stays per-post — tests
    # that mock the LLM expect that behavior.
    batch_results: dict[str, dict] = {}
    if llm_call is None and unseen:
        batch_items = [
            {"id": str(p.get("id")), "text": p.get("text") or ""}
            for p in unseen
        ]
        try:
            batch_results = _gemini_extract_batch(batch_items, model=effective_model)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "tweet_id": None,
                "error":    f"batch_extract: {type(e).__name__}: {e}",
            })
            batch_results = {}

    for p in unseen:
        tid = str(p.get("id") or "")
        text   = p.get("text") or ""
        author = (p.get("author") or {}).get("username") or p.get("handle") or ""
        ts     = p.get("created_at")

        try:
            if llm_call is None:
                # Default path: pluck the batch result for this id. If Gemini
                # dropped it (rare), fall back to a single-post call so we
                # don't silently lose ideas.
                data = batch_results.get(tid)
                if data is None:
                    data = _gemini_extract(text, model=effective_model)
                cls, ideas = _parse_extraction_dict_all(data)
            else:
                # Legacy / test path — per-post LLM call.
                cls, ideas = classify_and_extract_all(
                    text, llm_call=llm_call, model=model,
                )
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "tweet_id": tid,
                "error":    f"{type(e).__name__}: {e}",
            })
            continue

        if not cls.is_trade_idea:
            summary["classified_skip"] += 1
            if not dry_run:
                # Replace any prior multi-ticker rows for this tweet, then
                # write a single commentary marker row (ticker=None).
                clear_tweet_rows(tid)
                upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                             raw_text=text, cls=cls, idea=None,
                             llm_model=effective_model)
                seen.add(tid)
            continue

        # Classified as a trade idea — validate per-ticker, then persist
        # ALL valid ticker variants of this single post.
        summary["classified_idea"] += 1
        if not ideas:
            # LLM said is_trade_idea=true but emitted no tickers — the
            # extraction is unusable. Capture as a drop with the raw text
            # so we can spot-check why the model couldn't pick a ticker
            # (commonly: multi-ticker post + ambiguous schema; covered
            # post-2026-05-18 by the `tickers: array` field).
            summary["extracted_dropped"] += 1
            summary["errors"].append({
                "tweet_id": tid,
                "error": "validation: missing ticker (LLM returned empty tickers list)",
                "raw_text": text[:140],
            })
            continue

        valid_ideas: list[ExtractedIdea] = []
        for idea in ideas:
            ok, reason = is_valid_idea(idea)
            if ok:
                valid_ideas.append(idea)
            else:
                summary["errors"].append({
                    "tweet_id": tid,
                    "error":    f"validation ({idea.ticker!r}): {reason}",
                })
        if not valid_ideas:
            summary["extracted_dropped"] += 1
            continue

        if not dry_run:
            # Wipe prior rows for this tweet first (covers shrinking from
            # N tickers to N-1, or a re-run that yielded a different set).
            clear_tweet_rows(tid)

        for idea in valid_ideas:
            summary["extracted_valid"] += 1
            if not dry_run:
                upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                             raw_text=text, cls=cls, idea=idea,
                             llm_model=effective_model)
                # Thread linkage + alert (after the post row exists).
                # Each ticker gets its OWN thread keyed on (author, ticker).
                if thread_link and idea.ticker:
                    try:
                        thread_id, action, thread_state = \
                            ctl_threads.upsert_thread_for_post(
                                tweet_id=tid, posted_at=ts,
                                author=author, ticker=idea.ticker,
                                cls=cls, idea=idea,
                            )
                        if emit_alerts and thread_id:
                            _emit_thread_alert(action, thread_id, thread_state,
                                                cls, idea, ctl_threads)
                    except Exception as e:  # noqa: BLE001
                        summary["errors"].append({
                            "tweet_id": tid,
                            "error":    f"thread/alert ({idea.ticker!r}): {type(e).__name__}: {e}",
                        })
            summary["ideas"].append({
                "tweet_id": tid, "author": author, "ticker": idea.ticker,
                "direction": idea.direction, "entry": idea.entry_text,
                "stop": idea.stop_text, "target": idea.target_text,
                "horizon": idea.horizon, "confidence": idea.extract_confidence,
                "thread_intent": cls.thread_intent,
            })
        if not dry_run:
            seen.add(tid)

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        state["processed_ids"] = sorted(seen)[-2000:]
        save_state(state, state_file)
        # One-line append to the log
        try:
            CTL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CTL_LOG_FILE.open("a") as f:
                f.write(f"{summary['finished_at']} fetched={summary['fetched']} "
                        f"new_ideas={summary['extracted_valid']} "
                        f"skip_commentary={summary['classified_skip']} "
                        f"errors={len(summary['errors'])}\n")
        except OSError:
            pass

    return summary
