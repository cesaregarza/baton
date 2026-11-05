"""Reference documents: text a topic keeps, maintained one revision at a time by a core role or a topic grantee.

A reference is a named document in a topic, such as `job-board` in `job-hunt/boards`. An edit never changes what
is stored: it appends the document's next revision, and the current text is the highest one. So nothing is edited
or deleted, whoever asks, and an undo is the old text written again as a new revision. Documents hang on the
topic's id, never its path, so a rename carries them along and a path it frees starts again with none.

An edit is a compare-and-swap. The caller names the revision it read, or 0 to create the document, and an edit
made from any other is refused as stale, so two writers who read the same revision cannot both win. Everything an
edit decides runs in one write transaction: the topic, the caller's authority, a retry, the compare and the
limits. Nothing is read before the lock is held, which is also why a grant revoked a moment earlier refuses the
edit. A refusal for the shape of the arguments comes first and needs no database.

Reading is open to every identified caller, which the guard decides. It consults no grant and writes no row, for
references follow topic visibility and any identified caller may list the topics.
"""

from __future__ import annotations

import secrets
from typing import Any

from baton import events, messages, topics
from baton.agents.permissions import Capability, Permissions
from baton.config import Config
from baton.database import Connection, Row
from baton.errors import InvalidRequest, LimitExceeded, StaleReference, UnknownReference
from baton.topics import grants as topic_grants
from baton.topics import paths as topic_paths

CAPABILITY = "reference.edit"
MAX_BODY_BYTES = 32768
MAX_DOCS_PER_TOPIC = 20
MAX_NAME = topic_paths.MAX_TOPIC_SEGMENT
MAX_HISTORY_PAGE = 100
# A revision as every answer describes it, without its text. SQLite counts the size in UTF-8 bytes, so a list of
# documents or a page of history never loads a body.
_DESCRIBED = (
    "revision, base_revision, at, author_role, author_alias_id, author_alias_name, author_alias_code,"
    " grant_id, LENGTH(CAST(body AS BLOB)) AS size"
)


def is_name(value: Any) -> bool:
    """Whether `value` is a reference name: one topic segment, which no binding token or grant key ever is."""
    return topic_paths.is_segment(value)


def _is_count(value: Any, minimum: int) -> bool:
    # A bool is an int in Python, and `True` must not pass as revision 1.
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _check_name(name: Any) -> None:
    if not is_name(name):
        raise InvalidRequest(f"name must be 1-{MAX_NAME} characters of a-z, 0-9 and inner hyphens, such as job-board")


def _utf8_size(body: Any) -> int:
    """A body's size in bytes of UTF-8, or 0 if it is not text a database can keep. A lone surrogate is not: it
    arrives from JSON, and has no UTF-8 form."""
    if not isinstance(body, str):
        return 0
    try:
        return len(body.encode())
    except UnicodeEncodeError:
        return 0


def _check_body(body: Any) -> None:
    """Text with something in it and at most 32768 bytes: bytes, so a two-byte letter counts twice."""
    size = _utf8_size(body)
    if size == 0:
        raise InvalidRequest(f"body must be text of 1-{MAX_BODY_BYTES} bytes of UTF-8")
    if size > MAX_BODY_BYTES:
        raise LimitExceeded(
            f"a reference body is at most {MAX_BODY_BYTES} bytes of UTF-8; this one is {size}", "reference_body_bytes"
        )


def _check_expected(expected: Any) -> None:
    """The revision the caller read must be given: a missing one would skip the compare, so the schema's
    requirement is not left to the schema alone."""
    if not _is_count(expected, 0):
        raise InvalidRequest(
            "expected_revision is required: 0 to create the reference, otherwise the revision you read"
        )


def _request_id(request_id: Any) -> str:
    """The caller's request id, or one the host makes when it left it out. An empty one is not left out.

    One with no UTF-8 form, a lone surrogate from JSON, is as malformed as one too long: the database cannot bind it.
    """
    if request_id is None:
        return messages.new_request_id()
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200 or _utf8_size(request_id) == 0:
        raise InvalidRequest("request_id is 1-200 characters")
    return request_id


def _may_edit(config: Config, actor: str) -> None:
    Permissions.for_role(config, actor).require(
        Capability.CREATE_TOPICS,
        f"{actor} may not edit references (roles.{actor}.can_create_topics)",
    )


def _authorize(
    conn: Connection, identity: dict[str, str] | None, grant_key: str | None, path: str
) -> tuple[Row, Row | None]:
    """The topic at `path` and the grant the caller acts under, or the refusal.

    A core role was settled by its flag. A guest is decided by its key's grant, which reads the topic itself and
    denies a path with no topic as it denies one the grant does not cover.
    """
    if grant_key is None:
        return topics.require_topic(conn, path), None
    grant = topic_grants.grant_for_use(conn, identity, grant_key)
    return topic_grants.require_covers(conn, grant, CAPABILITY, path), grant


