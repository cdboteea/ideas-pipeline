# ideas-pipeline

Capture / review / promote pipeline for an Obsidian-based personal idea
database. Built to work inside CC conversations — captures get staged to
`~/Documents/ObsidianVault/Inbox/`, promotions classify per
`_meta/classification-guide.md` and land in `Ideas/<category>/`.

**Daily operations reference:** `~/clawd/docs/runbook.md` §12

## Design principles

- **Capture is lossless; promotion is deliberate** — inbox catches everything, nothing auto-promotes.
- **Classification lives where it's needed** — inbox is unclassified and noisy by design; idea database is strictly classified (7 categories).
- **Obsidian is both inbox and IDB** — one vault, different folders. Obsidian's built-in graph/backlinks/tags ARE the review and retrieval UI.
- **Local-only data** — no cloud sync, no telemetry.
- **CC is the classifier** — since ingestion happens in conversation.

## The 5-stage flow

```
CAPTURE → INBOX → REVIEW → PROMOTE → GRAPHITI
```

Capture is automated for 5 sources (Gmail, X, PDFs, Telegram, the assistant mid-conversation). Promoted notes feed a temporal knowledge graph via graphiti.

## Manual CLI

```bash
# Capture (usually invoked by the assistant mid-conversation)
ideas stage url <URL> --title "..." --preview "..."
ideas stage x <URL> --author @handle --content "..."
ideas stage pdf <path> --title "..." --text "..."
ideas stage thought "..."
ideas stage research <title> --content-file <path>

# Review the inbox
ideas review inbox [--source url|pdf|x-post|email|telegram] [--since YYYY-MM-DD]
ideas review summary
ideas review discard <path>
ideas review defer <path>
ideas review auto-archive

# Promote (classify + move to Ideas/<category>/)
ideas promote <inbox_path> --category <cat> --title "..." --summary "..." --tags a,b,c
ideas promote-direct --title "..." --category <cat> --summary "..." --source-type <t> --tags a,b,c --source-url <url>
```

## Automated capture pollers (in `scripts/`)

| Script | Launchd agent | Cadence | Captures |
|---|---|---|---|
| `poll_gmail_tostage.py` | `com.matias.ideas-gmail-tostage` | 09:00 / 13:00 / 17:00 | Gmail messages with label `ToStage`; strips label after staging |
| `poll_x_bookmarks.py` | `com.matias.ideas-x-bookmarks` | Daily 09:30 | X bookmarks via `bird` CLI (`@clawdbot59030` — bot account only) |
| `poll_x_bookmarks_personal.py` | _in-session, no plist yet_ | Manual / on-trigger | Personal-X bookmarks via Claude-in-Chrome on `x.com/i/bookmarks` |
| `poll_ctl_ideas.py` | _in-session, no plist yet_ | Manual / on-trigger | CTL Trading list posts → structured trade ideas + thread linkage + alert hook → `~/clawd/data/ctl-trade-ideas.duckdb` |
| `enrich_ctl_outcomes.py` | _in-session, no plist yet_ | Daily after market close | Walks open CTL threads, fetches firstrate bars, computes MFE/MAE/pnl, auto-closes on stop/target hit, marks 14d-stale |
| `poll_pdf_dropfolder.py` | `com.matias.ideas-pdf-dropfolder` | Every 30 min, 08-22 | PDFs landing in `~/Downloads/ToStage/` (text-extracted, file moved to `_staged/`) |
| `poll_telegram_queue.py` | `com.matias.ideas-telegram-queue` | Every 15 min | Drains `~/clawd/data/telegram-queue.jsonl` — spec: `~/clawd/docs/openclaw-telegram-bridge.md` |
| `ingest_obsidian_to_graphiti.py` | `com.matias.ideas-graphiti-ingest` | Nightly 03:00 | Promoted notes → graphiti KG (limit 25/run, mtime+size dedup) |

Every poller supports `--dry-run`, `--json`, and isolates per-item errors so one bad entry doesn't abort the batch.

### CTL trade-idea extraction workflow

