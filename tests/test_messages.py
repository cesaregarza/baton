from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from baton import inbox, messages
from baton.errors import IdempotencyConflict, InvalidRequest, MailboxFull, RequestTooOld


def rid(suffix: str, at: datetime | None = None) -> str:
    return f"{(at or datetime.now(UTC)):%Y%m%dT%H%M%SZ}-{suffix}"


def test_each_recipient_gets_a_gap_free_sequence(conn) -> None:
    for text in ("one", "two"):
        messages.send(conn, sender="planner", recipient="worker", text=text)
    messages.send(conn, sender="worker", recipient="planner", text="reply")

    page = messages.read(conn, "worker")
    assert [(m["seq"], m["text"]) for m in page["messages"]] == [(1, "one"), (2, "two")]
    assert (page["next_seq"], page["unread"], page["truncated"]) == (2, 0, False)
    assert [m["seq"] for m in messages.read(conn, "planner")["messages"]] == [1]


def test_a_read_without_a_cursor_returns_only_undelivered_messages(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="first")
    messages.read(conn, "worker")
    messages.send(conn, sender="planner", recipient="worker", text="second")

    assert [m["text"] for m in messages.read(conn, "worker")["messages"]] == ["second"]
    assert [m["text"] for m in messages.read(conn, "worker", after_seq=0)["messages"]] == ["first", "second"]


def test_paging_by_limit_continues_from_next_seq(conn) -> None:
    for index in range(5):
        messages.send(conn, sender="planner", recipient="worker", text=f"m{index}")
    first = messages.read(conn, "worker", after_seq=0, limit=2)
    second = messages.read(conn, "worker", after_seq=first["next_seq"], limit=2)
    assert [m["text"] for m in first["messages"] + second["messages"]] == ["m0", "m1", "m2", "m3"]
    assert second["unread"] == 1


def test_a_retry_replays_and_a_changed_retry_conflicts(conn) -> None:
    request_id = rid("retry")
    first = messages.send(conn, sender="planner", recipient="worker", text="once", request_id=request_id)
    again = messages.send(conn, sender="planner", recipient="worker", text="once", request_id=request_id)
    assert (first["created"], again["created"], again["seq"]) == (True, False, first["seq"])
    with pytest.raises(IdempotencyConflict):
        messages.send(conn, sender="planner", recipient="worker", text="different", request_id=request_id)
    assert len(messages.read(conn, "worker")["messages"]) == 1


@pytest.mark.parametrize("request_id", ["no-time", "20261399T000000Z-x", rid("future", datetime.now(UTC) +
                                                                              timedelta(hours=1))])
def test_malformed_or_future_request_ids_are_refused(conn, request_id: str) -> None:
    with pytest.raises(InvalidRequest):
        messages.send(conn, sender="planner", recipient="worker", text="x", request_id=request_id)


def test_an_old_request_id_is_refused(conn) -> None:
    with pytest.raises(RequestTooOld):
        messages.send(conn, sender="planner", recipient="worker", text="x",
                      request_id=rid("old", datetime.now(UTC) - timedelta(days=31)))


def test_oversized_messages_are_refused(conn) -> None:
    with pytest.raises(InvalidRequest):
        messages.send(conn, sender="planner", recipient="worker", text="x" * (messages.MAX_MESSAGE_BYTES + 1))
    # Non-ASCII text is measured escaped, so it hits the cap sooner.
    with pytest.raises(InvalidRequest):
        messages.send(conn, sender="planner", recipient="worker", text="é" * (messages.MAX_MESSAGE_BYTES // 6 + 1))


def test_a_full_mailbox_pushes_back_until_read(conn, monkeypatch) -> None:
    monkeypatch.setattr(messages, "MAX_UNREAD", 2)
    messages.send(conn, sender="planner", recipient="worker", text="a")
    messages.send(conn, sender="planner", recipient="worker", text="b")
    with pytest.raises(MailboxFull):
        messages.send(conn, sender="planner", recipient="worker", text="c")
    messages.read(conn, "worker")
    assert messages.send(conn, sender="planner", recipient="worker", text="c")["seq"] == 3


def test_retention_deletes_old_read_messages_and_reports_truncation(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="old")
    messages.send(conn, sender="planner", recipient="worker", text="new")
    messages.read(conn, "worker", limit=1)
    stale = (datetime.now(UTC) - messages.READ_RETENTION - timedelta(days=1)).isoformat()
    conn.execute("UPDATE messages SET at = ? WHERE seq = 1", (stale,))
    conn.execute("UPDATE events SET at = ? WHERE seq = 1 AND job_id IS NULL", (stale,))

    page = messages.read(conn, "worker", after_seq=0)
    assert [m["text"] for m in page["messages"]] == ["new"]
    assert page["truncated"] is True
    # The event log never held the text, and the retired event is gone too.
    assert conn.execute("SELECT COUNT(*) FROM events WHERE data LIKE '%old%'").fetchone()[0] == 0


def test_wait_returns_messages_once_and_skips_ones_already_read(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="read elsewhere")
    messages.read(conn, "worker")
    messages.send(conn, sender="planner", recipient="worker", text="for wait")

    taken = inbox.wait(conn, "worker", timeout_seconds=0)
    assert [e["message"]["text"] for e in taken["events"]] == ["for wait"]
    assert inbox.wait(conn, "worker", timeout_seconds=0)["events"] == []
    assert messages.read(conn, "worker")["messages"] == []


def test_wait_times_out_empty(conn) -> None:
    assert inbox.wait(conn, "planner", timeout_seconds=0.1, poll_seconds=0.02) == {"events": [], "more": False}