def _digest(actor: str, identity: dict[str, str] | None, topic_id: str, name: str, body: str, expected: int) -> str:
    """A request's identity: who asked, which document by the topic's id and the whole of the edit.

    The alias is in it because every guest acts as the same role, and the topic's id because a path can be given
    to another topic. Never a key or a binding token, so a retry made after claiming the alias again still replays.
    """
    return events.request_digest(
        "edit_reference",
        {
            "actor": actor,
            "alias_id": (identity or {}).get("alias_id"),
            "topic_id": topic_id,
            "name": name,
            "body": body,
            "expected_revision": expected,
        },
    )


def _locate(conn: Connection, topic_id: str, name: str) -> tuple[str | None, int]:
    """A document's id and its current revision, its highest. A document that does not exist is at revision 0."""
    row = conn.execute(
        "SELECT d.id, r.revision FROM reference_docs d JOIN reference_revisions r ON r.doc_id = d.id"
        " WHERE d.topic_id = ? AND d.name = ? ORDER BY r.revision DESC LIMIT 1",
        (topic_id, name),
    ).fetchone()
    return (None, 0) if row is None else (row["id"], row["revision"])


def _document(conn: Connection, topic_id: str, path: str, name: str, expected: int) -> str:
    """The id of the document an edit appends to, made if it is new, or the refusal of a stale edit or a full topic.

    The topic's limit counts its own documents only, never its subtopics', and only when one is made: a topic that
    is full can still have each of its documents edited.
    """
    doc_id, current = _locate(conn, topic_id, name)
    if expected != current:
        raise StaleReference(
            f"reference {name!r} in {path!r} is at revision {current}, not {expected}; read it again before editing it",
            current,
        )
    if doc_id is not None:
        return doc_id
    held = conn.execute("SELECT COUNT(*) FROM reference_docs WHERE topic_id = ?", (topic_id,)).one()[0]
    if held >= MAX_DOCS_PER_TOPIC:
        raise LimitExceeded(
            f"topic {path!r} already holds {MAX_DOCS_PER_TOPIC} references; edit one of them", "references_per_topic"
        )
    doc_id = "ref_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO reference_docs (id, topic_id, name, created_at) VALUES (?, ?, ?, ?)",
        (doc_id, topic_id, name, events.now()),
    )
    return doc_id


def _append(
    conn: Connection,
    *,
    doc_id: str,
    number: int,
    body: str,
    actor: str,
    identity: dict[str, str] | None,
    grant_id: str | None,
    request_id: str,
    digest: str,
) -> None:
    """Write revision `number`, based on the one before it, with who wrote it and under which grant.

    The alias's name and code are what it is called now, kept for reading only. The retry identity is stored with
    the revision and lives as long as it does.
    """
    who = identity or {}
    conn.execute(
        "INSERT INTO reference_revisions (doc_id, revision, body, author_role, author_alias_id,"
        " author_alias_name, author_alias_code, grant_id, request_id, request_digest, base_revision, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            number,
            body,
            actor,
            who.get("alias_id"),
            who.get("alias"),
            who.get("code"),
            grant_id,
            request_id,
            digest,
            number - 1,
            events.now(),
        ),
    )


def _revision(conn: Connection, doc_id: str, number: int) -> Row:
    """A revision with its text. Revisions are exactly 1 to the current one, so a caller within that has its row.

    Every answer about a revision is read back from here, so an edit, its retry and a later read cannot describe it
    differently.
    """
    return conn.execute(
        f"SELECT {_DESCRIBED}, body FROM reference_revisions WHERE doc_id = ? AND revision = ?", (doc_id, number)
    ).one()


def _author(row: Row) -> dict[str, str]:
    """Who wrote a revision: the role it ran as, and the alias as it was called then if an alias was behind it."""
    author = {"role": row["author_role"]}
    if row["author_alias_id"] is not None:
        author.update(alias_id=row["author_alias_id"], alias=row["author_alias_name"], code=row["author_alias_code"])
    return author


def _summary(row: Row) -> dict[str, Any]:
    """What an answer says of a revision besides its text."""
    return {
        "revision": row["revision"],
        "base_revision": row["base_revision"],
        "at": row["at"],
        "size": row["size"],
        "author": _author(row),
        "grant_id": row["grant_id"],
    }


