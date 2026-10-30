"""The local indexing writer: batches, manifests, commits, receipts, and invalidation in the same transaction.

Contract: indexer r1–r5 with r5b final, and the designer's rulings on the slice 2 plan (implicit generation,
`dirty --producer` as a selector, rebase as the recovery from an invalid version, receipts kept apart from
batches, invalidation by one match over flattened inputs). Generic role and producer names only; nothing here
runs a model or a worker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from baton import cli, conversations, messages, store
from baton import config as config_module
from baton.config import Config, ConfigError
from baton.errors import BatonError
from baton.store import connect

try:  # written before the module exists: each test then fails on its own instead of the whole run stopping
    from baton import derived
except ImportError:
    derived = None  # type: ignore[assignment]

GROUP = ["orchestrator", "reviewer", "assistant"]
ROLES = {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
         "reviewer": {}, "assistant": {}, "outsider": {}}


def index_config(tmp_path: Path, **indexer: Any) -> Config:
    """Generic roles, conversations on, and two listed producers unless a test says otherwise."""
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": ROLES, "conversations": {"enabled": True},
        "indexer": {"producers": ["alpha", "beta"], **indexer},
    })


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return index_config(tmp_path)


@pytest.fixture
def db(config: Config):
    connection = connect(config.db_path)
    yield connection
    connection.close()


def refused(code: str, operation, *args, **kwargs) -> BatonError:
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    assert raised.value.code == code, f"{raised.value.code}: {raised.value}"
    return raised.value


def converse(db, config, count: int, participants=GROUP) -> tuple[str, list[str]]:
    """A conversation with `count` messages from the orchestrator, so every message has two copies."""
    conversation_id = conversations.create(db, config, actor="orchestrator", title="Release plan",
                                           participants=participants)["conversation_id"]
    return conversation_id, [say(db, config, conversation_id, f"note {n}") for n in range(1, count + 1)]


def say(db, config, conversation_id: str, text: str, sender: str = "orchestrator") -> str:
    return conversations.send(db, config, sender=sender, conversation_id=conversation_id, text=text)["message_id"]


def expire(db, *message_ids: str, only: str | None = None) -> None:
    """Retention takes every copy of each message (or only `only`'s), through the real sweep."""
    for role in GROUP:
        messages.read(db, role)  # delivered and read, so retention may take them
    for message_id in message_ids:
        aged = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        if only is None:
            db.execute("UPDATE messages SET at = ? WHERE id = ?", (aged, message_id))
        else:
            db.execute("UPDATE messages SET at = ? WHERE id = ? AND recipient = ?", (aged, message_id, only))
    messages.sweep(db)


def begin(db, config, conversation_id: str, kinds=("summary",), producer: str = "alpha") -> dict[str, Any]:
    return derived.begin_batch(db, config, producer, conversation_id, list(kinds))


def read_all(db, config, manifest: dict[str, Any], producer: str = "alpha") -> None:
    for page in range(1, manifest["pages"] + 1):
        derived.read_batch(db, config, producer, manifest["batch_id"], page)


def artifacts_for(manifest: dict[str, Any], kinds=None, **overrides: Any) -> list[dict[str, Any]]:
    return [{"kind": kind, "body": f"{kind} of the release plan", "citations": [],
             "expected_version": manifest["kinds"][kind]["prior_version"], **overrides}
            for kind in (kinds or manifest["kinds"])]


def commit(db, config, manifest: dict[str, Any], artifacts=None, request_id: str = "r-1",
           producer: str = "alpha") -> dict[str, Any]:
    payload = {"request_id": request_id, "artifacts": artifacts_for(manifest) if artifacts is None else artifacts}
    return derived.commit_batch(db, config, producer, manifest["batch_id"], payload)


def cycle(db, config, conversation_id: str, kinds=("summary",), request_id: str = "r-1",
          producer: str = "alpha") -> dict[str, Any]:
    manifest = begin(db, config, conversation_id, kinds, producer)
    read_all(db, config, manifest, producer)
    return commit(db, config, manifest, request_id=request_id, producer=producer)


def versions(db, conversation_id: str, kind: str = "summary", producer: str = "alpha") -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM artifact_versions WHERE conversation_id = ? AND kind = ? AND producer = ? ORDER BY version",
        (conversation_id, kind, producer))]


def inputs(db, version_id: str) -> list[list[int]]:
    return [[row[0], row[1]] for row in db.execute(
        "SELECT from_seq, to_seq FROM artifact_inputs WHERE version_id = ? ORDER BY from_seq", (version_id,))]


