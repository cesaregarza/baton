"""Baton stops deleting messages, summaries and mechanical records; credentials are a later change.

Each test names one thing that used to be swept and says it is still stored, and that what reads and retries
see has not changed. A test that asserts a refusal is a guard: it holds today and must go on holding once
the rows it used to depend on are kept.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_index_writer import GROUP, begin, cycle, inputs, read_all, refused, say, versions
from test_index_writer import commit as write_commit
from test_index_writer import converse as converse_in
from test_messages import rid
from test_vectors import UNIT, embedding_generation, items, payload, vectors_config
from test_vectors import begin as begin_in_generation
from test_vectors import commit as commit_in_generation
from test_vectors import fill as fill_generation

from baton import changes, conversations, derived, jobs, messages, search
from baton.errors import BatonError, RequestTooOld
from baton.store import connect

LONG_AGO = timedelta(days=40)  # past the old 30-day read retention and the 31-day retry-record retention


def ago(delta: timedelta = LONG_AGO) -> str:
    return (datetime.now(UTC) - delta).isoformat()


@pytest.fixture
def config(tmp_path: Path):
    return vectors_config(tmp_path, generation_retention_days=1)


@pytest.fixture
def db(config):
    connection = connect(config.db_path)
    yield connection
    connection.close()


def count(db: sqlite3.Connection, sql: str, *args: Any) -> int:
    return db.execute(sql, args).fetchone()[0]


def read_everywhere(db) -> None:
    for role in GROUP:
        messages.read(db, role)


def expire(db, *message_ids: str) -> None:
    """Reads every role, ages each message 40 days, then reads again: the second read is what used to expire it."""
    read_everywhere(db)
    for message_id in message_ids:
        db.execute("UPDATE messages SET at = ? WHERE id = ?", (ago(), message_id))
    read_everywhere(db)


# Messages ------------------------------------------------------------------------------------------------------


def test_an_old_read_direct_message_survives_a_later_read(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="old")
    messages.read(conn, "worker")
    conn.execute("UPDATE messages SET at = ?", (ago(),))

    messages.read(conn, "worker")  # the read that used to delete it

    assert count(conn, "SELECT COUNT(*) FROM messages WHERE text = 'old'") == 1
    page = messages.read(conn, "worker", after_seq=0)
    assert [m["text"] for m in page["messages"]] == ["old"]
    assert page["truncated"] is False


def test_an_old_read_thread_message_survives_a_later_read(db, config) -> None:
    conversation_id, sent = converse_in(db, config, 2)
    read_everywhere(db)
    db.execute("UPDATE messages SET at = ?", (ago(),))

    read_everywhere(db)  # the reads that used to delete it

    assert count(db, "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", conversation_id) == 4  # two copies each
    page = conversations.history(db, "assistant", conversation_id, config=config)
    assert [m["text"] for m in page["messages"]] == ["note 1", "note 2"]
    assert page["retention_gaps"] == []
    assert all(messages.read(db, role, after_seq=0)["truncated"] is False for role in ("reviewer", "assistant"))


def test_a_later_read_logs_no_expiry_for_a_message_it_keeps(db, config) -> None:
    # `message_expired` in the change log would tell a watcher a copy is gone when it is not.
    converse_in(db, config, 2)
    read_everywhere(db)
    db.execute("UPDATE messages SET at = ?", (ago(),))

    read_everywhere(db)

    assert count(db, "SELECT COUNT(*) FROM change_log WHERE kind = 'message_expired'") == 0


def test_the_wake_events_of_old_read_messages_are_kept(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="old")
    messages.read(conn, "worker")
    conn.execute("UPDATE messages SET at = ?", (ago(),))
    conn.execute("UPDATE events SET at = ? WHERE job_id IS NULL", (ago(),))

    messages.read(conn, "worker")

    assert count(conn, "SELECT COUNT(*) FROM events WHERE job_id IS NULL AND kind = 'message'") == 1


# Retry records ------------------------------------------------------------------------------------------------


def aged_request(conn, table: str) -> str:
    """Rewrite the one retry record in `table` as if it were made 40 days ago; returns its old request_id."""
    old_id = rid("aged", datetime.now(UTC) - LONG_AGO)
    conn.execute(f"UPDATE {table} SET request_id = ?, created_at = ?", (old_id, ago()))
    return old_id


def test_a_message_request_record_older_than_the_identity_retention_is_kept(conn) -> None:
    messages.send(conn, sender="planner", recipient="worker", text="once", request_id=rid("retry"))
    old_id = aged_request(conn, "message_requests")

    messages.read(conn, "worker")  # the read that used to delete it

    assert count(conn, "SELECT COUNT(*) FROM message_requests WHERE request_id = ?", old_id) == 1


def test_a_job_move_record_older_than_the_identity_retention_is_kept(host, conn) -> None:
    graph = host.config.jobs
    job = jobs.create(conn, graph, request_id="job-1", actor="planner", title="t")
    jobs.move(conn, graph, job["job_id"], "working", actor="worker", request_id=rid("move"))
    old_id = aged_request(conn, "job_moves")
    other = jobs.create(conn, graph, request_id="job-2", actor="planner", title="t2")

    jobs.move(conn, graph, other["job_id"], "working", actor="worker")  # the move that used to delete it

    assert count(conn, "SELECT COUNT(*) FROM job_moves WHERE request_id = ?", old_id) == 1


@pytest.mark.parametrize("record", ["message", "job_move"])
def test_guard_a_retry_with_an_old_request_id_is_still_refused(host, conn, record: str) -> None:
    # The refusal comes from the date in the request_id, never from the retry record being gone.
    graph = host.config.jobs
    job = jobs.create(conn, graph, request_id="job-1", actor="planner", title="t")
    if record == "message":
        messages.send(conn, sender="planner", recipient="worker", text="once", request_id=rid("retry"))
        old_id = aged_request(conn, "message_requests")
        retry = lambda: messages.send(conn, sender="planner", recipient="worker",  # noqa: E731
                                      text="once", request_id=old_id)
        messages.read(conn, "worker")  # the read that used to delete it
    else:
        jobs.move(conn, graph, job["job_id"], "working", actor="worker", request_id=rid("move"))
        old_id = aged_request(conn, "job_moves")
        retry = lambda: jobs.move(conn, graph, job["job_id"], "working", actor="worker",  # noqa: E731
                                  request_id=old_id)
        other = jobs.create(conn, graph, request_id="job-2", actor="planner", title="t2")
        jobs.move(conn, graph, other["job_id"], "working", actor="worker")  # the move that used to delete it

    with pytest.raises(RequestTooOld):
        retry()


# The change log ------------------------------------------------------------------------------------------------


def test_the_change_log_keeps_old_rows_and_old_cursors_stay_valid(db, config) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    seqs = [row[0] for row in db.execute("SELECT change_seq FROM change_log ORDER BY change_seq")]
    cursor = f"{changes.epoch(db)}:{seqs[0] - 1}"
    db.execute("UPDATE change_log SET at = ?", (ago(),))

    say(db, config, conversation_id, "a change that would prune the old ones")  # record() prunes

    assert [row[0] for row in db.execute("SELECT change_seq FROM change_log ORDER BY change_seq")][:len(seqs)] == seqs
    assert db.execute("SELECT value FROM log_meta WHERE key = 'pruned_through'").fetchone()[0] == "0"
    page = changes.read(db, changes.Scope.ALL, cursor=cursor, limit=100)
    assert [entry["change_seq"] for entry in page["entries"]][:len(seqs)] == seqs


# Summaries and vectors ----------------------------------------------------------------------------------------


def stored(db, generation: str) -> dict[str, int]:
    """How much of `generation` is stored, table by table."""
    return {
        "versions": count(db, "SELECT COUNT(*) FROM artifact_versions WHERE generation = ?", generation),
        "inputs": count(db, "SELECT COUNT(*) FROM artifact_inputs WHERE version_id IN"
                            " (SELECT id FROM artifact_versions WHERE generation = ?)", generation),
        "depends_on": count(db, "SELECT COUNT(*) FROM artifact_depends_on WHERE version_id IN"
                                " (SELECT id FROM artifact_versions WHERE generation = ?)", generation),
        "vectors": count(db, "SELECT COUNT(*) FROM vectors WHERE generation = ?", generation),
        "vector_skips": count(db, "SELECT COUNT(*) FROM vector_skips WHERE generation = ?", generation),
        "batches": count(db, "SELECT COUNT(*) FROM batches WHERE generation = ?", generation),
        "batch_pages": count(db, "SELECT COUNT(*) FROM batch_pages WHERE batch_id IN"
                                 " (SELECT id FROM batches WHERE generation = ?)", generation),
    }


def replaced_generation(db, config) -> tuple[str, str, str, dict[str, int], dict[str, int]]:
    """A generation with two versions per kind, vectors and a skip, replaced by another and aged past its
    retention; then a writer call sweeps. Returns the conversation, both generations, and what the old one held
    before and after that sweep."""
    conversation_id, sent = converse_in(db, config, 2)
    old = embedding_generation(db, config)
    manifest = begin_in_generation(db, config, conversation_id, old, kinds=derived.KINDS)
    first = {**{("kind", kind): UNIT["x"] for kind in manifest["kinds"]}, ("message_id", sent[1]): UNIT["y"]}
    commit_in_generation(db, config, manifest, payload(
        manifest, request_id="r-old-1", vectors=first,
        skips=[{"item": {"message_id": sent[0]}, "reason": "over_token_limit"}]))
    say(db, config, conversation_id, "a later note")
    manifest = begin_in_generation(db, config, conversation_id, old, kinds=derived.KINDS)
    later = {**{("kind", kind): UNIT["x"] for kind in manifest["kinds"]},
             **{("message_id", message_id): UNIT["y"] for message_id in items(manifest)}}
    commit_in_generation(db, config, manifest, payload(manifest, request_id="r-old-2", vectors=later))
    derived.generation_activate(db, config, "alpha", old)
    new = embedding_generation(db, config)
    fill_generation(db, config, conversation_id, new)
    derived.generation_activate(db, config, "alpha", new)
    db.execute("UPDATE generations SET deactivated_at = ? WHERE id = ?", (ago(timedelta(days=2)), old))
    before = stored(db, old)
    other, _ = converse_in(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # a writer call sweeps
    return conversation_id, old, new, before, stored(db, old)


def test_a_replaced_generations_versions_inputs_and_dependencies_are_kept(db, config) -> None:
    _, _, _, before, after = replaced_generation(db, config)

    assert before["versions"] == 6 and before["depends_on"] > 0  # the setup built what is to be kept
    assert (after["versions"], after["inputs"], after["depends_on"]) == (
        before["versions"], before["inputs"], before["depends_on"])


def test_a_replaced_generations_vectors_and_skips_are_kept(db, config) -> None:
    _, _, _, before, after = replaced_generation(db, config)

    assert before["vectors"] > 0 and before["vector_skips"] == 1
    assert (after["vectors"], after["vector_skips"]) == (before["vectors"], before["vector_skips"])


def test_a_replaced_generations_work_sessions_are_kept(db, config) -> None:
    _, _, _, before, after = replaced_generation(db, config)

    assert before["batches"] == 2 and before["batch_pages"] > 0
    assert (after["batches"], after["batch_pages"]) == (before["batches"], before["batch_pages"])


def test_guard_a_replaced_generation_is_never_served(db, config) -> None:
    conversation_id, old, new, _, _ = replaced_generation(db, config)

    served = search.read_artifacts(db, config, "viewer", conversation_id)["artifacts"]
    assert {artifact["generation"] for artifact in served} == {new}
    assert db.execute("SELECT active FROM generations WHERE id = ?", (old,)).fetchone()[0] == 0


def test_an_invalid_version_a_newer_one_replaced_is_kept_with_its_vectors_and_not_served(db, config) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    generation = embedding_generation(db, config)
    fill_generation(db, config, conversation_id, generation)
    derived.generation_activate(db, config, "alpha", generation)
    [first] = versions(db, conversation_id)  # the summary
    db.execute("UPDATE artifact_versions SET state = 'invalid' WHERE id = ?", (first["id"],))
    manifest = begin_in_generation(db, config, conversation_id, generation, kinds=("summary",))
    commit_in_generation(db, config, manifest, payload(manifest, request_id="r-rebase"))  # version 2 replaces it
    other, _ = converse_in(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # a writer call sweeps

    assert count(db, "SELECT COUNT(*) FROM artifact_versions WHERE id = ?", first["id"]) == 1
    assert inputs(db, first["id"]) == [[1, 2]]
    assert count(db, "SELECT COUNT(*) FROM vectors WHERE item_kind = 'artifact' AND item_id = ?", first["id"]) == 1
    served = search.read_artifacts(db, config, "viewer", conversation_id, producer="alpha")["artifacts"]
    assert [(a["kind"], a["version"]) for a in served if a["kind"] == "summary"] == [("summary", 2)]


# Work sessions and receipts -----------------------------------------------------------------------------------


def test_expired_work_sessions_and_their_pages_are_kept(db, config) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    write_commit(db, config, manifest)
    abandoned = begin(db, config, conversation_id, kinds=("decisions",))
    read_all(db, config, abandoned)
    db.execute("UPDATE batches SET expires_at = 0")
    pages = count(db, "SELECT COUNT(*) FROM batch_pages")

    other, _ = converse_in(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # a writer call sweeps

    assert count(db, "SELECT COUNT(*) FROM batches WHERE id IN (?, ?)", manifest["batch_id"],
                 abandoned["batch_id"]) == 2
    assert pages > 0 and count(db, "SELECT COUNT(*) FROM batch_pages") >= pages


def test_a_commit_receipt_past_its_expiry_is_kept(db, config) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    cycle(db, config, conversation_id)
    db.execute("UPDATE commit_requests SET expires_at = ?", (ago(timedelta(days=1)),))

    other, _ = converse_in(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # a writer call sweeps

    assert count(db, "SELECT COUNT(*) FROM commit_requests WHERE request_id = 'r-1'") == 1


def test_a_reused_request_id_under_an_aged_out_receipt_is_refused_and_writes_nothing(db, config) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    cycle(db, config, conversation_id, request_id="r-1")
    db.execute("UPDATE commit_requests SET expires_at = ?", (ago(timedelta(days=1)),))
    receipt = dict(db.execute("SELECT * FROM commit_requests WHERE request_id = 'r-1'").fetchone())
    other = begin(db, config, conversation_id, kinds=("decisions",))
    read_all(db, config, other)
    versions_before = count(db, "SELECT COUNT(*) FROM artifact_versions")

    refused("idempotency_conflict", write_commit, db, config, other, request_id="r-1")  # no crash

    assert dict(db.execute("SELECT * FROM commit_requests WHERE request_id = 'r-1'").fetchone()) == receipt
    assert count(db, "SELECT COUNT(*) FROM artifact_versions") == versions_before
    assert count(db, "SELECT COUNT(*) FROM batches WHERE id = ? AND state = 'open'", other["batch_id"]) == 1
    assert write_commit(db, config, other, request_id="r-2")["created"] is True


@pytest.mark.parametrize("how", ["abandoned", "committed", "committed_and_receipt_aged_out"])
def test_guard_an_expired_or_closed_batch_and_an_aged_out_receipt_create_nothing(db, config, how: str) -> None:
    conversation_id, _ = converse_in(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    if how != "abandoned":
        write_commit(db, config, manifest)
    db.execute("UPDATE batches SET expires_at = 0")
    if how == "committed_and_receipt_aged_out":
        db.execute("UPDATE commit_requests SET expires_at = ?", (ago(timedelta(days=1)),))
    other, _ = converse_in(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # a writer call sweeps
    before = count(db, "SELECT COUNT(*) FROM artifact_versions")

    # The code names the batch gone, expired or closed; which one is the implementer's to say.
    with pytest.raises(BatonError) as refused:
        write_commit(db, config, manifest, request_id="r-new")
    assert refused.value.code in {"unknown_batch", "batch_expired", "batch_closed"}
    assert count(db, "SELECT COUNT(*) FROM artifact_versions") == before


# Summaries are no longer invalidated by retention -------------------------------------------------------------


def test_a_summary_built_from_an_old_read_message_stays_valid_and_served(db, config) -> None:
    conversation_id, sent = converse_in(db, config, 3)
    cycle(db, config, conversation_id)

    expire(db, sent[1])  # every copy of the middle message is read, 40 days old, and read again

    assert [v["state"] for v in versions(db, conversation_id)] == ["valid"]
    served = search.read_artifacts(db, config, "viewer", conversation_id)["artifacts"]
    assert [(a["kind"], a["version"]) for a in served] == [("summary", 1)]