def edit(
    conn: Connection,
    config: Config,
    *,
    actor: str,
    topic: Any,
    name: Any,
    body: Any,
    expected_revision: Any,
    request_id: Any = None,
    identity: dict[str, str] | None = None,
    grant_key: str | None = None,
    seen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a document's next revision, or answer the retry of one already appended.

    With no `grant_key` the caller is a core role and needs `can_create_topics`. With one it is a guest, and its
    key's own grant must be active and cover the topic with `reference.edit`. A retry runs that check first, so a
    grant that has ended answers no retry from the stored revision, and a retry is found before the compare, so one
    made from a revision that has since moved on still answers.
    """
    if grant_key is None:
        _may_edit(config, actor)
    path = topics.check_path(topic, field="topic")
    _check_name(name)
    _check_body(body)
    _check_expected(expected_revision)
    request_id = _request_id(request_id)
    with conn.transaction():
        topic_row, grant = _authorize(conn, identity, grant_key, path)
        grant_id = None if grant is None else grant["id"]
        digest = _digest(actor, identity, topic_row["id"], name, body, expected_revision)
        stored = events.replayed(conn, "reference_revisions", request_id, digest)
        if stored is None:
            doc_id = _document(conn, topic_row["id"], path, name, expected_revision)
            number = expected_revision + 1
            _append(
                conn,
                doc_id=doc_id,
                number=number,
                body=body,
                actor=actor,
                identity=identity,
                grant_id=grant_id,
                request_id=request_id,
                digest=digest,
            )
        else:
            doc_id, number = stored["doc_id"], stored["revision"]
        topics.record_seen(seen, path, topic_row, reference=name, revision=number, grant_id=grant_id)
        return {
            "topic": path,
            "topic_id": topic_row["id"],
            "name": name,
            **_summary(_revision(conn, doc_id, number)),
            "request_id": request_id,
            "created": stored is None,
        }


def _existing(conn: Connection, topic_id: str, path: str, name: str) -> tuple[str, int]:
    """A document that must exist: its id and current revision, or the refusal naming what is missing."""
    doc_id, current = _locate(conn, topic_id, name)
    if doc_id is None:
        raise UnknownReference(f"no reference {name!r} in {path!r}")
    return doc_id, current


def _listing(conn: Connection, topic_id: str) -> list[dict[str, Any]]:
    """The topic's own documents by name, each described by its current revision."""
    rows = conn.execute(
        f"SELECT d.name, {_DESCRIBED} FROM reference_docs d JOIN reference_revisions r"
        " ON r.doc_id = d.id WHERE d.topic_id = ? AND r.revision ="
        " (SELECT MAX(revision) FROM reference_revisions WHERE doc_id = d.id) ORDER BY d.name",
        (topic_id,),
    ).fetchall()
    return [
        {
            "name": row["name"],
            "current_revision": row["revision"],
            "at": row["at"],
            "size": row["size"],
            "author": _author(row),
            "grant_id": row["grant_id"],
        }
        for row in rows
    ]


def read(
    conn: Connection, *, topic: Any, name: Any = None, revision: Any = None, seen: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A topic's references, or one reference's current text, or one old revision of it, as it was written.

    The list holds only the topic's own documents. A revision needs a name, and a revision the document does not
    have is refused with the number it is at, never read as the current one.
    """
    path = topics.check_path(topic, field="topic")
    if name is not None:
        _check_name(name)
    if revision is not None and not _is_count(revision, 1):
        raise InvalidRequest("revision is a whole number from 1")
    if revision is not None and name is None:
        raise InvalidRequest("revision needs a name")
    with conn.snapshot():  # one view, so a write in between cannot mix two revisions into one answer
        topic_row = topics.require_topic(conn, path)
        topics.record_seen(seen, path, topic_row, reference=name)
        if name is None:
            return {"topic": path, "topic_id": topic_row["id"], "references": _listing(conn, topic_row["id"])}
        doc_id, current = _existing(conn, topic_row["id"], path, name)
        # Compared here, not asked of the database: it cannot take a number beyond its integers, and every number up
        # to the current revision has a row.
        if revision is not None and revision > current:
            raise UnknownReference(
                f"reference {name!r} in {path!r} has no revision {revision}; its current revision is {current}"
            )
        found = _revision(conn, doc_id, current if revision is None else revision)
        return {
            "topic": path,
            "topic_id": topic_row["id"],
            "name": name,
            "current_revision": current,
            "body": found["body"],
            **_summary(found),
        }


def history(
    conn: Connection,
    *,
    topic: Any,
    name: Any,
    after_revision: Any = 0,
    limit: Any = 50,
    seen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A reference's revisions after `after_revision`, oldest first and without their text.

    `next_revision` is the last one returned, or `after_revision` when the page is empty, so feeding it back
    reads on until nothing is left.
    """
    path = topics.check_path(topic, field="topic")
    _check_name(name)
    if not (_is_count(after_revision, 0) and _is_count(limit, 1) and limit <= MAX_HISTORY_PAGE):
        raise InvalidRequest(f"limit is 1-{MAX_HISTORY_PAGE} and after_revision at least 0")
    with conn.snapshot():
        topic_row = topics.require_topic(conn, path)
        topics.record_seen(seen, path, topic_row, reference=name)
        doc_id, current = _existing(conn, topic_row["id"], path, name)
        # A cursor at or past the last revision reads nothing, and one beyond the database's integers cannot be bound.
        rows = (
            []
            if after_revision >= current
            else conn.execute(
                f"SELECT {_DESCRIBED} FROM reference_revisions "
                "WHERE doc_id = ? AND revision > ? ORDER BY revision LIMIT ?",
                (doc_id, after_revision, limit),
            ).fetchall()
        )
        return {
            "topic": path,
            "topic_id": topic_row["id"],
            "name": name,
            "current_revision": current,
            "revisions": [_summary(row) for row in rows],
            "next_revision": rows[-1]["revision"] if rows else after_revision,
        }
