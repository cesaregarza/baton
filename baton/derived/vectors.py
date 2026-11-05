"""Embedding profiles, canonical vector requests, and vector or skip persistence.

Checks and writes run within the caller's transaction. Artifact lookups come from storage;
this module does not call batch or generation orchestration."""

from __future__ import annotations

import json
import math
import struct
from typing import Any

from baton.config import Config
from baton.database import Connection, Row
from baton.errors import EmbeddingProfileConflict, InvalidRequest, LimitExceeded, VectorConflict, VectorsIncomplete

from . import storage
from .constants import KINDS, SKIP_REASONS, UNIT_TOLERANCE


def signature(embedding: dict[str, Any]) -> dict[str, Any]:
    """What an embedding profile commits a model name to: every active generation using the name must agree."""
    return {key: embedding[key] for key in ("model", "dim", "normalization", "distance")}


def _require_one_signature(conn: Connection, producer: str, target: Row) -> None:
    """One model name, one embedding signature across active generations. Every other producer's active one is
    compared; this producer's own, which the activation replaces, is not, so a producer may change its own."""
    embedding = json.loads(target["profile"]).get("embedding")
    if not embedding:
        return
    mine = signature(embedding)
    for other in conn.execute(
        "SELECT id, producer, profile FROM generations WHERE active = 1 AND producer != ? ORDER BY producer, id",
        (producer,),
    ):
        theirs = json.loads(other["profile"]).get("embedding")
        if theirs and theirs["model"] == mine["model"] and signature(theirs) != mine:
            raise EmbeddingProfileConflict(
                f"{other['producer']}'s active generation {other['id']} embeds {mine['model']} as {signature(theirs)},"
                f" and {target['id']} as {mine}: one model name has one signature, so give this one its own name",
                mine["model"],
                mine,
                {"producer": other["producer"], "generation": other["id"], "signature": signature(theirs)},
            )


def _unvectored(conn: Connection, producer: str, generation: str, counted: list) -> list[dict]:
    """Served versions and messages with a copy, in counted conversations, holding no vector or skip."""
    missing = []
    for conversation in counted:
        for kind in KINDS:
            version = storage._current(conn, producer, generation, conversation["id"], kind)
            if version is not None and _held(conn, generation, "artifact", version["id"]) is None:
                missing.append({"conversation_id": conversation["id"], "item": f"artifact {version['id']}"})
        for (message_id,) in conn.execute(
            "SELECT DISTINCT id FROM messages WHERE conversation_id = ?", (conversation["id"],)
        ).fetchall():
            if _held(conn, generation, "message", message_id) is None:
                missing.append({"conversation_id": conversation["id"], "item": f"message {message_id}"})
    return missing


def _profile(config: Config, profile: Any) -> dict[str, Any]:
    """A generation's profile, checked: an optional summarizer label, and an optional embedding description."""
    if profile is None:
        return {}
    if not isinstance(profile, dict) or not set(profile) <= {"summarizer", "embedding"}:
        raise InvalidRequest("a profile has only summarizer and embedding")
    summarizer = profile.get("summarizer")
    if summarizer is not None and (not isinstance(summarizer, str) or not 0 < len(summarizer) <= 200):
        raise InvalidRequest("profile.summarizer is a label of 1 to 200 characters")
    embedding = profile.get("embedding")
    if embedding is not None and not config.search.embeddings:
        raise InvalidRequest("profile.embedding needs [search] embeddings = true")
    if embedding is not None:
        if not isinstance(embedding, dict) or set(embedding) != {"model", "dim", "normalization", "distance"}:
            raise InvalidRequest("profile.embedding has exactly model, dim, normalization and distance")
        model, dim = embedding["model"], embedding["dim"]
        if not isinstance(model, str) or not 0 < len(model) <= 200:
            raise InvalidRequest("profile.embedding.model is a name of 1 to 200 characters")
        if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= config.search.max_vector_dim:
            raise InvalidRequest(f"profile.embedding.dim is 1 to {config.search.max_vector_dim}")
        if embedding["normalization"] not in ("unit", "none") or embedding["distance"] not in (
            "cosine",
            "inner_product",
        ):
            raise InvalidRequest(
                "profile.embedding.normalization is unit or none, and distance is cosine or inner_product"
            )
    return profile