def snapshot(db, skip: tuple[str, ...] = ()) -> dict[str, list[tuple]]:
    """Every table's rows, so a call can be shown to write nothing (or only what it should)."""
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        " AND name NOT LIKE '%fts%' ORDER BY name")]
    return {table: sorted(tuple(row) for row in db.execute(f"SELECT * FROM {table}"))
            for table in tables if table not in skip}


def dirty(db, config, producer: str = "alpha") -> dict[tuple[str, str], dict[str, Any]]:
    return {(pair["conversation_id"], pair["kind"]): pair for pair in derived.dirty(db, config, producer)["pairs"]}


# Config: producers, and limits ----------------------------------------------------------------------------


def test_29_a_producer_name_may_not_be_a_role_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="indexer.producers"):
        index_config(tmp_path, producers=["reviewer"])


@pytest.mark.parametrize("producers", [["Alpha"], ["has space"], ["alpha", "alpha"], "alpha", [7]])
def test_29_producer_names_follow_the_role_name_rule_and_are_listed_once(tmp_path: Path, producers) -> None:
    with pytest.raises(ConfigError, match="indexer.producers"):
        index_config(tmp_path, producers=producers)


def test_the_indexer_limits_default_to_the_contract_and_refuse_nonsense(tmp_path: Path) -> None:
    limits = index_config(tmp_path).indexer
    assert (limits.max_artifact_bytes, limits.max_citations, limits.max_manifest_items, limits.max_manifest_segments,
            limits.page_items, limits.max_open_batches, limits.batch_ttl_seconds, limits.max_input_segments) \
        == (16384, 64, 500, 64, 100, 4, 900, 256)
    assert limits.receipt_retention_days == 30 and limits.producers == ("alpha", "beta")
    for bad in ({"max_citations": 0}, {"page_items": "100"}, {"max_open_batches": True}):
        with pytest.raises(ConfigError, match="indexer."):
            index_config(tmp_path, **bad)


def test_no_producers_means_the_writer_is_off(tmp_path: Path) -> None:
    config = index_config(tmp_path, producers=[])
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 1)
    refused("forbidden", begin, db, config, conversation_id)
    db.close()


