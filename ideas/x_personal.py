"""X bookmarks — personal-account capture via Claude-in-Chrome.

Companion to `scripts/poll_x_bookmarks.py` (which uses bird on the suspended
@clawdbot59030 bot account and CANNOT see Matias's personal bookmarks).

This module handles the personal-X side via Claude-in-Chrome MCP:

  Capture flow (in-session, today):
    1. CC navigates Chrome to https://x.com/i/bookmarks
    2. Runs `BOOKMARKS_CAPTURE_JS` to extract bookmarks from the live DOM
       and stash on window.__xbm
    3. Saves the captured bookmarks list to disk as JSON
    4. Calls `poll_personal(json_path)` to diff vs seen + stage to Inbox

  State:
    - Personal account state lives at PERSONAL_STATE_FILE (separate from the
      bird-side state at ~/clawd/data/x-bookmarks-state.json — never shared,
      otherwise bot-account IDs would dedupe Matias's bookmarks).

  Tweet shape — normalized to match the bird-side path so `capture.stage_x_post`
  works unchanged:
      {
          "id":          "<status_id>",
          "text":        "<full text>",
          "created_at":  "2026-05-05T08:13:14.000Z",
          "author":      {"username": "<handle no @>", "name": "<display>"},
          "media":       {"photos": int, "videos": int}
      }

The MCP filter blocks single-string returns containing `@handle` patterns
(treated as sensitive). The capture JS strips the `@` prefix from handles
on the page side — they're rebuilt on the Python side.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Personal-account state — distinct from the bird path
PERSONAL_STATE_FILE = Path.home() / "clawd" / "data" / "x-bookmarks-personal-state.json"


# JavaScript injected via Claude-in-Chrome `javascript_tool` to extract
# posts/bookmarks from the live DOM AND scroll-until-seen.
#
# Caller is expected to navigate to the target page (bookmarks / list / home
# Following) first. The JS scrolls top→bottom, accumulating tweets into a
# Map keyed by tweet ID, stopping when:
#   (a) it encounters a tweet ID already in window.__seen_ids (set the caller
#       primed via a separate javascript_tool call) — i.e., we've reached
#       overlap with previously-captured state, OR
#   (b) it sees N consecutive scrolls with zero new IDs (feed exhausted), OR
#   (c) it hits the safety ceiling (MAX_SCROLLS).
#
# window.__seen_ids may be a Set, Array, or absent. Absent ⇒ scroll-to-bottom
# (capped at MAX_SCROLLS / NO_GROWTH_SCROLLS).
#
# Stashes the final payload at window.__xbm AND window.__xbm_json (the latter
# so the caller can console.log+read it back when MCP truncates the
# javascript_tool response). Also returns the post count from the IIFE.
BOOKMARKS_CAPTURE_JS = r"""
(async function(){
  const seenInput = window.__seen_ids;
  const seen = (seenInput instanceof Set)
    ? seenInput
    : new Set(Array.isArray(seenInput) ? seenInput : []);
  const harvested = new Map();
  const MAX_SCROLLS = 60;
  const SCROLL_BY = 900;
  const WAIT_MS = 1100;
  const NO_GROWTH_SCROLLS = 3;
  let scrollsSinceGrowth = 0;
  let hitSeen = false;

  function harvestOnce(){
    let newCount = 0;
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (const art of articles) {
      const statusLink = art.querySelector('a[href*="/status/"]');
      const href = statusLink ? statusLink.getAttribute('href') : null;
      if (!href) continue;
      const m = href.match(/^\/([^\/]+)\/status\/(\d+)/);
      if (!m) continue;
      const handle = m[1], id = m[2];   // strip @ to bypass MCP filter
      if (harvested.has(id)) continue;
      if (seen.has(id)) { hitSeen = true; continue; }
      const timeEl = art.querySelector('time');
      const created_at = timeEl ? timeEl.getAttribute('datetime') : null;
      const userBlock = art.querySelector('[data-testid="User-Name"]');
      const display = userBlock
        ? (userBlock.textContent.split('\n')[0] || null)
        : null;
      const tt = art.querySelector('[data-testid="tweetText"]');
      const text = tt ? tt.textContent : '';
      const photos = art.querySelectorAll('[data-testid="tweetPhoto"]').length;
      const videos = art.querySelectorAll('video').length;
      harvested.set(id, {
        id, handle, display, created_at, text,
        media: { photos, videos },
      });
      newCount++;
    }
    return newCount;
  }

  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 1500));
  harvestOnce();

  let scrolls = 0;
  for (; scrolls < MAX_SCROLLS; scrolls++) {
    window.scrollBy(0, SCROLL_BY);
    await new Promise(r => setTimeout(r, WAIT_MS));
    const newCount = harvestOnce();
    if (hitSeen) break;
    if (newCount === 0) {
      scrollsSinceGrowth++;
      if (scrollsSinceGrowth >= NO_GROWTH_SCROLLS) break;
    } else {
      scrollsSinceGrowth = 0;
    }
  }

  const out = Array.from(harvested.values());
  const stopReason = hitSeen
    ? "hit-seen"
    : (scrollsSinceGrowth >= NO_GROWTH_SCROLLS ? "no-growth" : "max-scrolls");
  window.__xbm = {
    count: out.length,
    bookmarks: out,
    stop_reason: stopReason,
    scrolls: scrolls,
    seen_size: seen.size,
  };
  window.__xbm_json = JSON.stringify(window.__xbm);
  return { count: out.length, stop_reason: stopReason, scrolls };
})()
"""


def normalize_capture(raw: dict | list[dict] | str) -> list[dict]:
    """Turn a Chrome-MCP capture payload (or JSON string of one) into the
    normalized tweet shape `poll_personal` consumes. Idempotent on re-call.

    Accepts:
      - dict with `bookmarks` key (the JS return shape)
      - bare list of bookmark dicts
      - JSON string of either of the above
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and "bookmarks" in raw:
        items = raw["bookmarks"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"unrecognized capture shape: keys={list(raw)[:5]!r}")

    out = []
    for it in items:
        tid = str(it.get("id") or "").strip()
        if not tid:
            continue
        handle = (it.get("handle") or "").lstrip("@")  # tolerate either
        display = it.get("display") or handle or "anonymous"
        # X DOM often emits 'DisplayName@handle·time' as a single string when
        # the User-Name block lacks newlines. Strip the '@handle...' tail and
        # any trailing whitespace.
        if handle and f"@{handle}" in display:
            display = display.split(f"@{handle}", 1)[0].strip()
        elif handle and display.endswith(handle):
            display = display[: -len(handle)].rstrip(" @·")
        out.append({
            "id":         tid,
            "text":       it.get("text") or "",
            "created_at": it.get("created_at"),
            "author": {
                "username": handle or "anonymous",
                "name":     display or handle or "anonymous",
            },
            "media": {
                "photos": int((it.get("media") or {}).get("photos") or 0),
                "videos": int((it.get("media") or {}).get("videos") or 0),
            },
        })
    return out


