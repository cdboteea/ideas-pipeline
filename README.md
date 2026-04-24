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
| `poll_x_bookmarks.py` | `com.matias.ideas-x-bookmarks` | Daily 09:30 | X bookmarks via `bird` CLI |
| `poll_pdf_dropfolder.py` | `com.matias.ideas-pdf-dropfolder` | Every 30 min, 08-22 | PDFs landing in `~/Downloads/ToStage/` (text-extracted, file moved to `_staged/`) |
| `poll_telegram_queue.py` | `com.matias.ideas-telegram-queue` | Every 15 min | Drains `~/clawd/data/telegram-queue.jsonl` — spec: `~/clawd/docs/openclaw-telegram-bridge.md` |
| `ingest_obsidian_to_graphiti.py` | `com.matias.ideas-graphiti-ingest` | Nightly 03:00 | Promoted notes → graphiti KG (limit 25/run, mtime+size dedup) |

Every poller supports `--dry-run`, `--json`, and isolates per-item errors so one bad entry doesn't abort the batch.

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
├── poll_x_bookmarks.py
├── poll_pdf_dropfolder.py
├── poll_telegram_queue.py
├── ingest_obsidian_to_graphiti.py
└── launchagents/     # plist mirrors (source of truth is ~/Library/LaunchAgents)
tests/                # 37 behavioral tests
```

## Links

- Comprehensive guide (§12): `~/clawd/docs/guides/quant-trading-system-guide-2026-04-23.md`
- Runbook: `~/clawd/docs/runbook.md`
- Telegram queue spec: `~/clawd/docs/openclaw-telegram-bridge.md`
- Companion repo: `cdboteea/strategy-engine`
