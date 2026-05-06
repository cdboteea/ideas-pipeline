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
CREATE TABLE IF NOT EXISTS trade_ideas (
    -- identity
    tweet_id           TEXT PRIMARY KEY,
    posted_at          TIMESTAMP,
    author_handle      TEXT,            -- e.g. "CTLFutures"
    raw_text           TEXT,

    -- classifier output
    is_trade_idea      BOOLEAN,
    classify_confidence DOUBLE,         -- 0.0–1.0
    classify_reason    TEXT,

    -- extraction
    ticker             TEXT,            -- "$SB_F", "$AMD" (preserves $ prefix)
    direction          TEXT,            -- "long" / "short" / NULL
    entry_text         TEXT,            -- raw entry phrase ("here", "@408", "1235-40 area")
    entry_price        DOUBLE,          -- parsed numeric if extractable
    stop_text          TEXT,            -- raw stop phrase ("Risk 14.94", "Hard 1164")
    stop_price         DOUBLE,          -- parsed numeric
    target_text        TEXT,
    target_price       DOUBLE,
    horizon            TEXT,            -- "H4", "Swing", "intraday", "long-term"
    tags               TEXT,            -- JSON array of #tags
    extract_confidence DOUBLE,

    -- ops
    fetched_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extractor_version  TEXT,            -- "v1"
    llm_model          TEXT             -- "gpt-5.5" / "claude-code" / etc.
);

CREATE INDEX IF NOT EXISTS idx_trade_ideas_posted_at ON trade_ideas(posted_at);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_author    ON trade_ideas(author_handle);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_ticker    ON trade_ideas(ticker);
"""


def _con():
    CTL_DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(CTL_DUCKDB_PATH))


def ensure_schema():
    con = _con()
    try:
        con.execute(CTL_SCHEMA_SQL)
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
specifics, position updates ("nice move", "still holding"), reply jokes,
charts without entries.

TASK 2 — IF a trade idea, EXTRACT these fields (omit any that aren't stated):
  - ticker: PRESERVE the dollar prefix and futures suffix exactly as written.
    Examples: "$SB_F", "$AMD", "$SPY", "$BTC".
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
  - tags: array of any #hashtags in the post (no leading #).
  - extract_confidence: 0.0–1.0 reflecting how clear the fields are.

OUTPUT FORMAT — STRICT JSON ONLY, no prose around it:
{
  "is_trade_idea":      <bool>,
  "classify_confidence": <0.0-1.0>,
  "classify_reason":    "<short reason>",
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


def classify_and_extract(post_text: str, *, llm_call=None,
                          model: str | None = None) -> tuple[Classification, ExtractedIdea | None]:
    """One LLM round-trip per post. `llm_call` is injected for tests."""
    if llm_call is None:
        llm_call = lambda p: call_codex(p, model=model)
    prompt = (EXTRACTOR_PROMPT + "\n\n--- POST ---\n" + post_text + "\n--- END POST ---\n"
              "Reply with JSON only.")
    raw = llm_call(prompt)
    data = parse_llm_response(raw)

    cls = Classification(
        is_trade_idea=bool(data.get("is_trade_idea", False)),
        classify_confidence=float(data.get("classify_confidence", 0.0)),
        classify_reason=str(data.get("classify_reason", "")),
    )
    if not cls.is_trade_idea:
        return cls, None
    ext = data.get("extraction") or {}
    if not ext:
        return cls, None
    idea = ExtractedIdea(
        ticker=ext.get("ticker"),
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
    return cls, idea


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


def upsert_idea(*, tweet_id: str, posted_at, author_handle: str, raw_text: str,
                 cls: Classification, idea: ExtractedIdea | None,
                 extractor_version: str = "v1", llm_model: str | None = None) -> None:
    """Idempotent insert. DuckDB has no native UPSERT-with-PK, so we
    DELETE-then-INSERT (consistent with the citrini-pipeline pattern)."""
    ensure_schema()
    posted_at_norm = _normalize_timestamp(posted_at)
    con = _con()
    try:
        con.execute("DELETE FROM trade_ideas WHERE tweet_id = ?", [tweet_id])
        tags_json = json.dumps(idea.tags) if (idea and idea.tags) else None
        con.execute("""
            INSERT INTO trade_ideas (
                tweet_id, posted_at, author_handle, raw_text,
                is_trade_idea, classify_confidence, classify_reason,
                ticker, direction, entry_text, entry_price,
                stop_text, stop_price, target_text, target_price,
                horizon, tags, extract_confidence,
                extractor_version, llm_model
            ) VALUES (?,?,?,?, ?,?,?, ?,?,?,?,?,?,?,?, ?,?,?, ?,?)
        """, [
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
                   state_file: Path = CTL_STATE_FILE) -> dict:
    """Run capture → classify → extract → persist over a list of normalized
    post dicts (shape from ideas.x_personal.normalize_capture)."""
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
        "model":             model,
    }

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
        try:
            cls, idea = classify_and_extract(text, llm_call=llm_call, model=model)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "tweet_id": tid,
                "error":    f"{type(e).__name__}: {e}",
            })
            continue

        if not cls.is_trade_idea:
            summary["classified_skip"] += 1
            if not dry_run:
                upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                             raw_text=text, cls=cls, idea=None, llm_model=model)
                seen.add(tid)
            continue

        summary["classified_idea"] += 1
        if not idea:
            summary["extracted_dropped"] += 1
            continue
        ok, reason = is_valid_idea(idea)
        if not ok:
            summary["extracted_dropped"] += 1
            summary["errors"].append({"tweet_id": tid, "error": f"validation: {reason}"})
            continue

        summary["extracted_valid"] += 1
        if not dry_run:
            upsert_idea(tweet_id=tid, posted_at=ts, author_handle=author,
                         raw_text=text, cls=cls, idea=idea, llm_model=model)
            seen.add(tid)
        summary["ideas"].append({
            "tweet_id": tid, "author": author, "ticker": idea.ticker,
            "direction": idea.direction, "entry": idea.entry_text,
            "stop": idea.stop_text, "target": idea.target_text,
            "horizon": idea.horizon, "confidence": idea.extract_confidence,
        })

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
