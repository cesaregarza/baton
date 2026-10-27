"""move_job over MCP: a retry replays its original outcome, and a stale revision is refused."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import anyio
from test_integration import client, payload


def rid(suffix: str) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{suffix}"


def run(host, steps) -> list[Any]:
    """Run `steps(planner, worker)`, an async function returning results, over two real MCP clients."""

    async def go() -> list[Any]:
        async with client(host, "planner") as planner, client(host, "worker") as worker:
            return await steps(planner, worker)

    return anyio.run(go)


async def call(session, tool: str, **arguments: Any) -> Any:
    return payload(await session.call_tool(tool, arguments))


async def working_job(planner, worker) -> str:
    job = await call(planner, "create_job", title="Example job")
    await call(worker, "move_job", job_id=job["job_id"], to_state="working")
    return job["job_id"]


def test_a_delayed_retry_replays_the_original_move_instead_of_moving_again(start_host) -> None:
    host = start_host()
    request_id = rid("to-review")

    async def steps(planner, worker) -> list[Any]:
        job_id = await working_job(planner, worker)
        first = await call(worker, "move_job", job_id=job_id, to_state="review", request_id=request_id)
        await call(planner, "move_job", job_id=job_id, to_state="working", note="needs another pass")
        before = await call(planner, "read_job_events", job_id=job_id)
        retry = await call(worker, "move_job", job_id=job_id, to_state="review", request_id=request_id)
        after = await call(planner, "read_job_events", job_id=job_id)
        return [first, retry, await call(planner, "get_job", job_id=job_id), before, after]

    first, retry, job, before, after = run(host, steps)
    assert (first.get("created"), first.get("request_id"), first.get("state")) == (True, request_id, "review")
    assert retry.get("created") is False
    assert {key: retry.get(key) for key in ("state", "revision", "request_id")} == \
        {key: first.get(key) for key in ("state", "revision", "request_id")}
    assert job["state"] == "working"
    assert after["events"] == before["events"]


def test_the_same_request_id_with_a_different_move_is_a_conflict(start_host) -> None:
    host = start_host()
    request_id = rid("conflict")

    async def steps(planner, worker) -> list[Any]:
        job_id = await working_job(planner, worker)
        await call(worker, "move_job", job_id=job_id, to_state="review", request_id=request_id)
        conflict = await call(worker, "move_job", job_id=job_id, to_state="done", request_id=request_id)
        return [conflict, await call(planner, "get_job", job_id=job_id)]

    conflict, job = run(host, steps)
    assert conflict.get("error") == "idempotency_conflict"
    assert job["state"] == "review"


def test_a_stale_revision_is_refused_even_after_the_job_returns_to_the_same_state(start_host) -> None:
    host = start_host()

    async def steps(planner, worker) -> list[Any]:
        job_id = await working_job(planner, worker)
        seen = await call(worker, "get_job", job_id=job_id)
        await call(worker, "move_job", job_id=job_id, to_state="review")
        await call(planner, "move_job", job_id=job_id, to_state="working")  # back where it was: A -> B -> A
        before = await call(planner, "read_job_events", job_id=job_id)
        stale = await call(worker, "move_job", job_id=job_id, to_state="review",
                           expected_revision=seen.get("revision", 0))
        after = await call(planner, "read_job_events", job_id=job_id)
        return [seen, stale, await call(planner, "get_job", job_id=job_id), before, after]

    seen, stale, job, before, after = run(host, steps)
    assert stale.get("error") == "stale_revision"
    assert job["state"] == "working"
    assert job.get("revision") == seen.get("revision", 0) + 2
    assert after["events"] == before["events"]


def test_a_matching_revision_or_no_guard_at_all_moves_the_job(start_host) -> None:
    host = start_host()

    async def steps(planner, worker) -> list[Any]:
        job_id = await working_job(planner, worker)
        seen = await call(worker, "get_job", job_id=job_id)
        await call(worker, "send_message", to="planner", text="about the job", job_id=job_id)
        guarded = await call(worker, "move_job", job_id=job_id, to_state="review",
                             expected_revision=seen.get("revision", 0))
        unguarded = await call(planner, "move_job", job_id=job_id, to_state="done")
        return [seen, guarded, unguarded]

    seen, guarded, unguarded = run(host, steps)
    assert isinstance(seen.get("revision"), int)
    # A message about the job is not a state change, so the revision the worker read still matches.
    assert (guarded.get("state"), guarded.get("revision"), guarded.get("created")) == \
        ("review", seen["revision"] + 1, True)
    assert unguarded.get("state") == "done"
    assert unguarded.get("revision") == seen["revision"] + 2
    assert str(unguarded.get("request_id", "")).count("-") >= 1  # the host made one and returned it
