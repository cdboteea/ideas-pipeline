# ideas-pipeline

Capture / review / promote pipeline for an Obsidian-based personal idea
database. Built to work inside CC conversations — captures get staged to
`~/Documents/ObsidianVault/Inbox/`, promotions classify per
`_meta/classification-guide.md` and land in `Ideas/<category>/`.

## Design principles

- **Capture is lossless; promotion is deliberate** — inbox catches everything,
  nothing auto-promotes.
- **Classification lives where it's needed** — inbox is unclassified and noisy
  by design; idea database is strictly classified.
- **Obsidian is both inbox and IDB** — one vault, different folders. Obsidian's
  built-in graph/backlinks/tags ARE the review and retrieval UI.
- **Local-only data** — no cloud sync, no telemetry.
- **CC is the classifier** — since ingestion happens in conversation.

## Commands

```
ideas stage url <URL> --title "..." --preview "..."
ideas stage x <URL> --author @handle --content "..."
ideas stage pdf <path> --title "..." --text "..."
ideas stage thought "..."
ideas stage research <title> --content-file <path>

ideas review inbox [--source url|pdf|x-post] [--since YYYY-MM-DD]
ideas review summary
ideas review discard <path>
ideas review defer <path>
ideas review auto-archive

ideas promote <inbox_path> --category <cat> --title "..." --summary "..." --tags a,b,c
ideas promote-direct --title "..." --category <cat> --summary "..." --source-type <t> --tags a,b,c --source-url <url>
```

## Auto-archive

launchd job at `com.matias.ideas-auto-archive` runs daily 04:15 local; moves
inbox items older than 14 days to `Archive/YYYY-MM/`.

## Status

v0.1.0 — shipped 2026-04-22. 7/7 behavioral tests pass.
