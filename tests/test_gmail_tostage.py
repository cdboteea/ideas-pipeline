"""Behavioral tests for the Gmail ToStage poller.

We don't hit Gmail for real — we inject a fake service with the same
interface shape as google-api-python-client's `build('gmail', 'v1', …)`.
"""
from __future__ import annotations
import base64
import json
import sys
from pathlib import Path

import pytest

# Make the poller importable (scripts/ isn't a package)
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _encode_body(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


class _FakeCall:
    """Wraps a dict as ``.execute()`` return — mimics googleapiclient chaining."""
    def __init__(self, payload): self._payload = payload
    def execute(self): return self._payload


class _FakeMessages:
    def __init__(self, msgs: dict[str, dict]):
        self.msgs = msgs
        self.modifications: list[dict] = []

    def list(self, **_):
        return _FakeCall({"messages": [{"id": k} for k in self.msgs.keys()]})

    def get(self, userId, id, format):
        return _FakeCall(self.msgs[id])

    def modify(self, userId, id, body):
        self.modifications.append({"id": id, **body})
        return _FakeCall({})


class _FakeLabels:
    def __init__(self):
        self._labels = [{"id": "Label_tostage", "name": "ToStage"},
                        {"id": "Label_staged", "name": "Staged"}]
    def list(self, **_):
        return _FakeCall({"labels": self._labels})
    def create(self, userId, body):
        new = {"id": f"Label_new_{body['name'].lower()}", "name": body["name"]}
        self._labels.append(new)
        return _FakeCall(new)


class _FakeUsers:
    def __init__(self, messages_obj, labels_obj):
        self._m = messages_obj; self._l = labels_obj
    def messages(self): return self._m
    def labels(self): return self._l


class _FakeService:
    def __init__(self, msgs):
        self.messages_obj = _FakeMessages(msgs)
        self.labels_obj = _FakeLabels()
    def users(self):
        return _FakeUsers(self.messages_obj, self.labels_obj)


def _mk_gmail_message(msg_id, subject, sender, body_text, date="Wed, 23 Apr 2026 09:00:00 -0400"):
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
            "mimeType": "text/plain",
            "body": {"data": _encode_body(body_text)},
        },
    }


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point the ideas package at a fresh ObsidianVault under tmp_path."""
    vault = tmp_path / "ObsidianVault"
    monkeypatch.setattr("ideas.config.VAULT", vault)
    monkeypatch.setattr("ideas.config.INBOX", vault / "Inbox")
    monkeypatch.setattr("ideas.config.IDEAS", vault / "Ideas")
    monkeypatch.setattr("ideas.config.ARCHIVE", vault / "Archive")
    monkeypatch.setattr("ideas.config.META", vault / "_meta")
    import ideas.storage as storage  # noqa: F401 — force import
    from ideas.config import ensure_dirs
    ensure_dirs()
    yield vault


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Isolate the state file per-test."""
    import poll_gmail_tostage as poller
    state = tmp_path / "state.json"
    monkeypatch.setattr(poller, "STATE_FILE", state)
    yield state


# ── Tests ──────────────────────────────────────────────────────────────────

def test_poll_stages_new_messages(isolated_vault, isolated_state, monkeypatch):
    import poll_gmail_tostage as poller

    service = _FakeService({
        "m1": _mk_gmail_message("m1", "Interesting article", "Alice <alice@x.com>",
                                "Body of the first mail"),
        "m2": _mk_gmail_message("m2", "Another idea",        "Bob <bob@y.com>",
                                "Body of second mail"),
    })
    monkeypatch.setattr(poller, "get_service", lambda: service)

    summary = poller.poll(max_messages=20, dry_run=False)

    assert summary["found"] == 2
    assert summary["staged"] == 2
    assert summary["skipped_duplicate"] == 0
    assert summary["errors"] == []
    # Both messages had ToStage removed + Staged added
    assert len(service.messages_obj.modifications) == 2
    for mod in service.messages_obj.modifications:
        assert "Label_tostage" in mod["removeLabelIds"]
        assert "Label_staged" in mod["addLabelIds"]
    # Inbox files created
    inbox = isolated_vault / "Inbox"
    assert len(list(inbox.glob("*.md"))) == 2


def test_poll_skips_already_processed_ids(isolated_vault, isolated_state, monkeypatch):
    import poll_gmail_tostage as poller

    # Pre-seed state so m1 is "already processed"
    isolated_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_state.write_text(json.dumps({"processed_ids": ["m1"]}))

    service = _FakeService({
        "m1": _mk_gmail_message("m1", "Dup", "A <a@x>", "body"),
        "m2": _mk_gmail_message("m2", "New", "B <b@y>", "body"),
    })
    monkeypatch.setattr(poller, "get_service", lambda: service)

    summary = poller.poll(max_messages=20, dry_run=False)
    assert summary["staged"] == 1
    assert summary["skipped_duplicate"] == 1
    # Only m2's label should have been modified
    assert [m["id"] for m in service.messages_obj.modifications] == ["m2"]


def test_poll_dry_run_does_not_stage_or_modify(isolated_vault, isolated_state, monkeypatch):
    import poll_gmail_tostage as poller

    service = _FakeService({
        "m1": _mk_gmail_message("m1", "Subject", "A <a@x>", "body"),
    })
    monkeypatch.setattr(poller, "get_service", lambda: service)

    summary = poller.poll(max_messages=10, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["staged"] == 1
    # No actual inbox file and no label mods
    assert list((isolated_vault / "Inbox").glob("*.md")) == []
    assert service.messages_obj.modifications == []


def test_poll_continues_after_per_message_error(isolated_vault, isolated_state, monkeypatch):
    import poll_gmail_tostage as poller

    service = _FakeService({
        "m1": _mk_gmail_message("m1", "Ok", "A <a@x>", "body"),
        "m2": _mk_gmail_message("m2", "Ok2", "B <b@y>", "body"),
    })
    monkeypatch.setattr(poller, "get_service", lambda: service)

    # Break stage_email for m1 only
    original = poller.stage_email
    calls = []
    def flaky_stage(**kwargs):
        calls.append(kwargs["message_id"])
        if kwargs["message_id"] == "m1":
            raise RuntimeError("boom")
        return original(**kwargs)

    monkeypatch.setattr(poller, "stage_email", flaky_stage)

    summary = poller.poll(max_messages=10, dry_run=False)
    assert summary["staged"] == 1
    assert len(summary["errors"]) == 1
    assert "m1" in summary["errors"][0]["message_id"]


def test_stage_email_writes_inbox_md(isolated_vault):
    from ideas.capture import stage_email
    path = stage_email(
        subject="Trading idea",
        sender="Alice <alice@x.com>",
        body="Here's the setup I'm thinking about.",
        message_id="m-test-1",
        received_at="Wed, 23 Apr 2026 09:00:00 -0400",
    )
    p = Path(path)
    assert p.exists()
    content = p.read_text()
    assert "source_type: email" in content
    assert "captured_by: gmail-tostage-poller" in content
    assert "gmail-tostage" in content  # poller tag propagates
    assert "Trading idea" in content or "trading-idea" in content
    assert "gmail://m-test-1" in content