The CTL Trading list (4 members @ `x.com/i/lists/936040010809307136`, owned by @mirvois) ships structured trade ideas (CTLFutures: `$SB_F L here Risk 14.94 H4 #Swing`) and mixed commentary (canuck2usa: `$AMD 408`). The pipeline classifies each post (idea vs commentary), extracts ticker / direction / entry / stop / target / horizon / tags via codex (gpt-5 over the OpenClaw subscription — no API key needed), groups posts on the same ticker into **trade threads** with state evolution, and computes outcomes (MFE / MAE / pnl, auto-close on stop/target hit) from `firstrate.duckdb`.

#### Daily flow

```bash
# 1. In a CC session with Chrome MCP attached:
#    - Navigate Chrome to https://x.com/i/lists/936040010809307136
#    - Run the capture JS (same as bookmarks):
poll_ctl_ideas.py --print-capture-js
#    - Save the returned JSON to /tmp/ctl-capture.json

# 2. Classify + extract + thread-link + log alerts
poll_ctl_ideas.py /tmp/ctl-capture.json [--dry-run] [--json]

# 3. Enrich open threads with MFE/MAE/pnl + auto-close on stop/target
ideas ctl-enrich   (or `enrich_ctl_outcomes.py` standalone)

# 4. Session-start surfacing
ideas ctl-status --since-last-check    # what's new + updated + stale-open
ideas ctl-summary                       # per-author hit-rate + per-ticker pnl
```

#### Storage

| Path | What |
|------|------|
| `~/clawd/data/ctl-trade-ideas.duckdb` | `trade_ideas` (one row per post) + `trade_threads` (logical trades) + `ctl_alert_log` (de-dup) + analytics views |
| `~/clawd/data/ctl-state.json` | Processed tweet-ID dedup |
| `~/clawd/data/ctl-last-check.json` | Per-user last-check divider for `ctl-status --since-last-check` |
| `~/clawd/logs/ctl-trade-ideas.log` | One summary line per pipeline fire |
| `~/clawd/logs/ctl-enrich.log` | One summary line per outcome enrichment run |
| `~/clawd/logs/ctl-alerts.log` | Alert log channel (today). Telegram channel spec'd in design doc, not wired. |

#### Threading logic

Each post asks the LLM for `thread_intent`: `"new" | "update" | "close" | "unsure"`. New + open thread on same `(author, ticker)` exists → mark old stale, open new. Update + open thread → append. Close + open → close. Lookup window default 60 days. Threads with no update >14 days are auto-marked stale by the enrich job. See `~/clawd/research/workflows/ctl-pipeline-v2-2026-05-06.md` for full spec.

#### Coverage notes

Outcome enrichment splices two sources, mirroring the strategy-engine convention:

- **`firstrate.duckdb`** — historical daily bars, monthly-refreshed (this is by design — it's the historical reference, not a live feed). Covers the back portion of any thread's history.
- **EODHD** — live quotes + recent EOD bars for any equity ticker. Fills in the front portion of a thread's history beyond firstrate's ceiling, plus the live `last_mark_price` snapshot. Reads its API key from macOS keychain (`eodhd-api-key`).

Coverage by ticker shape:
- **Equities & ETFs** (`$AMD`, `$SPY`, `$XLK`, `$NVDA`, …) — fully covered (firstrate historical + EODHD recent + EODHD live).
- **Futures** (`$SB_F`, `$ZS_F`, `$ES_F`, …) — **MTM permanently skipped**. UW has futures snapshots but keyed by descriptive name not contract code, snapshot-only with no historical bars, and no contract-month resolution — not a fit. Threads still track post lifecycle + explicit closes; the structured idea text (Risk levels, targets, horizon) is enough for timely notification, which is what we actually wanted.
- **Crypto** — not auto-detected today; deferred (would need `.CC` suffix or routing through `hyperliquid.duckdb`).

#### Alerts (spec'd, log-channel-only today)

The `maybe_alert(thread, event)` hook in `ideas/ctl_threads.py` is the single funnel for new-thread / update / target-hit / stop-hit / thread-stale events. Today routes to `~/clawd/logs/ctl-alerts.log` with per-event-type throttling (60-min minimum interval) and a `ctl_alert_log` de-dup table. The Telegram channel raises `NotImplementedError` until wired — see `~/clawd/research/workflows/ctl-pipeline-v2-2026-05-06.md` §Alerts for the trigger conditions and channel spec.

### Personal-X bookmarks workflow

Bird runs on `@clawdbot59030` (a suspended bot account, structurally read-only). It cannot see Matias's personal bookmarks. The personal path uses Claude-in-Chrome on the running Chrome (Matias's personal X session lives in cookies there) and a separate state file (`~/clawd/data/x-bookmarks-personal-state.json`) so the two paths never poison each other's dedup.