def _vector_entries(entries: Any, field: str) -> list[dict[str, Any]]:
    """`vectors` or `skips` in canonical form: each names one item, a kind written here or a served message."""
    if not isinstance(entries, list):
        raise InvalidRequest(f"{field} is a list")
    canonical = []
    for entry in entries:
        item = entry.get("item") if isinstance(entry, dict) else None
        if (
            not isinstance(item, dict)
            or len(item) != 1
            or not isinstance(next(iter(item.values())), str)
            or next(iter(item)) not in ("kind", "message_id")
        ):
            raise InvalidRequest(f'each of {field} names one item: {{"kind": ...}} or {{"message_id": ...}}')
        if field == "vectors":
            vector = entry.get("vector")
            if not isinstance(vector, list) or not all(
                isinstance(x, int | float) and not isinstance(x, bool) for x in vector
            ):
                raise InvalidRequest(f"the vector for {_item_name(item)} is a list of numbers")  # range: _check_fit
            try:
                as_floats = [float(x) for x in vector]
            except OverflowError:  # an integer of any size is valid JSON; past a float's range it is refused here
                raise InvalidRequest(
                    f"the vector for {_item_name(item)} has a component that isn't a finite float32"
                ) from None
            canonical.append({"item": item, "vector": as_floats})
        else:
            if entry.get("reason") not in SKIP_REASONS:
                raise InvalidRequest(f"a skip's reason is one of {', '.join(SKIP_REASONS)}")
            canonical.append({"item": item, "reason": entry["reason"]})
    return sorted(canonical, key=lambda entry: _item_key(entry["item"]))


def _item_key(item: dict[str, str]) -> tuple[str, str]:
    return next(iter(item.items()))


def _item_name(item: dict[str, str]) -> str:
    field, value = _item_key(item)
    return f"kind {value}" if field == "kind" else f"message {value}"


def _embedding(conn: Connection, generation: str) -> dict[str, Any] | None:
    profile = conn.execute("SELECT profile FROM generations WHERE id = ?", (generation,)).fetchone()[0]
    return json.loads(profile).get("embedding")


def _check_vectors(
    conn: Connection,
    config: Config,
    manifest: dict[str, Any],
    artifacts: list[dict],
    vectors: list[dict],
    skips: list[dict],
    embedding: dict | None,
) -> dict:
    """What this commit owes in vectors and skips, checked whole before anything is written.

    Returns, for each kind written and each served message that holds nothing yet, its vector or skip; and
    None for a served message that already holds one (an identical resend is a no-op, a different one a
    conflict). Without an embedding profile, a commit carries none.
    """
    if embedding is None:
        if vectors or skips:
            raise InvalidRequest("this generation's profile has no embedding, so its commits carry no vectors")
        return {}
    given = {_item_key(entry["item"]): entry for entry in vectors + skips}
    served = {item["message_id"]: item["seq"] for segment in manifest["segments"] for item in segment["items"]}
    bodies = {artifact["kind"]: artifact["body"] for artifact in artifacts}
    _validate_given_vectors(conn, given, bodies, served, embedding)
    owed = _required_kind_vectors(given, bodies)
    owed.update(_required_message_vectors(conn, manifest["generation"], given, served, embedding))
    _check_vector_capacity(conn, manifest["generation"], config, owed)
    return owed


def _validate_given_vectors(conn: Connection, given: dict, bodies: dict, served: dict, embedding: dict) -> None:
    for (field, value), entry in given.items():
        if (field == "kind" and value not in bodies) or (field == "message_id" and value not in served):
            raise InvalidRequest(f"{_item_name(entry['item'])} isn't written or served by this commit")
        if "vector" in entry:
            _check_fit(entry["vector"], embedding, _item_name(entry["item"]))
        elif entry["reason"] == "empty_text":
            _validate_empty_skip(conn, field, value, entry, bodies)


