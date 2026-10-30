"""Vectors a producer supplies with its commits, and keyword-first hybrid search over them.

Contract r2 §6, r3 §5 and the designer's slice 3 rulings: Baton stores vectors and never computes them; in a
generation whose profile embeds, a commit is refused whole unless every kind it writes and every message it
served (that holds no vector or skip in the generation yet) has a vector or a checked skip; search stays
keyword-first, re-scoring only candidates the caller may read. Toy three-dimensional vectors only.
"""

from __future__ import annotations

import math
import random
import struct
from pathlib import Path
from typing import Any

import pytest
from test_index_writer import converse, expire, read_all, say

from baton import config as config_module
from baton import derived, search
from baton.config import Config
from baton.errors import BatonError
from baton.store import connect

ROLES = {"orchestrator": {"can_create_conversations": True, "can_manage_participants": True},
         "reviewer": {}, "assistant": {}, "outsider": {}, "viewer": {"can_view_all_conversations": True}}
PROFILE = {"embedding": {"model": "toy-embed", "dim": 3, "normalization": "unit", "distance": "cosine"}}
UNIT = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]}


def vectors_config(tmp_path: Path, *, embeddings: bool = True, **indexer: Any) -> Config:
    return config_module.parse({
        "host": {"public_url": "http://127.0.0.1:8765", "data_dir": str(tmp_path / "data")},
        "roles": ROLES, "conversations": {"enabled": True},
        "search": {"enabled": True, "embeddings": embeddings},
        "indexer": {"producers": ["alpha", "beta"], **indexer},
    })


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return vectors_config(tmp_path)


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


def embedding_generation(db, config, producer: str = "alpha") -> str:
    return derived.generation_create(db, config, producer, PROFILE)["generation"]


def items(manifest: dict[str, Any]) -> list[str]:
    return [item["message_id"] for segment in manifest["segments"] for item in segment["items"]]


def payload(manifest: dict[str, Any], *, request_id: str = "r-1", body: str = "a toy summary",
            vectors: dict | None = None, skips: list | None = None, kinds=None) -> dict[str, Any]:
    """Artifacts for the batch's kinds, and by default a vector for each kind and each message it served."""
    kinds = kinds or list(manifest["kinds"])
    if vectors is None:
        vectors = {**{("kind", kind): UNIT["x"] for kind in kinds},
                   **{("message_id", message_id): UNIT["y"] for message_id in items(manifest)}}
    return {"request_id": request_id,
            "artifacts": [{"kind": kind, "body": body, "citations": [],
                           "expected_version": manifest["kinds"][kind]["prior_version"]} for kind in kinds],
            "vectors": [{"item": {field: value}, "vector": vector} for (field, value), vector in vectors.items()],
            "skips": skips or []}


def begin(db, config, conversation_id: str, generation: str, kinds=("summary",), producer: str = "alpha"):
    manifest = derived.begin_batch(db, config, producer, conversation_id, list(kinds), generation=generation)
    read_all(db, config, manifest, producer)
    return manifest


def commit(db, config, manifest, body=None, producer: str = "alpha"):
    return derived.commit_batch(db, config, producer, manifest["batch_id"], body or payload(manifest))


def vector_rows(db, generation: str) -> dict[tuple[str, str], list[float]]:
    rows = db.execute("SELECT item_kind, item_id, dim, data FROM vectors WHERE generation = ?", (generation,))
    return {(row[0], row[1]): list(struct.unpack(f"<{row[2]}f", row[3])) for row in rows}


def skip_rows(db, generation: str) -> dict[tuple[str, str], str]:
    rows = db.execute("SELECT item_kind, item_id, reason FROM vector_skips WHERE generation = ?", (generation,))
    return {(row[0], row[1]): row[2] for row in rows}


def fill(db, config, conversation_id: str, generation: str, kinds=derived.KINDS, producer: str = "alpha",
         request_id: str | None = None) -> dict[str, Any]:
    manifest = begin(db, config, conversation_id, generation, kinds, producer)
    return commit(db, config, manifest, payload(manifest, request_id=request_id or manifest["batch_id"]), producer)


# Storing vectors ----------------------------------------------------------------------------------------------


