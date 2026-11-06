"""Vector lookup and ordering for an already-authorized keyword candidate pool."""

from __future__ import annotations

import math
import sqlite3
import struct

import pytest

from baton import search
from baton.database import memory

EMBEDDING = {"model": "toy", "dim": 3, "normalization": "none", "distance": "inner_product"}
PROFILES = {"generation-a": EMBEDDING, "generation-b": EMBEDDING}


@pytest.fixture
def db():
    conn = memory()
    conn.execute(
        "CREATE TABLE vectors (generation TEXT, item_kind TEXT, item_id TEXT, model TEXT,"
        " dim INTEGER, data BLOB, PRIMARY KEY (generation, item_kind, item_id))"
    )
    yield conn
    conn.close()


def candidate(kind: str, item_id: str) -> dict:
    return {"derived": kind == "artifact", "version_id" if kind == "artifact" else "message_id": item_id}


def vector(db, kind: str, item_id: str, score: float, *, generation: str = "generation-a", model: str = "toy"):
    db.execute(
        "INSERT INTO vectors VALUES (?, ?, ?, ?, 3, ?)",
        (generation, kind, item_id, model, struct.pack("<3f", score, 0.0, 0.0)),
    )


@pytest.mark.parametrize("variable_limit", [None, 8])
def test_a_full_mixed_candidate_pool_uses_bounded_vector_queries(db, variable_limit):
    hits = []
    expected = []
    for number in range(200):
        for offset, kind in enumerate(("message", "artifact")):
            hit = candidate(kind, str(number))
            score = float(number * 2 + offset)
            hits.append(hit)
            expected.append((hit, score))
            vector(db, kind, str(number), score)
    if variable_limit is not None:
        db._driver_connection().setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
    statements = []
    db.set_trace_callback(statements.append)
    try:
        ranked = search._rerank(db, hits, [1.0, 0.0, 0.0], "toy", PROFILES)
    finally:
        db.set_trace_callback(None)
    assert ranked == [hit for hit, _ in reversed(expected)]
    assert [hit["similarity"] for hit in ranked] == [score for _, score in reversed(expected)]
    vector_reads = [statement for statement in statements if "FROM vectors" in statement]
    batch_size = db.max_parameters - len(PROFILES) - 2
    assert len(vector_reads) <= 2 * math.ceil(200 / batch_size)


def test_ranking_keeps_best_generation_scores_and_keyword_order_for_ties(db):
    missing_first = candidate("message", "missing-first")
    negative_first = candidate("message", "negative-first")
    message = candidate("message", "shared-id")
    zero_first = candidate("message", "zero-first")
    artifact = candidate("artifact", "shared-id")
    negative_second = candidate("message", "negative-second")
    zero_second = candidate("message", "zero-second")
    missing_second = candidate("artifact", "missing-second")
    hits = [missing_first, negative_first, message, zero_first, artifact, negative_second, zero_second, missing_second]
    vector(db, "message", "negative-first", -2.0)
    vector(db, "message", "negative-second", -2.0)
    vector(db, "message", "zero-first", 0.0)
    vector(db, "message", "zero-second", 0.0)
    vector(db, "message", "shared-id", -4.0)
    vector(db, "message", "shared-id", 3.0, generation="generation-b")
    vector(db, "artifact", "shared-id", 2.0)
    # These vectors must not contribute to any candidate's score.
    vector(db, "message", "missing-first", 100.0, generation="inactive")
    vector(db, "artifact", "missing-second", 100.0, model="other-model")
    vector(db, "message", "outside-the-keyword-pool", 100.0)
    ranked = search._rerank(db, hits, [1.0, 0.0, 0.0], "toy", PROFILES)
    assert ranked == [
        message,
        artifact,
        zero_first,
        zero_second,
        negative_first,
        negative_second,
        missing_first,
        missing_second,
    ]
    assert [hit["similarity"] for hit in ranked] == [3.0, 2.0, 0.0, 0.0, -2.0, -2.0, None, None]


def test_an_empty_keyword_pool_reads_no_vectors(db):
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert search._rerank(db, [], [1.0, 0.0, 0.0], "toy", PROFILES) == []
    finally:
        db.set_trace_callback(None)
    assert not statements
