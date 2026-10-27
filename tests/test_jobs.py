from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from baton import inbox, jobs, messages
from baton.errors import (
    IdempotencyConflict,
    IllegalTransition,
    InvalidRequest,
    RequestTooOld,
    StaleRevision,
    UnknownJob,
)


def test_a_job_follows_the_configured_graph_and_tells_whoever_acts_next(host, conn) -> None:
    graph = host.config.jobs
    job = jobs.create(conn, graph, request_id="job-1", actor="planner", title="Summarise", body="Two lines.")
    assert (job["state"], job["created"]) == ("open", True)
    assert [e["kind"] for e in inbox.take(conn, "worker")["events"]] == ["job.created"]

    jobs.move(conn, graph, job["job_id"], "working", actor="worker")
    moved = jobs.move(conn, graph, job["job_id"], "review", actor="worker", note="ready for review")
    assert (moved["state"], moved["waiting_on"]) == ("review", "planner")
    [event] = inbox.take(conn, "planner")["events"]
    assert (event["summary"], event["data"]["from"], event["data"]["to"]) == ("ready for review", "working", "review")

    done = jobs.move(conn, graph, job["job_id"], "done", actor="planner")
    assert (done["state"], done["waiting_on"]) == ("done", None)
    # A terminal state tells the job's creator.
    assert [e["data"]["to"] for e in inbox.take(conn, "planner")["events"]] == ["done"]
    history = jobs.read_events(conn, job["job_id"])
    assert [e["seq"] for e in history["events"]] == [1, 2, 3, 4]


def test_moves_off_the_graph_are_refused(host, conn) -> None:
    job = jobs.create(conn, host.config.jobs, request_id="job-2", actor="planner", title="t")
    with pytest.raises(IllegalTransition):
        jobs.move(conn, host.config.jobs, job["job_id"], "done", actor="worker")
    jobs.move(conn, host.config.jobs, job["job_id"], "cancelled", actor="planner")
    with pytest.raises(IllegalTransition):
        jobs.move(conn, host.config.jobs, job["job_id"], "working", actor="worker")
    with pytest.raises(UnknownJob):
        jobs.move(conn, host.config.jobs, "job_missing", "working", actor="worker")


def test_create_is_idempotent_per_request_id(host, conn) -> None:
    first = jobs.create(conn, host.config.jobs, request_id="same", actor="planner", title="t")
    again = jobs.create(conn, host.config.jobs, request_id="same", actor="planner", title="t")
    assert (again["job_id"], again["created"]) == (first["job_id"], False)
    with pytest.raises(IdempotencyConflict):
        jobs.create(conn, host.config.jobs, request_id="same", actor="planner", title="other")
    with pytest.raises(InvalidRequest):
        jobs.create(conn, host.config.jobs, request_id="x", actor="planner", title="")


def test_list_filters_and_messages_can_name_a_job(host, conn) -> None:
    graph = host.config.jobs
    first = jobs.create(conn, graph, request_id="a", actor="planner", title="first")
    jobs.create(conn, graph, request_id="b", actor="planner", title="second")
    jobs.move(conn, graph, first["job_id"], "working", actor="worker")
    assert [j["title"] for j in jobs.list_jobs(conn, state="working")] == ["first"]
    assert "body" not in jobs.list_jobs(conn)[0]

    messages.send(conn, sender="planner", recipient="worker", text="about it", job_id=first["job_id"])
    assert messages.read(conn, "worker")["messages"][0]["job_id"] == first["job_id"]
    assert jobs.read_events(conn, first["job_id"])["events"][-1]["kind"] == "message"
    with pytest.raises(UnknownJob):
        messages.send(conn, sender="planner", recipient="worker", text="x", job_id="job_missing")


def test_moves_carry_a_revision_and_a_retry_identity(host, conn) -> None:
    graph = host.config.jobs
    job = jobs.create(conn, graph, request_id="rev", actor="planner", title="t")
    assert job["revision"] == 1
    request_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-a"
    moved = jobs.move(conn, graph, job["job_id"], "working", actor="worker", request_id=request_id, expected_revision=1)
    assert (moved["revision"], moved["created"]) == (2, True)
    events_before = len(jobs.read_events(conn, job["job_id"])["events"])
    again = jobs.move(conn, graph, job["job_id"], "working", actor="worker", request_id=moved["request_id"],
                      expected_revision=1)
    assert (again["revision"], again["created"]) == (2, False)
    assert len(jobs.read_events(conn, job["job_id"])["events"]) == events_before
    with pytest.raises(StaleRevision):
        jobs.move(conn, graph, job["job_id"], "review", actor="worker", expected_revision=1)
    with pytest.raises(RequestTooOld):
        jobs.move(conn, graph, job["job_id"], "review", actor="worker",
                  request_id=f"{datetime.now(UTC) - timedelta(days=31):%Y%m%dT%H%M%SZ}-old")
    assert jobs.get(conn, job["job_id"])["revision"] == 2