def test_a_commit_stores_one_vector_per_kind_and_per_served_message(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    result = commit(db, config, manifest)
    [version] = result["versions"]
    stored = vector_rows(db, generation)
    assert stored[("artifact", version["version_id"])] == UNIT["x"]  # an artifact's vector belongs to its version
    assert {key for key in stored if key[0] == "message"} == {("message", message_id) for message_id in sent}
    assert db.execute("SELECT DISTINCT model FROM vectors").fetchall()[0][0] == "toy-embed"


def test_content_ok_vectors_missing_is_refused_whole_and_retryable(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    without = payload(manifest)
    without["vectors"] = [v for v in without["vectors"] if "kind" not in v["item"]]
    error = refused("vectors_incomplete", commit, db, config, manifest, without)
    assert "summary" in str(error)
    assert db.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0] == 0  # no version, no coverage
    assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    dirty = {(p["conversation_id"], p["kind"]): p for p in derived.dirty(db, config, "alpha", generation)["pairs"]}
    assert dirty[(conversation_id, "summary")]["state"] == "none"
    assert commit(db, config, manifest, payload(manifest))["created"] is True  # same request id, now complete


def test_a_missing_message_vector_is_refused_naming_it(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    short = payload(manifest)
    short["vectors"] = [v for v in short["vectors"] if v["item"].get("message_id") != sent[1]]
    assert sent[1] in str(refused("vectors_incomplete", commit, db, config, manifest, short))


def test_a_message_that_already_holds_a_vector_needs_no_resend_and_may_not_change(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    commit(db, config, begin(db, config, conversation_id, generation, kinds=("summary",)))
    again = begin(db, config, conversation_id, generation, kinds=("decisions",))  # serves the same messages
    only_kind = payload(again, request_id="r-2", vectors={("kind", "decisions"): UNIT["x"]})
    assert commit(db, config, again, only_kind)["created"] is True  # nothing to resend
    third = begin(db, config, conversation_id, generation, kinds=("open_questions",))
    identical = payload(third, request_id="r-3")
    assert commit(db, config, third, identical)["created"] is True  # an identical resend is a no-op
    assert vector_rows(db, generation)[("message", sent[0])] == UNIT["y"]
    say(db, config, conversation_id, "a third message")
    fourth = begin(db, config, conversation_id, generation, kinds=("summary",))
    changed = payload(fourth, request_id="r-4", vectors={("kind", "summary"): UNIT["x"],
                                                          ("message_id", sent[0]): UNIT["z"],
                                                          ("message_id", items(fourth)[-1]): UNIT["y"]})
    refused("invalid_request", commit, db, config, fourth, changed)  # sent[0] wasn't served by this batch
    sixth_vectors = {("kind", "summary"): UNIT["x"], ("message_id", items(fourth)[-1]): UNIT["y"]}
    assert commit(db, config, fourth, payload(fourth, request_id="r-5", vectors=sixth_vectors))["created"] is True


def test_a_changed_vector_for_a_message_already_held_conflicts(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    commit(db, config, begin(db, config, conversation_id, generation, kinds=("summary",)))
    again = begin(db, config, conversation_id, generation, kinds=("decisions",))
    changed = payload(again, request_id="r-2")
    for vector in changed["vectors"]:
        if vector["item"].get("message_id") == sent[0]:
            vector["vector"] = UNIT["z"]
    assert sent[0] in str(refused("vector_conflict", commit, db, config, again, changed))


def test_a_generation_without_an_embedding_profile_refuses_vectors(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    plain = derived.generation_create(db, config, "alpha", {"summarizer": "toy"})["generation"]
    manifest = begin(db, config, conversation_id, plain)
    refused("invalid_request", commit, db, config, manifest, payload(manifest))
    assert commit(db, config, manifest, payload(manifest, vectors={}))["created"] is True


def test_embedding_generations_need_embeddings_on(tmp_path: Path) -> None:
    config = vectors_config(tmp_path, embeddings=False)
    db = connect(config.db_path)
    refused("invalid_request", derived.generation_create, db, config, "alpha", PROFILE)
    db.close()


@pytest.mark.parametrize("bad", [[1.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.5, 0.5, 0.5], [float("nan"), 1.0, 0.0],
                                 [float("inf"), 0.0, 0.0], ["1", 0, 0], [True, 0, 0]])
def test_a_vector_that_does_not_fit_the_profile_is_refused(db, config, bad) -> None:
    conversation_id, _ = converse(db, config, 1)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    body = payload(manifest)
    body["vectors"][0]["vector"] = bad
    refused("invalid_request", commit, db, config, manifest, body)


def test_a_vector_for_an_item_outside_the_commit_is_refused(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    other, other_sent = converse(db, config, 1)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    stray = payload(manifest)
    stray["vectors"].append({"item": {"message_id": other_sent[0]}, "vector": UNIT["z"]})
    assert other_sent[0] in str(refused("invalid_request", commit, db, config, manifest, stray))
    unwritten = payload(manifest)
    unwritten["vectors"].append({"item": {"kind": "decisions"}, "vector": UNIT["z"]})
    refused("invalid_request", commit, db, config, manifest, unwritten)


def test_skips_are_checked_and_recorded(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    lying = payload(manifest, vectors={("kind", "summary"): UNIT["x"], ("message_id", sent[1]): UNIT["y"]},
                    skips=[{"item": {"message_id": sent[0]}, "reason": "empty_text"}])
    refused("invalid_request", commit, db, config, manifest, lying)  # its text isn't empty
    unknown = payload(manifest, vectors={("kind", "summary"): UNIT["x"], ("message_id", sent[1]): UNIT["y"]},
                      skips=[{"item": {"message_id": sent[0]}, "reason": "felt_like_it"}])
    refused("invalid_request", commit, db, config, manifest, unknown)
    honest = payload(manifest, body="", vectors={("message_id", sent[1]): UNIT["y"]},
                     skips=[{"item": {"message_id": sent[0]}, "reason": "over_token_limit"},
                            {"item": {"kind": "summary"}, "reason": "empty_text"}])  # the body really is empty
    [version] = commit(db, config, manifest, honest)["versions"]
    assert skip_rows(db, generation) == {("message", sent[0]): "over_token_limit",
                                         ("artifact", version["version_id"]): "empty_text"}


def test_a_vector_and_a_skip_for_one_item_is_refused(db, config) -> None:
    conversation_id, sent = converse(db, config, 1)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    both = payload(manifest, skips=[{"item": {"message_id": sent[0]}, "reason": "over_token_limit"}])
    refused("invalid_request", commit, db, config, manifest, both)


def test_the_request_digest_covers_vectors_and_skips(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    commit(db, config, manifest, payload(manifest))
    changed = payload(manifest)
    changed["vectors"][0]["vector"] = UNIT["z"]
    refused("idempotency_conflict", commit, db, config, manifest, changed)
    assert commit(db, config, manifest, payload(manifest))["created"] is False


def test_vectors_per_generation_are_bounded(tmp_path: Path) -> None:
    config = vectors_config(tmp_path, max_vectors_per_generation=2)
    db = connect(config.db_path)
    conversation_id, _ = converse(db, config, 2)  # two message vectors and one kind vector
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation)
    assert refused("limit_exceeded", commit, db, config, manifest).extra["limit"] == "max_vectors_per_generation"
    db.close()


# Activation -----------------------------------------------------------------------------------------------


def served_generation(db, config, conversation_id: str) -> set[str]:
    return {a["generation"] for a in search.read_artifacts(db, config, "viewer", conversation_id)["artifacts"]}


def test_8_a_commit_refused_for_a_missing_vector_keeps_the_old_generation_serving(db, config) -> None:
    conversation_id, _ = converse(db, config, 2)
    old = embedding_generation(db, config)
    fill(db, config, conversation_id, old)
    derived.generation_activate(db, config, "alpha", old)
    new = embedding_generation(db, config)
    fill(db, config, conversation_id, new)
    say(db, config, conversation_id, "arrived after the watermark, no batch has covered it")
    manifest = begin(db, config, conversation_id, new, kinds=derived.KINDS)
    short = payload(manifest, request_id="r-new")
    short["vectors"] = [v for v in short["vectors"] if "message_id" not in v["item"]]
    refused("vectors_incomplete", commit, db, config, manifest, short)
    refused("generation_behind", derived.generation_activate, db, config, "alpha", new)
    assert served_generation(db, config, conversation_id) == {old}
    commit(db, config, manifest, payload(manifest, request_id="r-new"))  # the retry carries the vector
    assert derived.generation_activate(db, config, "alpha", new)["generation"] == new
    assert served_generation(db, config, conversation_id) == {new}


def test_activation_refuses_an_embedding_generation_with_an_item_lacking_a_vector(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    fill(db, config, conversation_id, generation)
    db.execute("DELETE FROM vectors WHERE item_kind = 'message' AND item_id = ?", (sent[0],))  # a gap, stood up
    error = refused("generation_incomplete", derived.generation_activate, db, config, "alpha", generation)
    assert sent[0] in str(error.extra)


def invariant_holds(db, generation: str) -> bool:
    """Every valid latest version, and every message any of them was made from, has a vector or a skip."""
    covered = set(vector_rows(db, generation)) | set(skip_rows(db, generation))
    versions = db.execute("SELECT v.id, v.conversation_id FROM artifact_versions v WHERE v.generation = ? AND"
                          " v.state = 'valid' AND v.version = (SELECT MAX(o.version) FROM artifact_versions o WHERE"
                          " o.generation = v.generation AND o.conversation_id = v.conversation_id AND"
                          " o.kind = v.kind)", (generation,)).fetchall()
    for version_id, conversation_id in versions:
        if ("artifact", version_id) not in covered:
            return False
        for (from_seq, to_seq) in db.execute("SELECT from_seq, to_seq FROM artifact_inputs WHERE version_id = ?",
                                             (version_id,)):
            for (message_id,) in db.execute("SELECT DISTINCT id FROM messages WHERE conversation_id = ? AND"
                                            " conversation_seq BETWEEN ? AND ?", (conversation_id, from_seq, to_seq)):
                if ("message", message_id) not in covered:
                    return False
    return True


def test_the_vector_invariant_holds_over_random_commit_sequences(db, config) -> None:
    rng = random.Random(1610)
    conversations = [converse(db, config, 1)[0] for _ in range(2)]
    generation = embedding_generation(db, config)
    for step in range(40):
        conversation_id = rng.choice(conversations)
        if rng.random() < 0.4:
            say(db, config, conversation_id, f"step {step}")
            continue
        kind = rng.choice(derived.KINDS)
        try:
            manifest = begin(db, config, conversation_id, generation, kinds=(kind,))
        except BatonError:
            continue  # up to date, or another refusal: nothing to commit
        body = payload(manifest, request_id=f"r-{step}")
        if rng.random() < 0.5:  # drop a random vector: the commit must then be refused whole
            body["vectors"].pop(rng.randrange(len(body["vectors"])))
        try:
            commit(db, config, manifest, body)
        except BatonError as error:
            assert error.code == "vectors_incomplete", error
        assert invariant_holds(db, generation), f"step {step}"


# Hybrid search --------------------------------------------------------------------------------------------


def every_kind() -> dict:
    return {("kind", kind): UNIT["x"] for kind in derived.KINDS}


def hybrid(db, config, role: str, query: str, vector, **kwargs) -> list[str]:
    hits = search.search(db, config, role, query, kinds=["message"], query_vector=vector, model="toy-embed",
                         **kwargs)["hits"]
    return [hit["message_id"] for hit in hits]


def test_hybrid_search_reranks_keyword_matches_by_their_vectors(db, config) -> None:
    conversation_id, _ = converse(db, config, 0)
    near = say(db, config, conversation_id, "kestrel kestrel kestrel field notes")
    far = say(db, config, conversation_id, "kestrel in passing")
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS)
    commit(db, config, manifest, payload(manifest, vectors={**every_kind(), ("message_id", near): UNIT["y"],
                                                            ("message_id", far): UNIT["z"]}))
    derived.generation_activate(db, config, "alpha", generation)
    assert hybrid(db, config, "assistant", "kestrel", UNIT["z"])[0] == far  # the vector moves it up
    assert hybrid(db, config, "assistant", "kestrel", UNIT["z"], limit=1) == [far]  # re-ranked from a wider pool
    assert hybrid(db, config, "assistant", "kestrel", UNIT["y"])[0] == near
    hits = search.search(db, config, "assistant", "kestrel", query_vector=UNIT["z"], model="toy-embed")["hits"]
    assert math.isclose(hits[0]["similarity"], 1.0)


def test_hybrid_search_never_lets_an_unreadable_perfect_match_in_or_ahead(db, config) -> None:
    private, _ = converse(db, config, 0, participants=["orchestrator", "reviewer"])
    shared, _ = converse(db, config, 0, participants=["orchestrator", "assistant"])
    hidden = say(db, config, private, "osprey osprey osprey")
    seen = say(db, config, shared, "an osprey once")
    generation = embedding_generation(db, config)
    for conversation_id, message_id, vector in ((private, hidden, UNIT["x"]), (shared, seen, UNIT["y"])):
        manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS)
        commit(db, config, manifest, payload(manifest, request_id=conversation_id,
                                             vectors={**every_kind(), ("message_id", message_id): vector}))
    derived.generation_activate(db, config, "alpha", generation)
    assert hybrid(db, config, "assistant", "osprey", UNIT["x"], limit=1) == [seen]  # perfect vector, unreadable
    assert set(hybrid(db, config, "viewer", "osprey", UNIT["x"])) == {hidden, seen}
    assert hybrid(db, config, "outsider", "osprey", UNIT["x"]) == []


def test_an_invalid_version_s_vector_never_contributes(db, config) -> None:
    conversation_id, sent = converse(db, config, 2)
    generation = embedding_generation(db, config)
    manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS)
    commit(db, config, manifest, payload(manifest, body="heron summary"))
    derived.generation_activate(db, config, "alpha", generation)
    expire(db, sent[0])
    hits = search.search(db, config, "viewer", "heron", kinds=["summary"], query_vector=UNIT["x"],
                         model="toy-embed")["hits"]
    assert hits == []


def test_a_query_vector_needs_embeddings_on_and_a_matching_profile(tmp_path: Path, db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=UNIT["x"],
            model="no-such-model")
    generation = embedding_generation(db, config)
    fill(db, config, conversation_id, generation)
    derived.generation_activate(db, config, "alpha", generation)
    refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=[1.0, 0.0],
            model="toy-embed")
    refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=UNIT["x"])
    off = vectors_config(tmp_path, embeddings=False)
    refused("invalid_request", search.search, db, off, "assistant", "note", query_vector=UNIT["x"],
            model="toy-embed")


def test_the_search_tool_takes_a_query_vector_and_a_model() -> None:
    import inspect

    from baton import app
    parameters = inspect.signature(app.Tools.search).parameters
    assert {"query_vector", "model"} <= set(parameters)


@pytest.mark.parametrize(("distance", "first"), [("cosine", "aligned"), ("inner_product", "long")])
def test_the_profile_distance_decides_the_order(db, config, distance: str, first: str) -> None:
    """Unnormalized vectors, where the two distances disagree: cosine ranks by direction alone, inner product by
    the raw product, so a long vector at an angle beats a short aligned one only under inner product."""
    conversation_id, _ = converse(db, config, 0)
    aligned = say(db, config, conversation_id, "plover plover plover")
    long = say(db, config, conversation_id, "plover once")
    profile = {"embedding": {"model": "toy-embed", "dim": 3, "normalization": "none", "distance": distance}}
    generation = derived.generation_create(db, config, "alpha", profile)["generation"]
    manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS)
    commit(db, config, manifest, payload(manifest, vectors={**every_kind(), ("message_id", aligned): [0.1, 0.01, 0.0],
                                                            ("message_id", long): [5.0, 5.0, 0.0]}))
    derived.generation_activate(db, config, "alpha", generation)
    hits = search.search(db, config, "assistant", "plover", query_vector=[1.0, 0.0, 0.0], model="toy-embed")["hits"]
    order = [hit["message_id"] for hit in hits if hit.get("message_id") in (aligned, long)]
    assert order[0] == {"aligned": aligned, "long": long}[first]
    similarity = {hit["message_id"]: hit["similarity"] for hit in hits if hit.get("message_id") in (aligned, long)}
    close = {"rel_tol": 1e-6}  # vectors are stored as float32
    if distance == "cosine":
        assert math.isclose(similarity[aligned], 0.1 / math.hypot(0.1, 0.01), **close) and \
            math.isclose(similarity[long], 1 / math.sqrt(2), **close)
    else:
        assert math.isclose(similarity[aligned], 0.1, **close) and math.isclose(similarity[long], 5.0, **close)


FLOAT32_MAX = 3.4028234663852886e38
UNNORMALIZED = {"embedding": {"model": "toy-embed", "dim": 3, "normalization": "none", "distance": "inner_product"}}


@pytest.mark.parametrize("bad", [[1e39, 0.0, 0.0], [0.0, -1e39, 0.0], [FLOAT32_MAX * 2, 0.0, 0.0],
                                 [float("nan"), 0.0, 0.0], [float("inf"), 0.0, 0.0], [0.0, float("-inf"), 0.0]])
def test_a_vector_that_float32_cannot_hold_is_refused_whole_before_anything_is_stored(db, config, bad) -> None:
    """Vectors are stored as float32: a component it can't represent is a labelled refusal, never an overflow,
    and the same request id then commits once corrected."""
    conversation_id, _ = converse(db, config, 1)
    generation = derived.generation_create(db, config, "alpha", UNNORMALIZED)["generation"]
    manifest = begin(db, config, conversation_id, generation)
    body = payload(manifest)
    body["vectors"][0]["vector"] = bad
    error = refused("invalid_request", commit, db, config, manifest, body)
    assert "float32" in str(error) or "finite" in str(error), str(error)
    assert vector_rows(db, generation) == {}
    assert commit(db, config, manifest, payload(manifest))["created"] is True


def test_the_largest_float32_component_is_accepted(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    generation = derived.generation_create(db, config, "alpha", UNNORMALIZED)["generation"]
    manifest = begin(db, config, conversation_id, generation)
    body = payload(manifest)
    body["vectors"][0]["vector"] = [FLOAT32_MAX, 0.0, 0.0]
    assert commit(db, config, manifest, body)["created"] is True


@pytest.mark.parametrize("bad", [[1e39, 0.0, 0.0], [1e300, 1e300, 0.0]])
def test_a_query_vector_that_float32_cannot_hold_is_refused(db, config, bad) -> None:
    conversation_id, _ = converse(db, config, 1)
    generation = derived.generation_create(db, config, "alpha", UNNORMALIZED)["generation"]
    fill(db, config, conversation_id, generation)
    derived.generation_activate(db, config, "alpha", generation)
    refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=bad, model="toy-embed")



# One model, one embedding signature across every active generation (Lane D's ruling on mixed profiles) --------


def embedded(db, config, producer: str, conversation_id: str, *, model: str = "toy-embed", dim: int = 3,
             normalization: str = "unit", distance: str = "cosine") -> str:
    """A filled generation for `producer` that embeds with the given signature, ready to activate."""
    profile = {"embedding": {"model": model, "dim": dim, "normalization": normalization, "distance": distance}}
    generation = derived.generation_create(db, config, producer, profile)["generation"]
    manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS, producer=producer)
    axis = [[1.0 if i == n else 0.0 for i in range(dim)] for n in range(min(dim, 2))]
    vectors = {**{("kind", kind): axis[0] for kind in manifest["kinds"]},
               **{("message_id", message_id): axis[-1] for message_id in items(manifest)}}
    commit(db, config, manifest, payload(manifest, request_id=generation, vectors=vectors), producer=producer)
    return generation


def active(db, producer: str) -> str | None:
    row = db.execute("SELECT id FROM generations WHERE producer = ? AND active = 1", (producer,)).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize("change", [{"dim": 2}, {"normalization": "none"}, {"distance": "inner_product"}])
def test_activating_a_second_signature_for_a_model_is_refused_and_nothing_switches(db, config, change) -> None:
    conversation_id, _ = converse(db, config, 1)
    alpha = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", alpha)
    beta = embedded(db, config, "beta", conversation_id, **change)
    error = refused("embedding_profile_conflict", derived.generation_activate, db, config, "beta", beta)
    message = str(error)
    assert "toy-embed" in message and "alpha" in message and alpha in message, message
    assert (active(db, "alpha"), active(db, "beta")) == (alpha, None)  # the earlier state keeps serving
    other = error.extra["other"]
    assert (other["producer"], other["generation"]) == ("alpha", alpha)
    assert error.extra["signature"] != other["signature"] and error.extra["model"] == "toy-embed"


def test_a_producer_may_change_its_own_signature(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    three = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", three)
    two = embedded(db, config, "alpha", conversation_id, dim=2)
    derived.generation_activate(db, config, "alpha", two)  # the generation it replaces doesn't count against it
    assert active(db, "alpha") == two


def test_a_blocked_signature_activates_once_the_conflicting_one_is_replaced(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    alpha = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", alpha)
    beta = embedded(db, config, "beta", conversation_id, dim=2)
    refused("embedding_profile_conflict", derived.generation_activate, db, config, "beta", beta)
    plain = derived.generation_create(db, config, "alpha", {"summarizer": "toy"})["generation"]
    manifest = begin(db, config, conversation_id, plain, kinds=derived.KINDS)
    commit(db, config, manifest, payload(manifest, request_id="plain-fill", vectors={}))
    derived.generation_activate(db, config, "alpha", plain)  # alpha no longer embeds with toy-embed
    derived.generation_activate(db, config, "beta", beta)
    assert active(db, "beta") == beta


def test_different_model_names_with_different_signatures_coexist(db, config) -> None:
    conversation_id, _ = converse(db, config, 1)
    alpha = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", alpha)
    beta = embedded(db, config, "beta", conversation_id, model="toy-embed@2", dim=2)
    derived.generation_activate(db, config, "beta", beta)
    assert search.search(db, config, "assistant", "note", query_vector=UNIT["x"], model="toy-embed")["hits"]
    assert search.search(db, config, "assistant", "note", query_vector=[1.0, 0.0], model="toy-embed@2")["hits"]


def test_a_scoped_query_on_the_one_active_signature_succeeds_after_a_conflict_was_refused(db, config) -> None:
    """The reviewer's reproduction, inverted: beta's 2-D activation is refused, so alpha's 3-D query works."""
    conversation_id, _ = converse(db, config, 1)
    alpha = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", alpha)
    beta = embedded(db, config, "beta", conversation_id, dim=2)
    refused("embedding_profile_conflict", derived.generation_activate, db, config, "beta", beta)
    hits = search.search(db, config, "assistant", "summary", kinds=["summary"], producer="alpha",
                         query_vector=UNIT["x"], model="toy-embed")["hits"]
    assert hits and all(hit["producer"] == "alpha" for hit in hits)


def test_two_active_signatures_for_one_model_make_search_refuse_not_score(db, config) -> None:
    """A database that holds two anyway (crafted here, past the activation check) fails closed, accurately."""
    conversation_id, _ = converse(db, config, 1)
    alpha = embedded(db, config, "alpha", conversation_id)
    derived.generation_activate(db, config, "alpha", alpha)
    beta = embedded(db, config, "beta", conversation_id, dim=2)
    db.execute("UPDATE generations SET active = 1 WHERE id = ?", (beta,))
    for query in (UNIT["x"], [1.0, 0.0]):
        error = refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=query,
                        model="toy-embed")
        message = str(error)
        assert alpha in message and beta in message and "toy-embed" in message, message


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_an_integer_component_too_large_for_any_float_is_refused_not_raised(db, config, huge) -> None:
    """A JSON integer can be any size, so turning it into a float overflows: that is a labelled refusal in a
    commit and in a query, never an overflow, and the same request id commits once corrected."""
    conversation_id, _ = converse(db, config, 1)
    generation = derived.generation_create(db, config, "alpha", UNNORMALIZED)["generation"]
    manifest = begin(db, config, conversation_id, generation, kinds=derived.KINDS)
    body = payload(manifest)
    body["vectors"][0]["vector"] = [huge, 0, 0]
    refused("invalid_request", commit, db, config, manifest, body)
    assert vector_rows(db, generation) == {}
    assert commit(db, config, manifest, payload(manifest))["created"] is True


@pytest.mark.parametrize("huge", [10**400, -(10**400)])
def test_a_query_integer_too_large_for_any_float_is_refused_not_raised(db, config, huge) -> None:
    conversation_id, _ = converse(db, config, 1)
    generation = derived.generation_create(db, config, "alpha", UNNORMALIZED)["generation"]
    fill(db, config, conversation_id, generation)
    derived.generation_activate(db, config, "alpha", generation)
    refused("invalid_request", search.search, db, config, "assistant", "note", query_vector=[huge, 0, 0],
            model="toy-embed")