def test_29_every_writer_refuses_an_unlisted_producer(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    before = snapshot(db)
    refused("forbidden", begin, db, config, conversation_id, producer="gamma")
    refused("forbidden", derived.read_batch, db, config, "gamma", manifest["batch_id"], 1)
    refused("forbidden", commit, db, config, manifest, producer="gamma")
    assert snapshot(db) == before


def test_29_dirty_needs_no_listed_producer_and_creates_nothing(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    before = snapshot(db)
    for name in ("alpha", "gamma"):  # a selector, never checked against the list
        pairs = dirty(db, config, name)
        assert {kind for (_, kind) in pairs} == set(derived.KINDS)
        assert all(pair["state"] == "none" for pair in pairs.values())
    assert snapshot(db) == before  # in particular, no generation was created


def test_24_no_mcp_tool_writes_derived_data_or_names_a_producer(config) -> None:
    import inspect

    from baton import app
    registered = [name for name, _, _ in app.TOOLS + app.CONVERSATION_TOOLS + app.SEARCH_TOOLS + app.ARTIFACT_TOOLS]
    writer_words = ("batch", "generation", "dirty", "commit", "index", "producer")
    assert not [name for name in registered if any(word in name for word in writer_words)]
    assert all(read_only for _, _, read_only in app.ARTIFACT_TOOLS)
    for name in registered:  # no tool takes a producer as who is calling; reads take one only as a filter
        if name not in ("read_artifacts", "search"):
            assert "producer" not in inspect.signature(getattr(app.Tools, name)).parameters, name
    source = inspect.getsource(app)
    assert not [call for call in ("begin_batch", "read_batch", "commit_batch") if call in source]
    assert "producer" not in app.Tools(config).whoami("reviewer")


# Manifests: exact items, holes, digests, bounds ------------------------------------------------------------


def test_the_first_batch_creates_one_implicit_generation_scoped_all(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    first = begin(db, config, conversation_id)
    second = begin(db, config, conversation_id, kinds=("decisions",))
    assert first["generation"] == second["generation"] and first["scope_id"] == "all"
    rows = [dict(row) for row in db.execute("SELECT * FROM generations")]
    assert len(rows) == 1 and rows[0]["producer"] == "alpha" and rows[0]["scope_id"] == "all"
    assert rows[0]["active"] == 1 and json.loads(rows[0]["profile"]) == {}
    assert [tuple(row) for row in db.execute("SELECT DISTINCT scope_id FROM batches")] == [("all",)]


def test_a_manifest_lists_exact_items_with_counts_and_digests(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    manifest = begin(db, config, conversation_id)
    [segment] = manifest["segments"]
    assert [(item["seq"], item["message_id"], item["item_revision"]) for item in segment["items"]] \
        == [(1, sent[0], 1), (2, sent[1], 1), (3, sent[2], 1)]
    assert (segment["from_seq"], segment["to_seq"], segment["count"]) == (1, 3, 3)
    expected = hashlib.sha256("".join(f"{n}:{sent[n - 1]}:1\n" for n in (1, 2, 3)).encode()).hexdigest()
    assert segment["digest"] == expected
    assert manifest["item_count"] == 3 and manifest["absent"] == [] and manifest["window"] == {"from_seq": 1,
                                                                                               "to_seq": 3}
    assert manifest["source_revision"] == db.execute("SELECT revision FROM conversations").fetchone()[0]
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    assert manifest["manifest_digest"] == hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_11_a_retention_hole_is_absent_and_splits_the_segments(db, config) -> None:
    conversation_id, sent = converse(db, config, 5)
    expire(db, sent[2])
    manifest = begin(db, config, conversation_id)
    assert manifest["absent"] == [{"from_seq": 3, "to_seq": 3, "reason": "expired"}]
    assert [(s["from_seq"], s["to_seq"], s["count"]) for s in manifest["segments"]] == [(1, 2, 2), (4, 5, 2)]
    read_all(db, config, manifest)
    commit(db, config, manifest)
    [version] = versions(db, conversation_id)
    assert version["covered_through_seq"] == 5  # coverage passes the hole
    assert inputs(db, version["id"]) == [[1, 2], [4, 5]]  # without counting it as served


def test_a_window_is_bounded_and_paged(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_items=3, page_items=2)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 5)
    manifest = begin(db, config, conversation_id)
    assert manifest["window"] == {"from_seq": 1, "to_seq": 3} and manifest["pages"] == 2
    assert [item["seq"] for item in derived.read_batch(db, config, "alpha", manifest["batch_id"], 2)["items"]] == [3]
    refused("invalid_request", derived.read_batch, db, config, "alpha", manifest["batch_id"], 3)
    db.close()


def test_read_batch_serves_text_and_writes_only_its_delivery_row(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    before = snapshot(db, skip=("batch_pages",))
    page = derived.read_batch(db, config, "alpha", manifest["batch_id"], 1)
    assert [(item["seq"], item["message_id"], item["text"]) for item in page["items"]] \
        == [(1, sent[0], "note 1"), (2, sent[1], "note 2")]
    assert snapshot(db, skip=("batch_pages",)) == before
    assert [tuple(row) for row in db.execute("SELECT batch_id, page FROM batch_pages")] == [(manifest["batch_id"], 1)]


def test_bounds_are_refused_naming_the_limit(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_open_batches=1, max_artifact_bytes=8, max_citations=1)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    assert refused("limit_exceeded", begin, db, config, conversation_id).extra["limit"] == "max_open_batches"
    read_all(db, config, manifest)
    error = refused("limit_exceeded", commit, db, config, manifest, artifacts_for(manifest, body="x" * 9))
    assert error.extra["limit"] == "max_artifact_bytes"
    error = refused("limit_exceeded", commit, db, config, manifest, artifacts_for(manifest, body="x",
                                                                                   citations=sent))
    assert error.extra["limit"] == "max_citations"
    db.close()


def test_begin_refuses_what_it_cannot_batch(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    refused("unknown_conversation", begin, db, config, "conv_missing")
    refused("invalid_request", begin, db, config, conversation_id, kinds=("summary_recent",))
    refused("invalid_request", begin, db, config, conversation_id, kinds=())
    cycle(db, config, conversation_id)
    refused("up_to_date", begin, db, config, conversation_id)


# Delivery, idempotency, versions, checkpoints ---------------------------------------------------------------


def test_12_a_commit_before_every_page_is_delivered_is_incomplete(tmp_path: Path) -> None:
    config = index_config(tmp_path, page_items=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    derived.read_batch(db, config, "alpha", manifest["batch_id"], 1)
    refused("batch_incomplete", commit, db, config, manifest)
    derived.read_batch(db, config, "alpha", manifest["batch_id"], 2)
    assert commit(db, config, manifest)["created"] is True
    db.close()


def test_5_an_identical_replay_creates_nothing_and_any_change_conflicts(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    first = commit(db, config, manifest, artifacts_for(manifest, citations=[sent[0]]))
    before = snapshot(db)
    replay = commit(db, config, manifest, artifacts_for(manifest, citations=[sent[0]]))
    assert replay["created"] is False and replay["versions"] == first["versions"]
    assert snapshot(db) == before
    for changed in ({"body": "another body"}, {"citations": [sent[1]]}, {"citations": []}, {"expected_version": 1}):
        refused("idempotency_conflict", commit, db, config, manifest, artifacts_for(manifest, **{
            "citations": [sent[0]], **changed}))
    other = begin(db, config, conversation_id, kinds=("decisions",))  # the same request id on another batch
    read_all(db, config, other)
    refused("idempotency_conflict", commit, db, config, other, artifacts_for(manifest, citations=[sent[0]]))
    assert snapshot(db, skip=("batch_pages", "batches", "generations")) == \
        {key: value for key, value in before.items() if key not in ("batch_pages", "batches", "generations")}


def test_6_two_commits_on_one_version_give_one_success_and_one_conflict(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    first, second = begin(db, config, conversation_id), begin(db, config, conversation_id)
    read_all(db, config, first)
    read_all(db, config, second)
    commit(db, config, first, request_id="r-a")
    refused("version_conflict", commit, db, config, second, request_id="r-b")
    assert [v["version"] for v in versions(db, conversation_id)] == [1]


def test_6_an_expiry_inside_the_window_supersedes_the_batch(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    expire(db, sent[1])
    error = refused("batch_superseded", commit, db, config, manifest)
    assert sent[1] in str(error)
    assert versions(db, conversation_id) == []


def test_13_the_prior_s_input_expiring_mid_batch_supersedes_it(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    cycle(db, config, conversation_id, request_id="r-1")
    [prior] = versions(db, conversation_id)
    say(db, config, conversation_id, "a new message")
    manifest = begin(db, config, conversation_id)
    assert manifest["kinds"]["summary"]["prior_version_id"] == prior["id"]
    read_all(db, config, manifest)
    expire(db, sent[0])  # an input of the prior, after the batch began
    error = refused("batch_superseded", commit, db, config, manifest, request_id="r-2")
    assert prior["id"] in str(error)
    assert [v["state"] for v in versions(db, conversation_id)] == ["invalid"]


def test_4_a_citation_outside_the_covered_inputs_is_refused_by_name(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    other_id, other_sent = converse(db, config, 1)
    error = refused("invalid_request", commit, db, config, manifest, artifacts_for(manifest, citations=[
        sent[0], other_sent[0]]))
    assert other_sent[0] in str(error)
    later = say(db, config, conversation_id, "after the batch began")  # in the conversation, not in the window
    error = refused("invalid_request", commit, db, config, manifest, artifacts_for(manifest, citations=[later]))
    assert later in str(error)
    assert commit(db, config, manifest, artifacts_for(manifest, citations=sent))["created"] is True


def test_14_kinds_keep_their_own_checkpoints_and_none_may_skip(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_items=2)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 6)
    cycle(db, config, conversation_id, kinds=("summary", "decisions"), request_id="r-1")  # both at 2
    cycle(db, config, conversation_id, kinds=("summary",), request_id="r-2")  # summary at 4, decisions at 2
    mixed = begin(db, config, conversation_id, kinds=("summary", "decisions"))
    assert mixed["window"]["from_seq"] == 3  # from the lowest checkpoint
    read_all(db, config, mixed)
    error = refused("checkpoint_gap", commit, db, config, mixed, request_id="r-3")
    assert "summary" in str(error)
    commit(db, config, mixed, artifacts_for(mixed, kinds=["decisions"]), request_id="r-4")
    assert versions(db, conversation_id, "summary")[-1]["covered_through_seq"] == 4
    assert versions(db, conversation_id, "decisions")[-1]["covered_through_seq"] == 4
    ahead = begin(db, config, conversation_id, kinds=("summary",))  # 5..6
    read_all(db, config, ahead)
    refused("invalid_request", commit, db, config, ahead, artifacts_for(mixed, kinds=["decisions"]),
            request_id="r-5")  # a kind the batch wasn't begun for
    db.close()


def test_7_a_crash_mid_commit_leaves_the_old_version_and_a_retry_succeeds(db, config, monkeypatch) -> None:
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id, kinds=("summary", "decisions"))
    read_all(db, config, manifest)
    before = snapshot(db)
    real = derived._write_version
    calls = []

    def crash_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(derived, "_write_version", crash_on_second)
    with pytest.raises(RuntimeError):
        commit(db, config, manifest)
    assert snapshot(db) == before  # neither kind, no receipt, the batch still open
    monkeypatch.setattr(derived, "_write_version", real)
    result = commit(db, config, manifest)
    assert result["created"] is True and {v["kind"] for v in result["versions"]} == {"summary", "decisions"}


def test_10_batches_and_commits_log_no_change_event_or_wake(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    watched = ("change_log", "events", "wakes", "mailboxes", "cursors")
    before = {table: snapshot(db)[table] for table in watched}
    revision = db.execute("SELECT revision FROM conversations").fetchone()[0]
    cycle(db, config, conversation_id, kinds=("summary", "decisions"))
    assert {table: snapshot(db)[table] for table in watched} == before
    assert db.execute("SELECT revision FROM conversations").fetchone()[0] == revision


def test_a_batch_expires_and_belongs_only_to_its_producer(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    refused("forbidden", derived.read_batch, db, config, "beta", manifest["batch_id"], 1)
    read_all(db, config, manifest)
    refused("forbidden", commit, db, config, manifest, producer="beta")
    db.execute("UPDATE batches SET expires_at = 0")
    refused("batch_expired", derived.read_batch, db, config, "alpha", manifest["batch_id"], 1)
    refused("batch_expired", commit, db, config, manifest)
    assert versions(db, conversation_id) == []


def test_a_committed_batch_takes_no_second_commit(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    commit(db, config, manifest, request_id="r-1")
    refused("batch_closed", commit, db, config, manifest, request_id="r-2")


def test_commits_are_budgeted_per_producer(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_commits_per_hour=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    cycle(db, config, conversation_id, kinds=("summary",), request_id="r-1")
    manifest = begin(db, config, conversation_id, kinds=("decisions",))
    read_all(db, config, manifest)
    assert refused("limit_exceeded", commit, db, config, manifest, request_id="r-2").extra["limit"] \
        == "max_commits_per_hour"
    cycle(db, config, conversation_id, kinds=("decisions",), producer="beta")  # another producer's own budget
    db.close()


# Invalidation, in the same transaction as the expiry ---------------------------------------------------------


def test_2_the_last_copy_expiring_invalidates_the_version_and_lists_it_dirty(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    cycle(db, config, conversation_id)
    expire(db, sent[1], only="reviewer")  # one copy of two goes: nothing changes
    assert [v["state"] for v in versions(db, conversation_id)] == ["valid"]
    expire(db, sent[1])  # the last copy goes
    assert [v["state"] for v in versions(db, conversation_id)] == ["invalid"]
    assert dirty(db, config)[(conversation_id, "summary")]["state"] == "invalid"


def test_2_invalidation_shares_the_expiry_transaction(db, config, monkeypatch) -> None:
    conversation_id, sent = converse(db, config, 2)
    cycle(db, config, conversation_id)

    def failing(*args, **kwargs):
        raise RuntimeError("invalidation failed")

    monkeypatch.setattr(derived, "invalidate_lost", failing)
    with pytest.raises(RuntimeError):
        expire(db, sent[0])
    held = db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (sent[0],)).fetchone()[0]
    assert held == 2 and [v["state"] for v in versions(db, conversation_id)] == ["valid"]


def test_a_chain_of_versions_is_invalidated_in_full_by_one_expiry(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_items=2)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 8)
    for n in range(4):
        cycle(db, config, conversation_id, request_id=f"r-{n}")
    chain = versions(db, conversation_id)
    assert [v["covered_through_seq"] for v in chain] == [2, 4, 6, 8]
    assert inputs(db, chain[-1]["id"]) == [[1, 8]]  # each version holds its prior's inputs
    expire(db, sent[0])  # seq 1, in the first window
    assert [v["state"] for v in versions(db, conversation_id)] == ["invalid"] * 4
    db.close()


def test_3_a_participant_change_flips_no_version(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    cycle(db, config, conversation_id)
    conversations.add(db, config, actor="orchestrator", conversation_id=conversation_id, participant="outsider")
    conversations.remove(db, config, actor="orchestrator", conversation_id=conversation_id, participant="reviewer")
    assert [v["state"] for v in versions(db, conversation_id)] == ["valid"]


def test_only_the_producer_s_own_pairs_are_its_dirty_set(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    cycle(db, config, conversation_id, kinds=derived.KINDS)
    assert (conversation_id, "summary") not in dirty(db, config, "alpha")
    assert dirty(db, config, "beta")[(conversation_id, "summary")]["state"] == "none"
    say(db, config, conversation_id, "one more")
    behind = dirty(db, config, "alpha")[(conversation_id, "summary")]
    assert (behind["state"], behind["covered_through_seq"], behind["last_seq"]) == ("behind", 2, 3)


# Recovery by rebase --------------------------------------------------------------------------------------


def test_an_invalid_version_recovers_by_a_rebase_from_seq_1(db, config) -> None:
    conversation_id, sent = converse(db, config, 3)
    cycle(db, config, conversation_id, request_id="r-1")
    [old] = versions(db, conversation_id)
    expire(db, sent[1])  # a real last-copy expiry
    assert dirty(db, config)[(conversation_id, "summary")]["state"] == "invalid"
    manifest = begin(db, config, conversation_id)
    plan = manifest["kinds"]["summary"]
    assert (plan["rebase"], plan["reason"], plan["prior_version_id"], plan["start_seq"]) \
        == (True, "prior_invalid", None, 1)
    assert plan["prior_version"] == old["version"]  # what the compare-and-set names
    assert manifest["absent"] == [{"from_seq": 2, "to_seq": 2, "reason": "expired"}]
    read_all(db, config, manifest)
    commit(db, config, manifest, request_id="r-2")
    old_now, new = versions(db, conversation_id)
    assert (old_now["state"], new["state"], new["version"]) == ("invalid", "valid", old["version"] + 1)
    assert inputs(db, new["id"]) == [[1, 1], [3, 3]]
    assert db.execute("SELECT COUNT(*) FROM artifact_depends_on WHERE version_id = ?", (new["id"],)).fetchone()[0] \
        == 0
    assert (conversation_id, "summary") not in dirty(db, config)


def test_a_rebase_commit_naming_an_older_version_conflicts(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    cycle(db, config, conversation_id, request_id="r-1")
    expire(db, sent[0])
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    refused("version_conflict", commit, db, config, manifest, artifacts_for(manifest, expected_version=0),
            request_id="r-2")


def test_a_long_conversation_recovers_over_several_batches(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_items=3)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 3)
    cycle(db, config, conversation_id, request_id="r-1")
    for n in range(3):
        say(db, config, conversation_id, f"later {n}")
    expire(db, sent[0])
    rebase = begin(db, config, conversation_id)
    assert rebase["kinds"]["summary"]["rebase"] is True and rebase["window"] == {"from_seq": 1, "to_seq": 4}
    read_all(db, config, rebase)
    partial = commit(db, config, rebase, request_id="r-2")
    assert [v["state"] for v in partial["versions"]] == ["behind"]  # as dirty says: valid, not yet current
    assert dirty(db, config)[(conversation_id, "summary")]["state"] == "behind"
    assert [v["state"] for v in commit(db, config, rebase, request_id="r-2")["versions"]] == ["behind"]
    chained = begin(db, config, conversation_id)
    assert chained["kinds"]["summary"]["rebase"] is False and chained["window"]["from_seq"] == 5
    read_all(db, config, chained)
    commit(db, config, chained, request_id="r-3")
    assert (conversation_id, "summary") not in dirty(db, config)
    assert inputs(db, versions(db, conversation_id)[-1]["id"]) == [[2, 6]]
    db.close()


def test_19_a_provenance_limit_fails_closed_and_stays_dirty(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_input_segments=2)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 6)
    expire(db, sent[1], sent[3])  # holes at 2 and 4: inputs 1, 3 and 5-6 are three disjoint segments
    first = begin(db, config, conversation_id)
    assert first["window"] == {"from_seq": 1, "to_seq": 4}  # bounded to what two segments can hold
    read_all(db, config, first)
    commit(db, config, first)
    before = snapshot(db)
    error = refused("provenance_limit_exceeded", begin, db, config, conversation_id)
    assert error.extra["limit"] == "max_input_segments" and error.extra["kind"] == "summary"
    assert (error.extra["segments"], error.extra["items"]) == (3, 3)  # 1, 3 and 5: three ranges of one item
    assert snapshot(db) == before  # nothing advances
    pair = dirty(db, config)[(conversation_id, "summary")]
    assert (pair["state"], pair["covered_through_seq"], pair["provenance_limit_exceeded"]) == ("behind", 4, True)
    db.close()


# Receipts, kept apart from batches -------------------------------------------------------------------------


def test_a_receipt_row_carries_what_a_replay_needs(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    result = cycle(db, config, conversation_id)
    row = dict(db.execute("SELECT * FROM commit_requests").fetchone())
    assert (row["producer"], row["request_id"]) == ("alpha", "r-1") and len(row["digest"]) == 64
    receipt = json.loads(row["receipt"])
    assert [v["version_id"] for v in receipt["versions"]] == [v["version_id"] for v in result["versions"]]
    committed = datetime.fromisoformat(row["committed_at"])
    assert datetime.fromisoformat(row["expires_at"]) - committed == timedelta(days=30)


def test_an_identical_replay_after_the_batch_is_cleaned_up_is_answered_from_the_receipt(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    first = commit(db, config, manifest)
    db.execute("UPDATE batches SET expires_at = 0")
    cycle(db, config, conversation_id, kinds=("decisions",), request_id="r-sweep")  # a writer call sweeps
    assert db.execute("SELECT COUNT(*) FROM batches WHERE id = ?", (manifest["batch_id"],)).fetchone()[0] == 0
    before = snapshot(db)
    replay = commit(db, config, manifest)
    assert replay["created"] is False and replay["versions"] == first["versions"]
    assert snapshot(db) == before
    expire(db, sent[0])  # invalidated since: the replay says so, and resurrects nothing
    replay = commit(db, config, manifest)
    assert [v["state"] for v in replay["versions"]] == ["invalid"]
    assert not {"body", "citations", "items"} & set(json.dumps(replay).split('"'))
    refused("idempotency_conflict", commit, db, config, manifest, artifacts_for(manifest, body="changed"))


def test_an_aged_out_receipt_cannot_create_anything(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    commit(db, config, manifest)
    db.execute("UPDATE batches SET expires_at = 0")
    db.execute("UPDATE commit_requests SET expires_at = ?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(),))
    cycle(db, config, conversation_id, kinds=("decisions",), request_id="r-sweep")
    assert db.execute("SELECT COUNT(*) FROM commit_requests WHERE request_id = 'r-1'").fetchone()[0] == 0
    before = snapshot(db)
    refused("unknown_batch", commit, db, config, manifest)
    assert snapshot(db) == before


def test_16_21_a_replay_after_revocation_gets_only_the_receipt_and_only_its_owner(tmp_path: Path) -> None:
    config = index_config(tmp_path)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    manifest = begin(db, config, conversation_id)
    read_all(db, config, manifest)
    first = commit(db, config, manifest)
    revoked = index_config(tmp_path, producers=["beta"])  # alpha removed from the config
    refused("forbidden", begin, db, revoked, conversation_id)
    refused("forbidden", derived.read_batch, db, revoked, "alpha", manifest["batch_id"], 1)
    replay = commit(db, revoked, manifest)
    assert replay["created"] is False and replay["versions"] == first["versions"]
    refused("idempotency_conflict", commit, db, revoked, manifest, artifacts_for(manifest, body="changed"))
    refused("forbidden", commit, db, revoked, manifest, request_id="r-unknown")
    refused("forbidden", commit, db, revoked, manifest, producer="beta")  # another principal, same request id
    db.close()


def test_26_a_producer_removed_from_the_config_is_refused_by_the_commands(db, config, tmp_path, capsys) -> None:
    conversation_id, _ = converse(db, config, 2)
    path = config_file(config, tmp_path, producers=["beta"])
    code, refusal = run(capsys, "--config", str(path), "index", "begin-batch", "--producer", "alpha",
                        "--conversation", conversation_id, "--kinds", "summary")
    assert code == 1 and refusal["error"] == "forbidden"


# The local commands, end to end -----------------------------------------------------------------------------


def config_file(config: Config, tmp_path: Path, producers=("alpha", "beta")) -> Path:
    path = tmp_path / "baton.toml"
    listed = ", ".join(f'"{name}"' for name in producers)
    path.write_text("\n".join([
        "[host]", 'public_url = "http://127.0.0.1:8765"', f'data_dir = "{config.data_dir}"',
        "[roles.orchestrator]", "can_create_conversations = true", "can_manage_participants = true",
        "[roles.reviewer]", "[roles.assistant]", "[roles.outsider]",
        "[conversations]", "enabled = true", "[indexer]", f"producers = [{listed}]", ""]))
    return path


def run(capsys, *argv: str) -> tuple[int, Any]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def files_digest(directory: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir()) if path.name.startswith("baton.db")}


def test_the_index_commands_batch_and_commit_end_to_end(db, config, tmp_path, capsys) -> None:
    conversation_id, sent = converse(db, config, 2)
    path = config_file(config, tmp_path)
    base = ["--config", str(path), "index"]
    code, manifest = run(capsys, *base, "begin-batch", "--producer", "alpha", "--conversation", conversation_id,
                         "--kinds", "summary,decisions")
    assert code == 0 and set(manifest["kinds"]) == {"summary", "decisions"}
    code, page = run(capsys, *base, "read-batch", manifest["batch_id"], "--page", "1", "--producer", "alpha")
    assert code == 0 and [item["message_id"] for item in page["items"]] == sent
    payload = tmp_path / "commit.json"
    payload.write_text(json.dumps({"request_id": "r-cli", "artifacts": artifacts_for(manifest)}))
    code, result = run(capsys, *base, "commit-batch", manifest["batch_id"], "--file", str(payload),
                       "--producer", "alpha")
    assert code == 0 and result["created"] is True
    code, listed = run(capsys, *base, "dirty", "--producer", "alpha")
    assert code == 0 and not [p for p in listed["pairs"] if p["conversation_id"] == conversation_id
                              and p["kind"] in ("summary", "decisions")]


def test_25_dirty_writes_nothing_at_all(db, config, tmp_path, capsys) -> None:
    conversation_id, _ = converse(db, config, 2)
    cycle(db, config, conversation_id)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path = config_file(config, tmp_path)
    before = files_digest(config.data_dir)
    code, listed = run(capsys, "--config", str(path), "index", "dirty", "--producer", "alpha")
    assert code == 0 and listed["producer"] == "alpha"
    assert files_digest(config.data_dir) == before  # not a byte, the WAL included


def test_dirty_requires_its_producer_selector(db, config, tmp_path, capsys) -> None:
    path = config_file(config, tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["--config", str(path), "index", "dirty"])
    assert "--producer" in capsys.readouterr().err  # required, with no every-producer default


# Schema ---------------------------------------------------------------------------------------------------


def test_the_derived_tables_are_step_10_and_carry_scope_all(db, config) -> None:
    assert set(store._ADDED_TABLES[10]) == {  # named by its own step, so a later step doesn't break it
        "generations", "batches", "batch_pages", "artifact_versions", "artifact_inputs", "artifact_depends_on",
        "commit_requests"}
    conversation_id, _ = converse(db, config, 2)
    cycle(db, config, conversation_id)
    for table in ("generations", "batches", "artifact_versions"):
        assert [tuple(row) for row in db.execute(f"SELECT DISTINCT scope_id FROM {table}")] == [("all",)], table


def test_the_derived_inputs_are_indexed_for_the_expiry_match(db) -> None:
    plan = " ".join(str(tuple(row)) for row in db.execute(
        "EXPLAIN QUERY PLAN SELECT version_id FROM artifact_inputs WHERE conversation_id = 'c'"
        " AND from_seq <= 3 AND to_seq >= 3"))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_the_read_only_open_sees_the_derived_tables(tmp_path: Path) -> None:
    """The read-only open of a current database still works with the new tables present."""
    config = index_config(tmp_path)
    connect(config.db_path).close()
    reader = store.connect_read_only(config.db_path)
    assert reader.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM artifact_versions")
    reader.close()


# Review round: bounded holes and scans, labels that match the current state, a complete receipt -----------


def test_a_run_of_holes_is_one_entry_and_the_manifest_stays_bounded(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_items=1)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 12)
    expire(db, *sent[:11])  # seqs 1-11 gone, only 12 left
    manifest = begin(db, config, conversation_id)
    assert manifest["absent"] == [{"from_seq": 1, "to_seq": 11, "reason": "expired"}]
    assert [item["seq"] for segment in manifest["segments"] for item in segment["items"]] == [12]
    db.close()


def test_alternating_holes_stay_within_the_segment_cap(tmp_path: Path) -> None:
    config = index_config(tmp_path, max_manifest_segments=3)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 20)
    expire(db, *sent[1::2])  # every even seq gone
    manifest = begin(db, config, conversation_id)
    assert [item["seq"] for segment in manifest["segments"] for item in segment["items"]] == [1, 3, 5]
    assert manifest["absent"] == [{"from_seq": n, "to_seq": n, "reason": "expired"} for n in (2, 4, 6)]
    assert len(manifest["absent"]) <= config.indexer.max_manifest_segments
    assert manifest["window"] == {"from_seq": 1, "to_seq": 6}
    db.close()


def test_a_batch_reads_a_bounded_number_of_rows(tmp_path: Path, monkeypatch) -> None:
    config = index_config(tmp_path, max_manifest_items=2)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 30)
    loaded = []
    real = derived._present

    def counting(*args, **kwargs):
        rows = real(*args, **kwargs)
        loaded.append(len(rows))
        return rows

    monkeypatch.setattr(derived, "_present", counting)
    begin(db, config, conversation_id)
    assert loaded and max(loaded) <= config.indexer.max_manifest_items + 1  # never the whole history
    db.close()


def test_a_full_commit_is_valid_and_a_replay_reports_the_state_now(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    first = cycle(db, config, conversation_id)
    assert [v["state"] for v in first["versions"]] == ["valid"] and first["operation"] == "commit_batch"
    say(db, config, conversation_id, "one more")
    manifest_id = db.execute("SELECT batch_id FROM artifact_versions").fetchone()[0]
    replay = derived.commit_batch(db, config, "alpha", manifest_id,
                                  {"request_id": "r-1", "artifacts": [
                                      {"kind": "summary", "body": "summary of the release plan", "citations": [],
                                       "expected_version": 0}]})
    assert replay["created"] is False and replay["operation"] == "commit_batch"
    assert [v["state"] for v in replay["versions"]] == ["behind"]  # current state, not the stored one