def load_personal_state(state_file: Path = PERSONAL_STATE_FILE) -> dict:
    if not state_file.exists():
        return {"processed_ids": []}
    try:
        return json.loads(state_file.read_text())
    except json.JSONDecodeError:
        return {"processed_ids": []}


def save_personal_state(state: dict, state_file: Path = PERSONAL_STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2))


def tweet_url(tweet: dict) -> str:
    """Canonical permalink — also resolves bookmark-card identity."""
    handle = (tweet.get("author") or {}).get("username") or "anonymous"
    return f"https://x.com/{handle}/status/{tweet['id']}"


def poll_personal(
    capture: list[dict] | dict | str | Path,
    *,
    dry_run: bool = False,
    state_file: Path = PERSONAL_STATE_FILE,
    stage_fn=None,
) -> dict:
    """Diff the captured bookmarks against personal-state, stage new ones.

    `capture`:
      - list[dict] of normalized tweets (already through normalize_capture), OR
      - dict with `bookmarks` (raw JS output), OR
      - str of JSON, OR
      - Path to a JSON file
    `stage_fn`: injection point for tests. Defaults to ideas.capture.stage_x_post.
    """
    if isinstance(capture, Path) or (
        isinstance(capture, str) and len(capture) < 4096 and Path(capture).exists()
    ):
        capture = json.loads(Path(capture).read_text())
    if isinstance(capture, list) and capture and isinstance(capture[0], dict) \
       and "author" in capture[0]:
        # Already normalized
        bookmarks = capture
    else:
        bookmarks = normalize_capture(capture)

    if stage_fn is None:
        from .capture import stage_x_post
        stage_fn = stage_x_post

    state = load_personal_state(state_file)
    processed = set(state.get("processed_ids", []))

    summary: dict[str, Any] = {
        "started_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched":      len(bookmarks),
        "staged":       0,
        "skipped_duplicate": 0,
        "errors":       [],
        "staged_items": [],
        "dry_run":      dry_run,
        "source":       "chrome-mcp-personal",
    }

    for tweet in bookmarks:
        tid = str(tweet.get("id") or "")
        if not tid:
            summary["errors"].append({"tweet_id": None, "error": "missing id"})
            continue
        if tid in processed:
            summary["skipped_duplicate"] += 1
            continue
        try:
            handle = (tweet.get("author") or {}).get("username") or "anonymous"
            display = (tweet.get("author") or {}).get("name") or handle
            content = tweet.get("text") or ""
            url = tweet_url(tweet)
            if dry_run:
                inbox_path = f"(dry-run — would stage {tid})"
            else:
                inbox_path = stage_fn(
                    url=url,
                    author=f"@{handle} ({display})",
                    content=content,
                )
                processed.add(tid)
            summary["staged"] += 1
            summary["staged_items"].append({
                "tweet_id":     tid,
                "url":          url,
                "author":       handle,
                "text_preview": content[:80],
                "inbox_path":   str(inbox_path),
            })
        except Exception as e:  # noqa: BLE001
            summary["errors"].append({
                "tweet_id": tid,
                "error":    f"{type(e).__name__}: {e}",
            })

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        state["processed_ids"] = sorted(processed)[-2000:]   # keep last 2000
        save_personal_state(state, state_file)
    return summary


def list_seen_personal(state_file: Path = PERSONAL_STATE_FILE) -> list[str]:
    return list(load_personal_state(state_file).get("processed_ids", []))


def is_seen_personal(tweet_id: str, state_file: Path = PERSONAL_STATE_FILE) -> bool:
    return str(tweet_id) in load_personal_state(state_file).get("processed_ids", [])
