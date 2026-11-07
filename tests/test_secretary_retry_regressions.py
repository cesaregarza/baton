"""Regression checks for transport retries, content failures, and durable recovery."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from secretary_support import archived_summary_count, conversation, make_world, say
from test_secretary_model import KILL_TIMEOUT, PAST_KILL
from test_secretary_recovery import _backlog, _step, _worker

from baton.agents.secretary.attention.decide import SecretaryUnavailable
from baton.agents.secretary.inbox.references import ReferenceWindow
from baton.agents.secretary.inbox.window import InboxTooLarge
from baton.config.secretary import load_policy


def _policy(world, **changes: object) -> None:
    value = json.loads(world.policy_path.read_text())
    value.update(changes)
    world.policy_path.write_text(json.dumps(value))


def _windows(world) -> list[tuple[int, int, str, int, int, str]]:
    backlog = _backlog(world)
    with backlog.log.connection() as conn:
        rows = conn.execute(
            "SELECT since,through,mode,attempts,content_failures,status FROM backlog_windows "
            "WHERE principal='planner' ORDER BY since,through"
        ).fetchall()
    return [tuple(row) for row in rows]


def test_six_message_page_retries_transport_failures_without_splitting_or_noticing(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{"exit": 1}, PAST_KILL, {"exit": 1}, {}]},
        )
        model.set_timeout(KILL_TIMEOUT)
        _policy(world, max_attempts=2, max_events=10, retry_initial_seconds=1, retry_max_seconds=60)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        sent = [say(world, room, "worker", f"Message {number} for the same alert page") for number in range(6)]
        event_ids = {f"message:{message['message_id']}" for message in sent}

        asyncio.run(_step(worker))
        assert len(model.requests) == 1
        assert len(_windows(world)) == 1
        assert _windows(world)[0][2:] == ("alert", 1, 0, "retry")
        assert archived_summary_count(world.baton) == 0

        for _ in range(2):
            now[0] += 60
            asyncio.run(_step(worker))
            assert len(_windows(world)) == 1
            assert _windows(world)[0][2] == "alert"
            assert _windows(world)[0][4] == 0
            assert archived_summary_count(world.baton) == 0

        now[0] += 60  # 180 seconds of fake wall time across recoverable transport failures.
        asyncio.run(_step(worker))
        assert len(model.requests) == 4
        assert len(_windows(world)) == 1
        assert _windows(world)[0][2:] == ("alert", 0, 0, "ready")
        [decision_id] = worker.instance.receipts.get("pending")
        result = asyncio.run(worker.instance.secretary.pending(decision_id))
        assert result is not None and result["kind"] == "summary"
        assert result["event_count"] == 6
        assert set(result["sources"]) == event_ids
        assert archived_summary_count(world.baton) == 1


@pytest.mark.parametrize("with_trigger", [False, True], ids=["untriggered", "same-trigger"])
def test_standalone_decide_retries_failed_page_after_backoff(tmp_path, monkeypatch, with_trigger) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"exit": 1}, {}]})
        model.set_timeout(KILL_TIMEOUT)
        _policy(world, retry_initial_seconds=1, retry_max_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        sent = say(world, room, "worker", "Retry this standalone page")
        trigger = f"message:{sent['seqs']['planner']}" if with_trigger else None

        with pytest.raises(SecretaryUnavailable):
            asyncio.run(secretary.decide(budget_seconds=5, trigger=trigger))
        assert len(model.requests) == 1
        assert len(_windows(world)) == 1 and _windows(world)[0][-1] == "retry"

        now[0] += 2
        result = asyncio.run(secretary.decide(budget_seconds=5, trigger=trigger))
        assert result["decision"] == "wake" and result["kind"] == "summary"
        assert result["event_count"] == 1
        assert f"message:{sent['message_id']}" in result["sources"]
        assert len(model.requests) == 2


def test_standalone_oversized_first_event_recovers_after_context_bound_increases(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path)
        model.set_timeout(KILL_TIMEOUT)
        _policy(world, context_bytes=1024, retry_initial_seconds=1, retry_max_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        secretary = worker.instance.secretary
        asyncio.run(secretary.activate())
        sent = say(world, room, "worker", "A sufficiently large event " + "x" * 1400)

        with pytest.raises(InboxTooLarge):
            asyncio.run(secretary.decide(budget_seconds=5))
        assert model.requests == []
        assert len(_windows(world)) == 1 and _windows(world)[0][-1] == "retry"

        _policy(world, context_bytes=100_000)
        now[0] += 2
        result = asyncio.run(secretary.decide(budget_seconds=5))
        assert result["kind"] == "summary" and result["event_count"] == 1
        assert f"message:{sent['message_id']}" in result["sources"]
        assert len(model.requests) == 1


def test_invalid_json_content_still_splits_after_content_failure_limit(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"raw": "not json"}]})
        model.set_timeout(KILL_TIMEOUT)
        _policy(world, max_attempts=2, max_events=10, retry_initial_seconds=1, retry_max_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        sent = [say(world, room, "worker", f"Invalid-response split source {number}") for number in range(2)]
        expected_sources = {f"message:{message['message_id']}" for message in sent}

        asyncio.run(_step(worker))
        [first_page] = _windows(world)
        assert first_page[2:5] == ("alert", 1, 1)
        assert len(model.requests) == 1

        now[0] += 2
        asyncio.run(_step(worker))
        pages = _windows(world)
        assert len(pages) == 2
        assert all(page[2] == "alert" and page[4] == 0 and page[5] == "retry" for page in pages)
        policy = load_policy(world.config, world.policy_path)
        split_sources = [
            {ref["source_key"] for ref in ReferenceWindow.read(world.config, policy, page[0], page[1]).refs}
            for page in pages
        ]
        assert all(len(sources) == 1 for sources in split_sources)
        assert set.union(*split_sources) == expected_sources
        assert len(model.requests) == 2
        assert archived_summary_count(world.baton) == 0


def test_digest_failure_notice_preserves_mode_and_prior_hold(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={"steps": [{"decision": "hold", "summary": "", "sources": []}, {"raw": "not json"}]},
        )
        model.set_timeout(KILL_TIMEOUT)
        _policy(
            world,
            max_attempts=2,
            retry_initial_seconds=1,
            retry_max_seconds=1,
            digest_seconds=1,
        )
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "This event is held for its digest")

        asyncio.run(_step(worker))
        assert model.requests[0]["context"]["mode"] == "alert"
        assert _windows(world)[0][2:] == ("digest", 0, 0, "held")
        assert archived_summary_count(world.baton) == 0

        now[0] += 2
        asyncio.run(_step(worker))
        now[0] += 2
        asyncio.run(_step(worker))
        assert [request["context"]["mode"] for request in model.requests] == ["alert", "digest", "digest"]
        [decision_id] = worker.instance.receipts.get("pending")
        result = asyncio.run(worker.instance.secretary.pending(decision_id))
        assert result is not None and result["kind"] == "failure_notice"
        assert result["mode"] == "digest" and result["decision"] == "keep"
        assert "held" in result["brief"].lower()
        assert "No relevance judgment" not in result["brief"]
        assert archived_summary_count(world.baton) == 1


def test_terminal_notice_persist_crash_recovers_idempotently_without_model_call(tmp_path, monkeypatch) -> None:
    from baton.agents.secretary.attention import queue as queue_module
    from baton.integrations.secretary.worker import Worker

    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(world, tmp_path, plan={"steps": [{"raw": "not json"}]})
        model.set_timeout(KILL_TIMEOUT)
        _policy(world, max_attempts=1, retry_initial_seconds=1, retry_max_seconds=1)
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        say(world, room, "worker", "Persist its deterministic failure notice")

        persisted: list[dict] = []
        original_persist = queue_module.persist

        def persist_then_crash(*args, **kwargs):
            result = original_persist(*args, **kwargs)
            persisted.append(result)
            raise RuntimeError("synthetic crash before backlog finish")

        monkeypatch.setattr(queue_module, "persist", persist_then_crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            asyncio.run(_step(worker))
        assert len(persisted) == 1 and archived_summary_count(world.baton) == 1
        assert len(model.requests) == 1
        assert _windows(world)[0][2:5] == ("alert", 1, 1)

        monkeypatch.setattr(queue_module, "persist", original_persist)
        model.set_plan({})
        restarted = Worker(worker.instance)
        now[0] += 2
        asyncio.run(_step(restarted))
        assert len(model.requests) == 1
        assert archived_summary_count(world.baton) == 1
        [decision_id] = restarted.instance.receipts.get("pending")
        assert decision_id == persisted[0]["decision_id"]
        result = asyncio.run(restarted.instance.secretary.pending(decision_id))
        assert result is not None and result["kind"] == "failure_notice"
        assert result["decision_id"] == persisted[0]["decision_id"]
        assert _windows(world)[0][2:] == ("alert", 0, 0, "ready")


def test_poll_uses_one_full_budget_lane_then_switches_to_other_due_lane(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        worker, model = _worker(
            world,
            tmp_path,
            plan={
                "steps": [
                    {"decision": "hold", "summary": "", "sources": []},
                    {"exit": 1},
                    {"sleep": 2},
                    {},
                ]
            },
            budget_seconds=3,
        )
        _policy(
            world,
            max_events=1,
            max_attempts=3,
            retry_initial_seconds=1,
            retry_max_seconds=1,
            digest_seconds=1,
        )
        now = [100.0]
        monkeypatch.setattr("baton.agents.secretary.attention.queue.time.time", lambda: now[0])
        asyncio.run(worker.instance.secretary.activate())
        held_message = say(world, room, "worker", "Hold this first page for a digest")
        asyncio.run(_step(worker))
        assert model.requests[0]["context"]["mode"] == "alert"
        held_row = next(row for row in _windows(world) if row[5] == "held")
        assert held_row[2:5] == ("digest", 0, 0)

        retry_message = say(world, room, "reviewer", "This second page needs a retry")
        asyncio.run(_step(worker))
        retry_row = next(row for row in _windows(world) if row[5] == "retry")
        assert retry_row[2:5] == ("alert", 1, 0)
        assert len(model.requests) == 2

        now[0] += 1  # Both the held digest and failed alert are due.
        started = time.monotonic()
        asyncio.run(_step(worker))
        elapsed = time.monotonic() - started
        assert elapsed >= 1.5  # The retry used most of the three-second poll budget.
        assert len(model.requests) == 3
        assert model.requests[2]["context"]["mode"] == "alert"
        assert f"message:{retry_message['message_id']}" in {
            event["source_key"] for event in model.requests[2]["context"]["events"]
        }
        rows = _windows(world)
        still_held = next(row for row in rows if row[5] == "held")
        retried = next(row for row in rows if row[2] == "alert")
        assert still_held[:2] == held_row[:2]
        assert still_held[2:5] == ("digest", 0, 0)
        assert still_held[5] == "held"
        assert retried[5] == "ready" and retried[3:5] == (0, 0)
        assert f"message:{held_message['message_id']}" not in {
            event["source_key"] for event in model.requests[2]["context"]["events"]
        }

        asyncio.run(_step(worker))  # Worker alternates its scheduled-lane priority on each poll.
        assert len(model.requests) == 4
        assert model.requests[3]["context"]["mode"] == "digest"
        assert f"message:{held_message['message_id']}" in {
            event["source_key"] for event in model.requests[3]["context"]["events"]
        }
        assert f"message:{retry_message['message_id']}" not in {
            event["source_key"] for event in model.requests[3]["context"]["events"]
        }
        assert all(row[5] == "ready" for row in _windows(world))