def _validate_empty_skip(conn: Connection, field: str, value: str, entry: dict, bodies: dict) -> None:
    text = (
        bodies[value]
        if field == "kind"
        else conn.execute("SELECT text FROM messages WHERE id = ? LIMIT 1", (value,)).fetchone()[0]
    )
    if text.strip():
        raise InvalidRequest(f"{_item_name(entry['item'])} isn't empty, so it can't be skipped as empty_text")


def _required_kind_vectors(given: dict, bodies: dict) -> dict:
    owed: dict = {}
    for kind in sorted(bodies):
        if ("kind", kind) not in given:
            raise VectorsIncomplete(f"no vector or skip for kind {kind}")
        owed[("kind", kind)] = given[("kind", kind)]
    return owed


def _required_message_vectors(conn: Connection, generation: str, given: dict, served: dict, embedding: dict) -> dict:
    owed = {}
    for message_id in served:
        held = _held(conn, generation, "message", message_id)
        entry = given.get(("message_id", message_id))
        if held is None:
            if entry is None:
                raise VectorsIncomplete(f"no vector or skip for message {message_id}")
            owed[("message_id", message_id)] = entry
        elif entry is not None and _stored_form(entry, embedding) != held:
            raise VectorConflict(f"message {message_id} already has a different vector or skip in this generation")
        else:
            owed[("message_id", message_id)] = None  # held already: nothing to write
    return owed


def _check_vector_capacity(conn: Connection, generation: str, config: Config, owed: dict) -> None:
    adding = sum(1 for entry in owed.values() if entry is not None and "vector" in entry)
    stored = conn.execute("SELECT COUNT(*) FROM vectors WHERE generation = ?", (generation,)).fetchone()[0]
    if stored + adding > config.indexer.max_vectors_per_generation:
        raise LimitExceeded(f"this generation would hold {stored + adding} vectors", "max_vectors_per_generation")


def fits_float32(value: float) -> bool:
    """Whether a vector component can be stored as it is kept, a float32: finite and within its range. An integer
    too large for any float is simply one that can't."""
    try:
        value = float(value)
    except OverflowError:
        return False
    if not math.isfinite(value):
        return False
    try:
        struct.pack("<f", value)
    except OverflowError:
        return False
    return True


def _check_fit(vector: list[float], embedding: dict[str, Any], name: str) -> None:
    if not all(fits_float32(x) for x in vector):
        raise InvalidRequest(f"the vector for {name} has a component that isn't a finite float32")
    if len(vector) != embedding["dim"]:
        raise InvalidRequest(f"the vector for {name} has {len(vector)} dimensions, and the profile {embedding['dim']}")
    if embedding["normalization"] == "unit" and abs(math.sqrt(sum(x * x for x in vector)) - 1) > UNIT_TOLERANCE:
        raise InvalidRequest(f"the vector for {name} isn't unit length, as the profile requires")


def _held(conn: Connection, generation: str, item_kind: str, item_id: str) -> tuple | None:
    vector = conn.execute(
        "SELECT data FROM vectors WHERE generation = ? AND item_kind = ? AND item_id = ?",
        (generation, item_kind, item_id),
    ).fetchone()
    if vector is not None:
        return ("vector", bytes(vector[0]))
    skip = conn.execute(
        "SELECT reason FROM vector_skips WHERE generation = ? AND item_kind = ? AND item_id = ?",
        (generation, item_kind, item_id),
    ).fetchone()
    return ("skip", skip[0]) if skip is not None else None


def _stored_form(entry: dict[str, Any], embedding: dict[str, Any]) -> tuple:
    if "vector" in entry:
        return ("vector", struct.pack(f"<{embedding['dim']}f", *entry["vector"]))
    return ("skip", entry["reason"])


def _write_vector(
    conn: Connection, generation: str, embedding: dict[str, Any], item_kind: str, item_id: str, entry: dict[str, Any]
) -> None:
    if "vector" in entry:
        conn.execute(
            "INSERT INTO vectors (generation, item_kind, item_id, model, dim, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                generation,
                item_kind,
                item_id,
                embedding["model"],
                embedding["dim"],
                _stored_form(entry, embedding)[1],
                storage._now(),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO vector_skips (generation, item_kind, item_id, reason) VALUES (?, ?, ?, ?)",
            (generation, item_kind, item_id, entry["reason"]),
        )
