"""Derived artifacts served to roles: read_artifacts and artifact hits in search.

An artifact version reaches a reader only if the reader can read every message it was made from, the same
rule `get_message` and `read_history` apply, checked in SQL before ranking. Only a kind's latest version is
served, only while valid, and only from the producer's active generation. Every result says it is derived
and not authoritative. Generic role and producer names; synthetic text only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_index_writer import artifacts_for, begin, commit, converse, expire, read_all, say, snapshot

from baton import config as config_module
from baton import conversations, derived, messages, search, store
from baton.config import Config, ConfigError
from baton.errors import BatonError
from baton.store import connect

ROLES = {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
         "reviewer": {}, "assistant": {}, "outsider": {}, "late": {},
         "viewer": {"can_view_all_conversations": True}}


def reads_config(tmp_path: Path, *, search_on: bool = True, rolling: int = 0, **indexer: Any) -> Config:
    search_table: dict[str, Any] = {"enabled": search_on}
    if rolling:
        search_table["rolling_window_messages"] = rolling
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": ROLES, "conversations": {"enabled": True}, "search": search_table,
        "indexer": {"producers": ["alpha", "beta"], **indexer},
    })


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return reads_config(tmp_path)


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


def publish(db, config, conversation_id: str, kinds=("summary",), producer: str = "alpha", request_id: str = "r-1",
            body: str | None = None) -> dict[str, Any]:
    manifest = begin(db, config, conversation_id, kinds, producer)
    read_all(db, config, manifest, producer)
    overrides = {"body": body} if body is not None else {}
    return commit(db, config, manifest, artifacts_for(manifest, **overrides), request_id=request_id,
                  producer=producer)


def shown(db, config, role: str, conversation_id: str, producer: str | None = None) -> list[tuple[str, str, int]]:
    artifacts = search.read_artifacts(db, config, role, conversation_id, producer=producer)["artifacts"]
    return sorted((a["producer"], a["kind"], a["version"]) for a in artifacts)


def artifact_hits(result: dict[str, Any]) -> list[str]:
    return [hit["version_id"] for hit in result["hits"] if hit["derived"]]


# read_artifacts ---------------------------------------------------------------------------------------------


def test_1_two_producers_keep_their_own_and_readers_see_each_labelled(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id, producer="alpha")
    publish(db, config, conversation_id, producer="beta")
    assert shown(db, config, "assistant", conversation_id) == [("alpha", "summary", 1), ("beta", "summary", 1)]
    assert shown(db, config, "outsider", conversation_id) == []


def test_every_artifact_says_it_is_derived_and_how_current_it_is(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    publish(db, config, conversation_id, kinds=("summary", "decisions"))
    [first, _] = search.read_artifacts(db, config, "assistant", conversation_id)["artifacts"]
    assert (first["derived"], first["authoritative"], first["scope_id"]) == (True, False, "all")
    assert (first["kind"], first["state"], first["covered_through_seq"], first["version"]) == (
        "decisions", "valid", 2, 1)
    assert first["body"] == "decisions of the release plan" and first["citations"] == []
    assert first["source_revision"] == db.execute("SELECT revision FROM conversations").fetchone()[0]
    assert first["generation"].startswith("gen_") and first["version_id"].startswith("art_")
    say(db, config, conversation_id, "one more")
    assert {a["state"] for a in search.read_artifacts(db, config, "assistant", conversation_id)["artifacts"]} \
        == {"behind"}  # still valid and served, and says it doesn't cover the newest message


def test_2_an_invalid_latest_version_is_served_nowhere(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    publish(db, config, conversation_id, request_id="r-1", body="heron first")
    third = say(db, config, conversation_id, "note 3")
    publish(db, config, conversation_id, request_id="r-2", body="heron second")
    expire(db, third)  # only the second version had seq 3: the first stays valid, and still isn't served
    states = [row[0] for row in db.execute("SELECT state FROM artifact_versions ORDER BY version")]
    assert states == ["valid", "invalid"]
    assert shown(db, config, "assistant", conversation_id) == []
    assert artifact_hits(search.search(db, config, "viewer", "heron", kinds=["summary"])) == []


def test_3_artifact_visibility_matches_raw_reads_for_every_role(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    publish(db, config, conversation_id)
    conversations.add(db, config, actor="orchestrator", conversation_id=conversation_id, participant="late")
    conversations.remove(db, config, actor="orchestrator", conversation_id=conversation_id, participant="reviewer")
    assert [row[0] for row in db.execute("SELECT state FROM artifact_versions")] == ["valid"]  # nothing flipped
    for role in ROLES:
        def can_read(message_id: str, role: str = role) -> bool:
            try:
                search.get_message(db, config, role, message_id)
                return True
            except BatonError:
                return False

        raw = all(can_read(message_id) for message_id in sent)
        assert bool(shown(db, config, role, conversation_id)) == raw, role
    assert shown(db, config, "late", conversation_id) == []  # joined after every input
    assert shown(db, config, "reviewer", conversation_id) == []  # removed
    assert shown(db, config, "viewer", conversation_id) == [("alpha", "summary", 1)]


def test_a_reader_missing_one_input_sees_nothing(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    conversations.add(db, config, actor="orchestrator", conversation_id=conversation_id, participant="late")
    say(db, config, conversation_id, "after the join")
    publish(db, config, conversation_id)  # inputs 1-3; late holds a copy of 3 only
    assert shown(db, config, "late", conversation_id) == []
    assert artifact_hits(search.search(db, config, "late", "summary", kinds=["summary"])) == []


def test_22_the_producer_filter_never_authorizes(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id, producer="alpha", body="otter alpha")
    publish(db, config, conversation_id, producer="beta", body="otter beta")
    assert shown(db, config, "assistant", conversation_id, producer="alpha") == [("alpha", "summary", 1)]
    assert shown(db, config, "outsider", conversation_id, producer="alpha") == []
    hits = search.search(db, config, "assistant", "otter", kinds=["summary"], producer="beta")["hits"]
    assert [hit["producer"] for hit in hits] == ["beta"]
    assert search.search(db, config, "outsider", "otter", kinds=["summary"], producer="beta")["hits"] == []


def test_an_unknown_conversation_reads_as_empty(db, config) -> None:
    assert search.read_artifacts(db, config, "assistant", "conv_missing")["artifacts"] == []


# Artifact hits in search ---------------------------------------------------------------------------------


def test_artifact_search_authorizes_before_ranking(db, config) -> None:
    private, _ = converse(db, config, 1, participants=["orchestrator", "reviewer"])
    shared, _ = converse(db, config, 1, participants=["orchestrator", "assistant"])
    perfect = publish(db, config, private, body="zebra zebra zebra zebra", request_id="r-p")
    weaker = publish(db, config, shared, body="a zebra once", request_id="r-s")
    hidden, seen = perfect["versions"][0]["version_id"], weaker["versions"][0]["version_id"]
    assert artifact_hits(search.search(db, config, "assistant", "zebra", kinds=["summary"], limit=1)) == [seen]
    assert set(artifact_hits(search.search(db, config, "viewer", "zebra", kinds=["summary"]))) == {hidden, seen}
    assert artifact_hits(search.search(db, config, "outsider", "zebra", kinds=["summary"])) == []


def test_search_returns_messages_and_artifacts_each_labelled(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    say(db, config, conversation_id, "the walrus plan")
    publish(db, config, conversation_id, body="walrus summary")
    hits = search.search(db, config, "assistant", "walrus", kinds=["message", "summary"])["hits"]
    message_hits = [hit for hit in hits if not hit["derived"]]
    artifact = [hit for hit in hits if hit["derived"]]
    assert len(message_hits) == 1 and len(artifact) == 1
    assert (artifact[0]["kind"], artifact[0]["authoritative"], artifact[0]["producer"], artifact[0]["state"]) \
        == ("summary", False, "alpha", "valid")
    assert artifact[0]["conversation_id"] == conversation_id and "walrus" in artifact[0]["snippet"]
    assert search.search(db, config, "assistant", "walrus")["hits"] == message_hits  # messages by default


def test_artifact_search_without_fts_authorizes_the_same(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "fts5_available", lambda conn: False)
    config = reads_config(tmp_path)
    db = connect(config.db_path)
    assert search.keyword_mode(db) == "like"
    private, _ = converse(db, config, 1, participants=["orchestrator", "reviewer"])
    shared, _ = converse(db, config, 1, participants=["orchestrator", "assistant"])
    hidden = publish(db, config, private, body="narwhal private", request_id="r-p")["versions"][0]["version_id"]
    seen = publish(db, config, shared, body="narwhal shared", request_id="r-s")["versions"][0]["version_id"]
    assert artifact_hits(search.search(db, config, "assistant", "narwhal", kinds=["summary"])) == [seen]
    assert hidden in artifact_hits(search.search(db, config, "viewer", "narwhal", kinds=["summary"]))
    db.close()


def test_search_refuses_a_kind_that_does_not_exist(db, config) -> None:
    refused("unsupported_kind", search.search, db, config, "assistant", "x", kinds=["vectors"])
    refused("unsupported_kind", search.search, db, config, "assistant", "x", kinds=[])


def test_9_artifact_reads_consume_nothing(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id, body="badger summary")
    unread = {role: messages.unread(db, role) for role in ROLES}
    before = snapshot(db)
    for _ in range(3):
        search.read_artifacts(db, config, "assistant", conversation_id)
        search.search(db, config, "assistant", "badger", kinds=["message", "summary"])
    assert snapshot(db) == before
    assert {role: messages.unread(db, role) for role in ROLES} == unread


# The rolling window ---------------------------------------------------------------------------------------


def test_20_a_recent_summary_is_labelled_with_its_bounds_and_covers_nothing_else(tmp_path: Path) -> None:
    config = reads_config(tmp_path, rolling=3)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 6)
    manifest = begin(db, config, conversation_id, kinds=("summary_recent",))
    assert manifest["window"] == {"from_seq": 4, "to_seq": 6}
    read_all(db, config, manifest)
    commit(db, config, manifest)
    [recent] = search.read_artifacts(db, config, "assistant", conversation_id)["artifacts"]
    assert (recent["kind"], recent["window"], recent["from_seq"], recent["covered_through_seq"]) \
        == ("summary_recent", "recent", 4, 6)
    pairs = {(p["conversation_id"], p["kind"]): p for p in derived.dirty(db, config, "alpha")["pairs"]}
    assert pairs[(conversation_id, "summary")]["covered_through_seq"] == 0  # the full summary is untouched
    assert (conversation_id, "summary_recent") not in pairs
    say(db, config, conversation_id, "note 7")
    later = begin(db, config, conversation_id, kinds=("summary_recent",))
    assert later["window"] == {"from_seq": 5, "to_seq": 7} and later["kinds"]["summary_recent"]["prior_version_id"] \
        is None
    db.close()


def test_the_rolling_window_is_off_by_default_and_bounded(tmp_path: Path, db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    refused("invalid_request", begin, db, config, conversation_id, kinds=("summary_recent",))
    refused("invalid_request", begin, db, reads_config(tmp_path, rolling=3), conversation_id,
            kinds=("summary_recent", "summary"))  # a recent window is its own batch
    with pytest.raises(ConfigError, match="rolling_window_messages"):
        reads_config(tmp_path, rolling=11, max_manifest_items=10)


# The MCP tool -----------------------------------------------------------------------------------------------


def test_read_artifacts_is_a_read_only_tool_present_with_search(tmp_path: Path, config) -> None:
    from baton import app
    assert [(name, read_only) for name, _, read_only in app.ARTIFACT_TOOLS] == [("read_artifacts", True)]
    assert reads_config(tmp_path, search_on=False).search.enabled is False
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 1)
    db.close()
    assert app.Tools(config).read_artifacts("assistant", conversation_id) == {"artifacts": []}


def test_a_version_made_from_no_message_is_served_to_no_one(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    expire(db, *sent)  # every message gone: a batch over holes alone
    manifest = begin(db, config, conversation_id)
    assert manifest["item_count"] == 0
    commit(db, config, manifest)
    assert db.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0] == 1
    assert shown(db, config, "viewer", conversation_id) == []  # not visible by vacuous truth


def test_only_an_active_generation_is_served(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id)
    db.execute("UPDATE generations SET active = 0")
    assert shown(db, config, "viewer", conversation_id) == []


def test_an_invalid_version_is_hidden_even_with_every_input_still_held(db, config) -> None:
    """Expiry is today's only invalidation, and it also removes an input; the reserved edit and delete
    invalidations would leave every input in place, and must hide the version just the same."""
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id, body="ibis summary")
    db.execute("UPDATE artifact_versions SET state = 'invalid'")
    assert shown(db, config, "viewer", conversation_id) == []
    assert artifact_hits(search.search(db, config, "viewer", "ibis", kinds=["summary"])) == []


# Review round: a recent window that reaches the newest message, and complete labels ---------------------------


def test_a_fragmented_recent_window_reaches_the_newest_message(tmp_path: Path) -> None:
    config = reads_config(tmp_path, rolling=3, max_manifest_segments=1)
    db = connect(config.db_path)
    conversation_id, sent = converse(db, config, 5)
    expire(db, sent[1], sent[3])  # retained seqs 1, 3 and 5: one segment holds only one of them
    manifest = begin(db, config, conversation_id, kinds=("summary_recent",))
    assert manifest["window"] == {"from_seq": 5, "to_seq": 5}
    assert [item["seq"] for segment in manifest["segments"] for item in segment["items"]] == [5]
    read_all(db, config, manifest)
    result = commit(db, config, manifest)
    assert [v["state"] for v in result["versions"]] == ["valid"]
    with pytest.raises(BatonError) as again:  # current, not the old prefix again
        begin(db, config, conversation_id, kinds=("summary_recent",))
    assert again.value.code == "up_to_date"
    db.close()


def test_artifact_hits_carry_the_provenance_labels(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    publish(db, config, conversation_id, body="puffin summary")
    [hit] = search.search(db, config, "assistant", "puffin", kinds=["summary"])["hits"]
    [artifact] = search.read_artifacts(db, config, "assistant", conversation_id)["artifacts"]
    for field in ("scope_id", "generation", "source_revision", "version", "state", "covered_through_seq"):
        assert hit[field] == artifact[field], field


def test_a_recent_summary_names_both_ends_of_its_window_everywhere(tmp_path: Path) -> None:
    config = reads_config(tmp_path, rolling=3)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 6)
    publish(db, config, conversation_id, kinds=("summary_recent",), body="gannet recent")
    [artifact] = search.read_artifacts(db, config, "assistant", conversation_id)["artifacts"]
    [hit] = search.search(db, config, "assistant", "gannet", kinds=["summary_recent"])["hits"]
    for shown_as in (artifact, hit):
        assert (shown_as["window"], shown_as["from_seq"], shown_as["to_seq"]) == ("recent", 4, 6)
    db.close()


def test_the_kinds_help_names_the_recent_window(capsys) -> None:
    from baton import cli
    with pytest.raises(SystemExit):
        cli.main(["index", "begin-batch", "--help"])
    assert "summary_recent" in capsys.readouterr().out


def test_before_filters_messages_by_send_time_and_artifacts_by_when_written(db, config) -> None:
    conversation_id, sent = converse(db, config, 1)
    say(db, config, conversation_id, "kestrel message")
    publish(db, config, conversation_id, body="kestrel summary")
    message_id = db.execute("SELECT id FROM messages WHERE text = 'kestrel message' LIMIT 1").fetchone()[0]

    def kinds_found(before: str) -> set[str]:
        hits = search.search(db, config, "assistant", "kestrel", kinds=["message", "summary"], before=before)["hits"]
        return {"artifact" if hit["derived"] else "message" for hit in hits}

    db.execute("UPDATE messages SET at = '2026-01-10T00:00:00+00:00' WHERE id = ?", (message_id,))
    db.execute("UPDATE artifact_versions SET created_at = '2026-01-20T00:00:00+00:00'")
    assert kinds_found("2026-01-15T00:00:00Z") == {"message"}
    db.execute("UPDATE messages SET at = '2026-01-20T00:00:00+00:00' WHERE id = ?", (message_id,))
    db.execute("UPDATE artifact_versions SET created_at = '2026-01-10T00:00:00+00:00'")
    assert kinds_found("2026-01-15T00:00:00Z") == {"artifact"}


def test_the_before_help_says_which_time_each_kind_is_compared_by() -> None:
    import inspect

    from baton import app
    annotation = inspect.signature(app.Tools.search).parameters["before"].annotation
    description = " ".join(str(getattr(meta, "description", "")) for meta in getattr(annotation, "__metadata__", ()))
    assert "sent" in description and "written" in description, description


def test_contiguous_recent_messages_fill_one_segment(tmp_path: Path) -> None:
    config = reads_config(tmp_path, rolling=3, max_manifest_segments=1)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 6)
    manifest = begin(db, config, conversation_id, kinds=("summary_recent",))
    assert manifest["window"] == {"from_seq": 4, "to_seq": 6}  # three messages, one range
    db.close()
