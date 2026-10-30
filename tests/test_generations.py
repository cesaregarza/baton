"""Generations: create one, fill it, and switch readers to it only when it is complete; or drop it.

Contract r2 §6, r3 §5, r4 §1/§3/§4 with Delta r4.1 (a pair is current when its latest version is valid and
covers through last_seq; source_revision is a label), and the designer's slice 3 rulings: a currently empty
conversation is required by nothing, and `generation drop` frees a slot at once. Generic names only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_artifact_reads import reads_config, shown
from test_index_writer import artifacts_for, commit, converse, expire, read_all, say

from baton import conversations, derived, store
from baton.errors import BatonError
from baton.store import connect


@pytest.fixture
def config(tmp_path: Path):
    return reads_config(tmp_path)


@pytest.fixture
def db(config):
    connection = connect(config.db_path)
    yield connection
    connection.close()


def refused(code: str, operation, *args, **kwargs) -> BatonError:
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    assert raised.value.code == code, f"{raised.value.code}: {raised.value}"
    return raised.value


def fill(db, config, conversation_id: str, generation: str | None = None, kinds=derived.KINDS,
         producer: str = "alpha", request_id: str | None = None, body: str | None = None) -> dict[str, Any]:
    """One batch over every kind asked for, read in full and committed."""
    manifest = derived.begin_batch(db, config, producer, conversation_id, list(kinds), generation=generation)
    read_all(db, config, manifest, producer)
    overrides = {"body": body} if body else {}
    return commit(db, config, manifest, artifacts_for(manifest, **overrides),
                  request_id=request_id or manifest["batch_id"], producer=producer)


def create(db, config, producer: str = "alpha", profile: dict | None = None) -> str:
    return derived.generation_create(db, config, producer, profile)["generation"]


def activate(db, config, generation: str, producer: str = "alpha") -> dict[str, Any]:
    return derived.generation_activate(db, config, producer, generation)


def active(db, producer: str = "alpha") -> str | None:
    row = db.execute("SELECT id FROM generations WHERE producer = ? AND active = 1", (producer,)).fetchone()
    return row[0] if row else None


def served_generation(db, config, conversation_id: str) -> set[str]:
    from baton import search
    return {a["generation"] for a in search.read_artifacts(db, config, "viewer", conversation_id)["artifacts"]}


def pairs(db, config, producer: str = "alpha", generation: str | None = None) -> dict[tuple[str, str], Any]:
    listed = derived.dirty(db, config, producer, generation=generation)["pairs"]
    return {(p["conversation_id"], p["kind"]): p for p in listed}


# Creating ---------------------------------------------------------------------------------------------------


def test_a_new_generation_is_inactive_with_its_watermark_and_profile(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    fill(db, config, conversation_id)  # the implicit generation, active
    created = derived.generation_create(db, config, "alpha", {"summarizer": "small-model-v2"})
    row = dict(db.execute("SELECT * FROM generations WHERE id = ?", (created["generation"],)).fetchone())
    last = db.execute("SELECT MAX(change_seq) FROM change_log").fetchone()[0]
    assert (row["active"], row["watermark_seq"], row["watermark_epoch"]) == (0, last, derived_epoch(db))
    assert created["profile"] == {"summarizer": "small-model-v2"} and created["active"] is False
    assert active(db) != created["generation"]


def derived_epoch(db) -> str:
    return db.execute("SELECT value FROM log_meta WHERE key = 'log_epoch'").fetchone()[0]


@pytest.mark.parametrize("profile", [
    {"colour": "blue"},
    {"embedding": {"model": "m", "dim": 2048, "normalization": "unit", "distance": "cosine"}},
    {"embedding": {"model": "m", "dim": 8, "normalization": "sometimes", "distance": "cosine"}},
    {"embedding": {"model": "", "dim": 8, "normalization": "unit", "distance": "cosine"}},
    {"summarizer": 7},
])
def test_a_profile_is_checked_when_the_generation_is_made(db, config, profile) -> None:
    refused("invalid_request", derived.generation_create, db, config, "alpha", profile)


def test_a_producer_holds_at_most_max_generations_and_a_drop_frees_a_slot_at_once(tmp_path: Path) -> None:
    config = reads_config(tmp_path, max_generations=2)
    db = connect(config.db_path)
    first, second = create(db, config), create(db, config)
    assert refused("limit_exceeded", create, db, config).extra["limit"] == "max_generations"
    derived.generation_drop(db, config, "alpha", first)
    assert create(db, config) not in (first, second)
    create(db, config, producer="beta")  # another producer's own slots
    db.close()


# Filling and switching --------------------------------------------------------------------------------------


def test_a_generation_fills_while_the_active_one_keeps_serving_then_switches_whole(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    fill(db, config, conversation_id)
    old = active(db)
    new = create(db, config)
    assert {kind for (_, kind), p in pairs(db, config, generation=new).items() if p["state"] == "none"} \
        == set(derived.KINDS)
    fill(db, config, conversation_id, generation=new, kinds=("summary", "decisions"))
    assert served_generation(db, config, conversation_id) == {old}  # still the old one, while filling
    refused("generation_incomplete", activate, db, config, new)  # open_questions not yet in it
    assert active(db) == old
    fill(db, config, conversation_id, generation=new, kinds=("open_questions",))
    result = activate(db, config, new)
    assert (result["generation"], result["previous"], result["mode"]) == (new, old, "watermark")
    assert active(db) == new and served_generation(db, config, conversation_id) == {new}
    assert activate(db, config, new)["previous"] == new  # activating the active one again changes nothing


def test_15_two_producers_each_keep_their_own_generations(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    mine, theirs = create(db, config, "alpha"), create(db, config, "beta")
    refused("forbidden", derived.begin_batch, db, config, "beta", conversation_id, ["summary"], generation=mine)
    refused("forbidden", activate, db, config, mine, producer="beta")
    refused("forbidden", derived.generation_drop, db, config, "beta", mine)
    fill(db, config, conversation_id, generation=mine)
    fill(db, config, conversation_id, generation=theirs, producer="beta")
    activate(db, config, mine)
    activate(db, config, theirs, producer="beta")
    assert served_generation(db, config, conversation_id) == {mine, theirs}
    assert {p for p, _, _ in shown(db, config, "viewer", conversation_id)} == {"alpha", "beta"}


def test_19_a_pair_held_by_the_provenance_bound_keeps_activation_incomplete(tmp_path: Path) -> None:
    config = reads_config(tmp_path, max_input_segments=2)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 6)
    expire(db, sent[1], sent[3])  # inputs 1, 3, 5-6 would take three ranges
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)  # covers 1-4 within two ranges, then can't go on
    error = refused("generation_incomplete", activate, db, config, new)
    assert {(p["conversation_id"], p["kind"]) for p in error.extra["pairs"]} == \
        {(conversation_id, kind) for kind in derived.KINDS}
    assert active(db) != new
    db.close()


def test_18_a_change_after_the_watermark_must_be_covered_before_switching(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    say(db, config, conversation_id, "after the watermark")
    error = refused("generation_behind", activate, db, config, new)
    assert error.extra["pairs"] and active(db) != new
    fill(db, config, conversation_id, generation=new)  # covers the new message
    assert activate(db, config, new)["generation"] == new


def test_r4_1_a_participant_change_after_the_watermark_needs_no_new_batch(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    conversations.add(db, config, actor="orchestrator", conversation_id=conversation_id, participant="late")
    conversations.remove(db, config, actor="orchestrator", conversation_id=conversation_id, participant="reviewer")
    with pytest.raises(BatonError) as nothing_new:  # the revision moved, the coverage didn't
        derived.begin_batch(db, config, "alpha", conversation_id, ["summary"], generation=new)
    assert nothing_new.value.code == "up_to_date"
    assert activate(db, config, new)["generation"] == new


def test_an_empty_log_never_reads_as_complete(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)  # nothing after the watermark, and nothing in the generation
    refused("generation_incomplete", activate, db, config, new)


# Reconciling from state ---------------------------------------------------------------------------------------


def age_out_the_watermark(db) -> None:
    """Retention has pruned past every watermark taken so far."""
    floor = db.execute("SELECT MAX(change_seq) FROM change_log").fetchone()[0]
    db.execute("DELETE FROM change_log WHERE change_seq <= ?", (floor,))
    db.execute("UPDATE log_meta SET value = ? WHERE key = 'pruned_through'", (str(floor),))


def test_23_an_aged_out_watermark_reconciles_from_state(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    age_out_the_watermark(db)
    assert activate(db, config, new)["mode"] == "reconcile"


def test_23_reconciling_after_a_participant_change_succeeds(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    conversations.add(db, config, actor="orchestrator", conversation_id=conversation_id, participant="late")
    age_out_the_watermark(db)
    assert activate(db, config, new)["mode"] == "reconcile"


def test_23_reconciling_refuses_a_pair_that_is_not_current(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    say(db, config, conversation_id, "uncovered")
    age_out_the_watermark(db)
    error = refused("generation_behind", activate, db, config, new)
    assert {p["kind"] for p in error.extra["pairs"]} == set(derived.KINDS)


def test_23_another_epoch_reconciles_too(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    db.execute("UPDATE log_meta SET value = ? WHERE key = 'log_epoch'", ("f" * 32,))
    assert activate(db, config, new)["mode"] == "reconcile"


def test_23_a_reconcile_over_the_bound_fails_closed(tmp_path: Path) -> None:
    config = reads_config(tmp_path, max_activation_reconcile=2)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)  # three pairs, over a bound of two
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    age_out_the_watermark(db)
    error = refused("reconcile_required", activate, db, config, new)
    assert error.extra["pairs"] == 3
    db.close()


# Currently empty conversations ---------------------------------------------------------------------------


def test_a_conversation_emptied_before_activation_is_required_by_nothing(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)  # valid versions of all three kinds
    expire(db, *sent)  # every copy goes
    assert {row[0] for row in db.execute("SELECT state FROM artifact_versions WHERE generation = ?", (new,))} \
        == {"invalid"}
    assert not [key for key in pairs(db, config, generation=new) if key[0] == conversation_id]
    refused("nothing_to_index", derived.begin_batch, db, config, "alpha", conversation_id, ["summary"],
            generation=new)
    versions_before = db.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0]
    assert activate(db, config, new)["generation"] == new  # nothing committed for it, empty or otherwise
    assert db.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0] == versions_before
    newer = create(db, config)
    fill_others = [key for key in pairs(db, config, generation=newer) if key[0] == conversation_id]
    assert fill_others == []
    say(db, config, conversation_id, "back again")  # required again
    manifest = derived.begin_batch(db, config, "alpha", conversation_id, ["summary"], generation=new)
    plan = manifest["kinds"]["summary"]
    assert (plan["rebase"], plan["start_seq"]) == (True, 1)  # rebuilt from seq 1, the expired seqs absent
    assert manifest["absent"] == [{"from_seq": 1, "to_seq": 2, "reason": "expired"}]


def test_a_new_message_makes_an_emptied_conversation_required_again(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    expire(db, *sent)
    say(db, config, conversation_id, "back again")
    refused("generation_behind", activate, db, config, new)
    fill(db, config, conversation_id, generation=new)
    assert activate(db, config, new)["generation"] == new


def test_trailing_seqs_with_no_copy_are_covered_by_a_window_of_holes(db, config) -> None:
    conversation_id, sent = converse(db, config, 4)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)  # covers 1-4
    tail = [say(db, config, conversation_id, f"tail {n}") for n in (5, 6)]
    expire(db, *tail)  # the newest seqs have no copy; earlier ones still do
    manifest = derived.begin_batch(db, config, "alpha", conversation_id, list(derived.KINDS), generation=new)
    assert manifest["item_count"] == 0 and manifest["window"] == {"from_seq": 5, "to_seq": 6}
    read_all(db, config, manifest)
    commit(db, config, manifest)
    assert activate(db, config, new)["generation"] == new


# Dropping -----------------------------------------------------------------------------------------------


def test_drop_takes_only_an_owned_inactive_generation(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    fill(db, config, conversation_id)
    implicit = active(db)
    refused("generation_active", derived.generation_drop, db, config, "alpha", implicit)
    new = create(db, config)
    refused("forbidden", derived.generation_drop, db, config, "beta", new)
    fill(db, config, conversation_id, generation=new)
    activate(db, config, new)
    derived.generation_drop(db, config, "alpha", implicit)  # droppable once another is active
    refused("generation_dropped", activate, db, config, implicit)


def test_a_dropped_generation_takes_no_batch_and_its_open_batches_close(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    manifest = derived.begin_batch(db, config, "alpha", conversation_id, ["summary"], generation=new)
    read_all(db, config, manifest)
    derived.generation_drop(db, config, "alpha", new)
    refused("batch_closed", commit, db, config, manifest)
    refused("generation_dropped", derived.begin_batch, db, config, "alpha", conversation_id, ["summary"],
            generation=new)
    refused("generation_dropped", activate, db, config, new)


def test_a_dropped_generation_s_rows_go_in_bounded_sweeps_and_receipts_say_invalid(tmp_path: Path) -> None:
    config = reads_config(tmp_path, cleanup_batch=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new, request_id="r-drop")
    derived.generation_drop(db, config, "alpha", new)
    held = lambda: db.execute("SELECT COUNT(*) FROM artifact_versions WHERE generation = ?",  # noqa: E731
                              (new,)).fetchone()[0]
    assert held() == 3
    other, _ = converse(db, config, 1)
    derived.begin_batch(db, config, "alpha", other, ["summary"])  # one writer call sweeps at most one version
    assert held() == 2
    replay = commit(db, config, {"batch_id": receipt_batch(db, "r-drop"), "kinds": {}}, [
        {"kind": kind, "body": f"{kind} of the release plan", "citations": [], "expected_version": 0}
        for kind in derived.KINDS], request_id="r-drop")
    assert {v["state"] for v in replay["versions"]} == {"invalid"} and replay["created"] is False
    db.close()


def receipt_batch(db, request_id: str) -> str:
    import json
    receipt = json.loads(db.execute("SELECT receipt FROM commit_requests WHERE request_id = ?",
                                    (request_id,)).fetchone()[0])
    version_id = receipt["versions"][0]["version_id"]
    return db.execute("SELECT batch_id FROM artifact_versions WHERE id = ?", (version_id,)).fetchone()[0]


def test_retention_sweeps_a_replaced_generation_and_never_the_active_one(tmp_path: Path) -> None:
    config = reads_config(tmp_path, generation_retention_days=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)
    fill(db, config, conversation_id)
    implicit = active(db)
    new = create(db, config)
    fill(db, config, conversation_id, generation=new)
    activate(db, config, new)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.execute("UPDATE generations SET deactivated_at = ? WHERE id = ?", (old, implicit))
    db.execute("UPDATE generations SET created_at = ?", (old,))
    other, _ = converse(db, config, 1)
    fill(db, config, other)  # a writer call sweeps
    assert db.execute("SELECT COUNT(*) FROM artifact_versions WHERE generation = ?", (implicit,)).fetchone()[0] == 0
    assert db.execute("SELECT active FROM generations WHERE id = ?", (new,)).fetchone()[0] == 1
    assert served_generation(db, config, conversation_id) == {new}
    db.close()


# dirty and the read-only path ------------------------------------------------------------------------------


def test_dirty_for_a_generation_creates_nothing_and_omits_empty_conversations(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    emptied, gone = converse(db, config, 1)
    expire(db, *gone)
    new = create(db, config)
    before = db.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
    listed = pairs(db, config, generation=new)
    assert {key[0] for key in listed} == {conversation_id}  # the emptied conversation isn't listed
    assert db.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == before


def test_begin_batch_refuses_a_generation_it_cannot_use(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    refused("unknown_generation", derived.begin_batch, db, config, "alpha", conversation_id, ["summary"],
            generation="gen_missing")


# Schema --------------------------------------------------------------------------------------------------


def test_step_11_adds_the_generation_columns_and_the_vector_tables(db) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(generations)")}
    assert {"watermark_epoch", "watermark_seq", "activated_at", "deactivated_at", "dropped_at"} <= columns
    assert set(store._ADDED_TABLES[store.SCHEMA_VERSION]) == {"vectors", "vector_skips"}
