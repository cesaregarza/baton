"""Empirical paging and strict revalidation checks for secretary inbox windows."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest
from secretary_support import conversation, make_world, say

from baton import messages
from baton.agents.secretary.inbox.references import ReferenceWindow
from baton.agents.secretary.inbox.window import InboxTooLarge, InboxWindow
from baton.config.secretary import load_policy
from baton.database import Connection


def _send(world, text: str):
    return messages.send(cast(Connection, world.baton), sender="worker", recipient="planner", text=text)


def _policy(world, *, max_events: int = 1000, context_bytes: int = 1024 * 1024):
    return replace(load_policy(world.config, world.policy_path), max_events=max_events, context_bytes=context_bytes)


def _position(world, message_id: str) -> int:
    row = world.baton.execute(
        "SELECT pos FROM events WHERE json_extract(data, '$.message_id')=? ORDER BY pos LIMIT 1", (message_id,)
    ).fetchone()
    assert row is not None
    return row["pos"]


def test_default_reads_largest_ordered_prefix_and_pages_every_delivery_once(tmp_path) -> None:
    with make_world(tmp_path) as world:
        sent = [_send(world, f"Report {index}") for index in range(5)]
        policy = _policy(world, max_events=2)

        first = InboxWindow.read(world.config, policy, 0)
        second = InboxWindow.read(world.config, policy, first.through)
        third = InboxWindow.read(world.config, policy, second.through)

        observed = [event["message"]["id"] for page in (first, second, third) for event in page.events]
        assert observed == [message["message_id"] for message in sent]
        assert [len(page.events) for page in (first, second, third)] == [2, 2, 1]
        assert first.through < second.through < third.through


def test_reply_parent_is_included_and_counts_toward_page_limits(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        parent = say(world, room, "worker", "Original request")
        reply = say(world, room, "worker", "Following up", reply_to=parent["message_id"])
        policy = _policy(world, max_events=2)

        window = InboxWindow.read(world.config, policy, 0)

        assert [event["message"]["id"] for event in window.events] == [parent["message_id"], reply["message_id"]]
        assert window.reply_context == []


def test_first_event_with_required_parent_reports_its_position_when_it_cannot_fit(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        parent = say(world, room, "worker", "Original request")
        reply = say(world, room, "worker", "Following up", reply_to=parent["message_id"])

        with pytest.raises(InboxTooLarge) as raised:
            InboxWindow.read(world.config, _policy(world, max_events=1), _position(world, parent["message_id"]))

        assert raised.value.extra == {"through_pos": _position(world, reply["message_id"])}


def test_reply_parent_fits_as_context_when_page_starts_after_parent_event(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        parent = say(world, room, "worker", "Original request")
        reply = say(world, room, "worker", "Following up", reply_to=parent["message_id"])
        policy = _policy(world, max_events=2)

        window = InboxWindow.read(world.config, policy, _position(world, parent["message_id"]))

        assert [event["message"]["id"] for event in window.events] == [reply["message_id"]]
        assert [event["message"]["id"] for event in window.reply_context] == [parent["message_id"]]


def test_explicit_revalidation_range_refuses_overflow_without_truncating(tmp_path) -> None:
    with make_world(tmp_path) as world:
        sent = [_send(world, f"Report {i}") for i in range(2)]

        with pytest.raises(InboxTooLarge) as raised:
            InboxWindow.read(
                world.config, _policy(world, max_events=1), 0, through=_position(world, sent[-1]["message_id"])
            )

        assert raised.value.extra == {}


def test_head_returns_epoch_and_event_head_from_current_snapshot(tmp_path) -> None:
    with make_world(tmp_path) as world:
        sent = _send(world, "Report")

        epoch, head = InboxWindow.head(world.config)

        assert epoch
        assert head == _position(world, sent["message_id"])


def test_large_backlog_pages_to_citation_output_budget_without_losing_events(tmp_path) -> None:
    with make_world(tmp_path) as world:
        sent = [_send(world, f"Report {index}") for index in range(24)]
        policy = replace(_policy(world, max_events=100), model={"provider": "openai", "max_output_tokens": 256})

        pages = []
        since = 0
        while True:
            page = InboxWindow.read(world.config, policy, since)
            pages.append(page)
            if not page.events or page.through == _position(world, sent[-1]["message_id"]):
                break
            since = page.through

        observed = [event["message"]["id"] for page in pages for event in page.events]
        assert observed == [message["message_id"] for message in sent]
        assert len(pages[0].events) < len(sent)
        assert all(len(json.dumps(sorted(page.source_keys), ensure_ascii=False).encode()) <= 128 for page in pages)


def test_reference_window_contains_only_visible_identities_and_stable_digest(tmp_path) -> None:
    from baton import conversations

    with make_world(tmp_path) as world:
        visible = _send(world, "Visible authored body")
        room = conversation(world)
        hidden = say(world, room, "worker", "Removed membership body")
        conversations.remove(cast(Connection, world.baton), world.config, room, "planner", actor="planner")
        end = _position(world, hidden["message_id"])

        first = ReferenceWindow.read(world.config, _policy(world), 0, end)
        second = ReferenceWindow.read(world.config, _policy(world), 0, end)

        message_ids = [ref["message_id"] for ref in first.refs]
        assert visible["message_id"] in message_ids
        assert hidden["message_id"] not in message_ids
        assert all(set(ref) == {"source_key", "pos", "message_id", "conversation_id", "job_id"} for ref in first.refs)
        assert "Visible authored body" not in repr(first.refs)
        assert "Removed membership body" not in repr(first.refs)
        assert first.digest == second.digest


def test_split_position_divides_distinct_visible_events_at_a_cursor(tmp_path) -> None:
    with make_world(tmp_path) as world:
        sent = [_send(world, f"Report {index}") for index in range(2)]
        start, end = 0, _position(world, sent[-1]["message_id"])

        split = InboxWindow.split_position(world.config, _policy(world), start, end)
        assert split is not None
        before = ReferenceWindow.read(world.config, _policy(world), start, split)
        after = ReferenceWindow.read(world.config, _policy(world), split, end)

        assert split == _position(world, sent[0]["message_id"])
        assert len(before.refs) == len(after.refs) == 1
        assert InboxWindow.split_position(world.config, _policy(world), split, end) is None