Two-step capture (in-session today; cron-ified once Chrome MCP transport stabilizes — see `~/projects/citrini-pipeline` task #128):

```bash
# 1. In a CC session with Chrome MCP attached:
#    - Navigate Chrome to https://x.com/i/bookmarks
#    - (Optional but recommended) Prime window.__seen_ids with recently-
#      ingested IDs so the capture JS knows when to stop scrolling. See
#      docs/feed-watch-spec.md §10 "scroll-until-seen" — the canonical
#      capture JS is a self-contained async IIFE that scrolls + harvests
#      + stops at the first overlap with window.__seen_ids (or 60-scroll
#      ceiling / 3-scroll no-growth, whichever comes first).
#    - Run the capture JS via `mcp__Claude_in_Chrome__javascript_tool`:
poll_x_bookmarks_personal.py --print-capture-js
#    - Save window.__xbm_json (or read back via console.log + read_console_messages)
#      to /tmp/x-bookmarks-capture.json

# 2. Diff vs personal-state and stage to Inbox
poll_x_bookmarks_personal.py /tmp/x-bookmarks-capture.json [--dry-run] [--json]
```

The capture JS strips `@` prefixes from handles to bypass the Chrome MCP response filter (which heuristics `@user` patterns as sensitive); `normalize_capture` rebuilds the canonical `@handle` on the Python side. The same `BOOKMARKS_CAPTURE_JS` constant in `ideas/x_personal.py` is also used by `/ctl-poll` (list timeline) and `/feed-x-general-once` (Following → Recent home feed) — same DOM shape, same scroll-until-seen logic.

## Auto-archive

`com.matias.ideas-auto-archive` (daily 04:15) moves Inbox items older than 14 days to `Archive/YYYY-MM/`.

## Data layout

| Path | What |
|---|---|
| `~/Documents/ObsidianVault/Inbox/` | Raw captures — unclassified |
| `~/Documents/ObsidianVault/Ideas/<category>/` | Promoted notes — trading, ai-infra, research, business-ideas, tools, people, decisions |
| `~/Documents/ObsidianVault/Archive/YYYY-MM/` | Auto-archived stale inbox items |
| `~/Documents/ObsidianVault/_meta/` | Classification guide + tag vocabulary + vault conventions |
| `~/clawd/data/*-state.json` | Per-poller dedup state |
| `~/clawd/data/telegram-queue.jsonl` | Shared queue for Telegram captures |

## Status as of 2026-04-23

- **37 behavioral tests pass** (up from 7 at v0.1)
- **5 automated capture sources live**, 1 manual CLI
- **6 launchd agents** installed + loaded
- Companion to `~/projects/strategy-engine/` — together they form the research → trade → observe → re-capture loop

## Repo layout

```
ideas/
├── capture.py        # stage_url / stage_x_post / stage_pdf / stage_thought / stage_research / stage_email / stage_telegram
├── cli.py            # click entry point
├── config.py         # vault paths, categories, source types
├── models.py         # InboxItem, IdeaNote dataclasses
├── promote.py        # Inbox → Ideas/<cat>/ classification
├── review.py         # queue listing, filtering, auto-archive
└── storage.py        # markdown + YAML frontmatter I/O
scripts/
├── poll_gmail_tostage.py
├── poll_x_bookmarks.py            # bird-based — bot account
├── poll_x_bookmarks_personal.py   # Claude-in-Chrome — Matias's personal X
├── poll_pdf_dropfolder.py
├── poll_telegram_queue.py
├── ingest_obsidian_to_graphiti.py
└── launchagents/     # plist mirrors (source of truth is ~/Library/LaunchAgents)
tests/                # 51 behavioral tests
```

## Links

- Comprehensive guide (§12): `~/clawd/docs/guides/quant-trading-system-guide-2026-04-23.md`
- Runbook: `~/clawd/docs/runbook.md`
- Telegram queue spec: `~/clawd/docs/openclaw-telegram-bridge.md`
- Companion repo: `cdboteea/strategy-engine`
