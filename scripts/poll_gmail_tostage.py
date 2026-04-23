#!/usr/bin/env python3
"""
Gmail 'ToStage' label poller → Obsidian Inbox.

Workflow:
  1. Search Gmail for messages with label `ToStage`
  2. For each, read subject + sender + body → stage to Inbox via capture.stage_email()
  3. Remove the `ToStage` label and add `Staged` so we don't re-process

Usage:
    poll_gmail_tostage.py [--max-messages 20] [--dry-run] [--json]

Env:
    GMAIL_TOSTAGE_LABEL   override the source label (default 'ToStage')
    GMAIL_STAGED_LABEL    override the destination label (default 'Staged')

The launchd agent `com.matias.ideas-gmail-tostage` (bi-hourly 09-17 weekdays)
runs this script. On first run the labels are auto-created if missing.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# Gmail API — the same venv that has gmail-manager.py deps
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.stderr.write(
        "ERROR: google-api-python-client not installed in this interpreter.\n"
        "Run with: ~/clawd/venv/bin/python scripts/poll_gmail_tostage.py\n"
    )
    sys.exit(1)

# Ensure we can import `ideas` even when invoked from anywhere
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ideas.capture import stage_email


WORKSPACE = Path.home() / "clawd"
CLIENT_FILE = WORKSPACE / "secrets" / "gdrive-oauth-client.json"
TOKEN_FILE = WORKSPACE / "secrets" / "gmail-oauth-token.json"

SCOPES = [
    "https://mail.google.com/",  # match gmail-manager.py — full access
]

SOURCE_LABEL = os.environ.get("GMAIL_TOSTAGE_LABEL", "ToStage")
DEST_LABEL = os.environ.get("GMAIL_STAGED_LABEL", "Staged")

# State: which message IDs we've already staged. Also redundant with the label
# move (Gmail side), but gives a fast local fence against duplicates if the
# label API temporarily refuses modifications.
STATE_FILE = Path.home() / "clawd" / "data" / "gmail-tostage-state.json"


# ── Gmail API helpers (mirror of gmail-manager.py patterns) ────────────────

def get_service():
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"Gmail OAuth token missing at {TOKEN_FILE}. "
            f"Run: ~/clawd/scripts/gmail-manager.py auth"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def ensure_label(service, name: str) -> str:
    """Return label_id for `name`, creating the label if it doesn't exist."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == name.lower():
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def list_messages_with_label(service, label_name: str, max_messages: int) -> list[str]:
    query = f"label:{label_name}"
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_messages,
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def get_header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def extract_body(payload) -> str:
    """Extract plain-text body. Prefers text/plain, falls back to stripped HTML."""
    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        if "parts" in part:
            for sp in part["parts"]:
                text = walk(sp)
                if text:
                    return text
        if mime == "text/html" and body.get("data"):
            html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
            # Super-naive HTML strip — good enough for plain-text-ish emails
            import re
            return re.sub(r"<[^>]+>", " ", html)
        return ""

    return walk(payload).strip()


def read_message(service, msg_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full",
    ).execute()
    headers = msg.get("payload", {}).get("headers", [])
    return {
        "id": msg_id,
        "thread_id": msg.get("threadId"),
        "subject": get_header(headers, "Subject"),
        "from": get_header(headers, "From"),
        "date": get_header(headers, "Date"),
        "body": extract_body(msg.get("payload", {})),
    }


def modify_labels(service, msg_id: str, add: list[str], remove: list[str]) -> None:
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": add, "removeLabelIds": remove},
    ).execute()


# ── State ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Core poll loop ───────────────────────────────────────────────────────

def poll(max_messages: int = 20, dry_run: bool = False) -> dict:
    service = get_service()
    source_id = ensure_label(service, SOURCE_LABEL)
    dest_id = ensure_label(service, DEST_LABEL)

    msg_ids = list_messages_with_label(service, SOURCE_LABEL, max_messages)
    state = load_state()
    processed = set(state.get("processed_ids", []))

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_label": SOURCE_LABEL,
        "dest_label": DEST_LABEL,
        "found": len(msg_ids),
        "staged": 0,
        "skipped_duplicate": 0,
        "errors": [],
        "staged_items": [],
        "dry_run": dry_run,
    }

    for mid in msg_ids:
        if mid in processed:
            summary["skipped_duplicate"] += 1
            continue
        try:
            msg = read_message(service, mid)
            sender_name, sender_addr = parseaddr(msg["from"])
            sender = sender_name or sender_addr or msg["from"]
            if dry_run:
                inbox_path = f"(dry-run — would stage {mid})"
            else:
                inbox_path = stage_email(
                    subject=msg["subject"],
                    sender=sender,
                    body=msg["body"],
                    message_id=mid,
                    received_at=msg["date"],
                )
                modify_labels(service, mid, add=[dest_id], remove=[source_id])
                processed.add(mid)
            summary["staged"] += 1
            summary["staged_items"].append({
                "message_id": mid,
                "subject": msg["subject"],
                "sender": sender,
                "inbox_path": inbox_path,
            })
        except Exception as e:  # noqa: BLE001 — log, keep going
            summary["errors"].append({"message_id": mid, "error": f"{type(e).__name__}: {e}"})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        state["processed_ids"] = sorted(processed)[-500:]  # keep last 500
        save_state(state)
    return summary


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Poll Gmail ToStage label → Obsidian Inbox")
    ap.add_argument("--max-messages", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true", help="don't stage or modify labels")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    try:
        summary = poll(max_messages=args.max_messages, dry_run=args.dry_run)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0

    print(f"Gmail ToStage poll  [{'dry-run' if args.dry_run else 'live'}]")
    print(f"  label:           {SOURCE_LABEL} → {DEST_LABEL}")
    print(f"  found:           {summary['found']}  "
          f"staged: {summary['staged']}  "
          f"skipped-dup: {summary['skipped_duplicate']}  "
          f"errors: {len(summary['errors'])}")
    for item in summary["staged_items"]:
        print(f"    ✓ {item['subject'][:60]}  ({item['sender']})")
        print(f"      → {item['inbox_path']}")
    for err in summary["errors"]:
        print(f"    ✗ {err['message_id']}: {err['error']}")
    return 0 if not summary["errors"] else (0 if summary["staged"] > 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())
