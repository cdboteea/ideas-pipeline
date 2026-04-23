"""Vault paths + constants. Single source of truth."""
from __future__ import annotations
from pathlib import Path

VAULT = Path.home() / "Documents" / "ObsidianVault"
INBOX = VAULT / "Inbox"
IDEAS = VAULT / "Ideas"
ARCHIVE = VAULT / "Archive"
META = VAULT / "_meta"

# Valid categories — must match _meta/classification-guide.md §2
CATEGORIES = [
    "trading",
    "ai-infra",
    "research",
    "business-ideas",
    "tools",
    "people",
    "decisions",
]

# Valid source types — must match schema in _meta/README.md
SOURCE_TYPES = [
    "x-post",
    "url",
    "pdf",
    "email",
    "thought",
    "research-output",
    "cc-session",
    "telegram",
]

# Valid statuses
INBOX_STATUSES = ["pending", "deferred", "promoted", "discarded", "duplicate-merged"]
IDEA_STATUSES = ["active", "superseded", "archived"]

AUTO_ARCHIVE_DAYS = 14
LOCAL_TZ = "America/New_York"


def ensure_dirs() -> None:
    """Create vault subdirs if missing. Safe to call repeatedly."""
    for path in [INBOX, IDEAS, ARCHIVE, META]:
        path.mkdir(parents=True, exist_ok=True)
    for cat in CATEGORIES:
        (IDEAS / cat).mkdir(parents=True, exist_ok=True)
