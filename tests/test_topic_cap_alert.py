"""The guest-wide cap alert, end to end offline: what is stored, what a read returns, what wake it emits.

The alert's whole point is that it reaches a person, and the only shape the delegated coordinator's
receiver accepts is an ordinary message wake. So this does not stop at "the envelope was accepted":
it fills the cap for real, then shows the stored host notice, the `read_messages` response that
returns it, and the wake payload the dispatcher actually builds for it.

Set BATON_EVIDENCE_DIR to have the run write those three artifacts out as JSON, so the fixtures a
receiver is checked against are produced by the same test that asserts them rather than by hand.
Nothing here touches a live host, a credential or any real mailbox.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_topic_containers import ALIAS, OTHER_ALIAS, refused, seed_aliases, topic_config

from baton import conversations, messages, topics, wakes
from baton.config import Config
from baton.database.store import connect

ALERT_ROLE = "orchestrator"


def filled(db: sqlite3.Connection, config: Config) -> None:
    """Three guest threads fill the guest-wide cap this config sets, so the next create is refused."""
    topics.create(db, config, actor=ALERT_ROLE, path="baton", members=[ALERT_ROLE, "reviewer"])
    for identity in (ALIAS, ALIAS, OTHER_ALIAS):
        conversations.create(db, config, actor="guest", title="A guest thread", participants=None,
                             topic="baton", identity=identity)


@pytest.fixture
def host(tmp_path: Path):
    config = topic_config(tmp_path)
    connection = connect(config.db_path)
    seed_aliases(connection, ALIAS, OTHER_ALIAS)
    yield config, connection
    connection.close()


def test_the_cap_alert_is_stored_read_and_woken_as_an_ordinary_message(host):
    config, db = host
    filled(db, config)
    refused("limit_exceeded", conversations.create, db, config, actor="guest", title="over",
            participants=None, topic="baton", identity=OTHER_ALIAS)

    # 1. The notice as stored: senderless, in no conversation, carrying the configured limit.
    row = db.execute("SELECT * FROM messages WHERE recipient = ? AND sender IS NULL"
                     " AND notice LIKE '%guest_threads.cap_reached%'", (ALERT_ROLE,)).fetchone()
    assert row is not None, "the refusal must have stored one host notice"
    stored = {key: row[key] for key in row.keys()}
    limit = config.aliases.max_threads_per_guest_per_hour
    assert json.loads(stored["notice"]) == {"event": "guest_threads.cap_reached", "limit": limit,
                                            "window": "1h"}
    assert str(limit) in stored["text"]
    assert stored["conversation_id"] is None
    assert stored["job_id"] is None
    assert stored["needs_decision"] == 0
    assert stored["relayed"] is None

    # 2. The read that returns it, exactly as the alert role's own helper would see it.
    page = messages.read(db, ALERT_ROLE)
    assert [message["message_id"] for message in page["messages"]] == [stored["id"]]
    delivered = page["messages"][0]
    assert delivered["sender"] is None
    assert delivered["notice"] == json.loads(stored["notice"])
    assert delivered["conversation_id"] is None

    # 3. The wake the dispatcher builds for it: reason=message, which is the shape a receiver accepts.
    second = connect(config.db_path)
    try:
        with second:
            second.execute("UPDATE mailboxes SET read_seq = 0 WHERE recipient = ?", (ALERT_ROLE,))
        # The body exactly as it is delivered. The shared test recorder adds the wake's own id to what
        # it records, and a receiver that matches the field set exactly would reject that extra key, so
        # the raw bytes are captured here instead.
        delivered_bodies: list[bytes] = []

        def deliver(wake: object, wake_id: str, body: bytes) -> bool:
            delivered_bodies.append(body)
            return True

        wakes.Dispatcher(config, deliver).pass_once()
        bodies = [json.loads(body) for body in delivered_bodies]
        woken = [body for body in bodies if body.get("role") == ALERT_ROLE]
        assert woken, "a stored host notice must produce a wake for its recipient"
        payload = woken[0]
        assert set(payload) == {"type", "role", "host", "reason", "first_unread_seq"}, \
            "exactly the fields a message wake carries, so an exact-match receiver accepts it"
        assert payload["type"] == "baton.wake"
        assert payload["reason"] == "message", "the only reason the receiver accepts"
        assert payload["first_unread_seq"] == delivered["seq"]
        assert "text" not in payload and "notice" not in payload, "a wake carries no content"
    finally:
        second.close()

    _write_evidence({"stored_notice": stored, "read_response": page, "wake_payload": payload})


def _write_evidence(artifacts: dict) -> None:
    """Write the three fixtures out when asked, so a receiver check runs against what this test asserted."""
    target = os.environ.get("BATON_EVIDENCE_DIR")
    if not target:
        return
    directory = Path(target)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, value in artifacts.items():
        (directory / f"cap-alert-{name.replace('_', '-')}.json").write_text(
            json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
