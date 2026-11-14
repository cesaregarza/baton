"""Append-only revisioned text: the rules a topic's reference documents and the wiki's pages share.

A document is never changed in place. Each edit appends its next revision, numbered from 1 with no gap, based on the
one before it, and the current text is the highest. The caller names the revision it read, or 0 to create the
document, and an edit made from any other is refused as stale, so two writers who read the same revision cannot both
win. A retry under the same request id answers the revision it stored, and the same id with any other request is a
conflict. Each revision records who wrote it: the role the call ran as and, when an alias was behind it, the alias's
id, name and code as they were then.

Each kind of document keeps its own tables and says what else a revision of it records (`Revisions`). The checks on a
call's arguments here run before any transaction; the compare, the retry and the append run inside the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from baton import events, messages
from baton.database import Connection, Row
from baton.errors import BatonError, InvalidRequest, LimitExceeded, StaleReference

MAX_BODY_BYTES = 32768
MAX_HISTORY_PAGE = 100


def is_count(value: Any, minimum: int) -> bool:
    # A bool is an int in Python, and `True` must not pass as revision 1.
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _utf8_size(body: Any) -> int:
    """A body's size in bytes of UTF-8, or 0 if it is not text a database can keep. A lone surrogate is not: it
    arrives from JSON, and has no UTF-8 form."""
    if not isinstance(body, str):
        return 0
    try:
        return len(body.encode())
    except UnicodeEncodeError:
        return 0


def check_body(body: Any, kind: str, limit: str) -> None:
    """Text with something in it and at most 32768 bytes: bytes, so a two-byte letter counts twice. Nothing is
    trimmed or normalized, so text of spaces alone is a body. `kind` names the document in the refusal, as in
    "a reference body", and `limit` is the refusal's limit name."""
    size = _utf8_size(body)
    if size == 0:
        raise InvalidRequest(f"body must be text of 1-{MAX_BODY_BYTES} bytes of UTF-8")
    if size > MAX_BODY_BYTES:
        raise LimitExceeded(f"{kind} is at most {MAX_BODY_BYTES} bytes of UTF-8; this one is {size}", limit)


def check_expected(expected: Any, document: str) -> None:
    """The revision the caller read must be given: a missing one would skip the compare, so the schema's
    requirement is not left to the schema alone."""
    if not is_count(expected, 0):
        raise InvalidRequest(f"expected_revision is required: 0 to create {document}, otherwise the revision you read")


def check_revision(revision: Any) -> None:
    if not is_count(revision, 1):
        raise InvalidRequest("revision is a whole number from 1")


def check_page(after_revision: Any, limit: Any) -> None:
    if not (is_count(after_revision, 0) and is_count(limit, 1) and limit <= MAX_HISTORY_PAGE):
        raise InvalidRequest(f"limit is 1-{MAX_HISTORY_PAGE} and after_revision at least 0")


def request_id(value: Any) -> str:
    """The caller's request id, or one the host makes when it left it out. An empty one is not left out.

    One with no UTF-8 form, a lone surrogate from JSON, is as malformed as one too long: the database cannot bind it.
    """
    if value is None:
        return messages.new_request_id()
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or _utf8_size(value) == 0:
        raise InvalidRequest("request_id is 1-200 characters")
    return value


def digest(operation: str, actor: str, identity: dict[str, str] | None, **request: Any) -> str:
    """A request's identity: who asked and the whole of what it asked for.

    The alias is in it because every guest acts as the same role. Never a key or a binding token, so a retry made
    after claiming the alias again still replays.
    """
    return events.request_digest(operation, {"actor": actor, "alias_id": (identity or {}).get("alias_id"), **request})


def require_current(document: str, current: int, expected: int) -> None:
    """The compare of a compare-and-swap. A document that does not exist is at revision 0."""
    if expected != current:
        raise StaleReference(
            f"{document} is at revision {current}, not {expected}; read it again before editing it", current
        )


def require_revision(missing: type[BatonError], document: str, revision: int, current: int) -> None:
    """Refuse a revision beyond the current one with the number the document is at, never read it as the current one.

    Compared here, not asked of the database: it cannot take a number beyond its integers, and every number up to
    the current revision has a row.
    """
    if revision > current:
        raise missing(f"{document} has no revision {revision}; its current revision is {current}")


def author(row: Row) -> dict[str, str]:
    """Who wrote a revision: the role it ran as, and the alias as it was called then if an alias was behind it."""
    found = {"role": row["author_role"]}
    if row["author_alias_id"] is not None:
        found.update(alias_id=row["author_alias_id"], alias=row["author_alias_name"], code=row["author_alias_code"])
    return found


@dataclass(frozen=True)
class Revisions:
    """One kind of document's revision table: `table`, keyed by the `key` column naming the document, and the
    columns `own` that only this kind records beside what every revision does."""

    table: str
    key: str
    own: tuple[str, ...]

    @property
    def described(self) -> str:
        """A revision as every answer describes it, without its text. SQLite counts the size in UTF-8 bytes, so a
        list of documents or a page of history never loads a body."""
        return (
            "revision, base_revision, at, author_role, author_alias_id, author_alias_name, author_alias_code, "
            + "".join(f"{column}, " for column in self.own)
            + "LENGTH(CAST(body AS BLOB)) AS size"
        )

    def append(
        self,
        conn: Connection,
        document: Any,
        number: int,
        body: str,
        *,
        actor: str,
        identity: dict[str, str] | None,
        request_id: str,
        digest: str,
        **own: Any,
    ) -> None:
        """Write revision `number`, based on the one before it, with who wrote it and this kind's `own` columns.

        The alias's name and code are what it is called now, kept for reading only. The retry identity is stored with
        the revision and lives as long as it does.
        """
        who = identity or {}
        columns = {
            self.key: document,
            "revision": number,
            "body": body,
            "author_role": actor,
            "author_alias_id": who.get("alias_id"),
            "author_alias_name": who.get("alias"),
            "author_alias_code": who.get("code"),
            **{column: own[column] for column in self.own},
            "request_id": request_id,
            "request_digest": digest,
            "base_revision": number - 1,
            "at": events.now(),
        }
        conn.execute(
            f"INSERT INTO {self.table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )

    def read(self, conn: Connection, document: Any, number: int) -> Row:
        """A revision with its text. Revisions are exactly 1 to the current one, so a caller within that has its row.

        Every answer about a revision is read back from here, so an edit, its retry and a later read cannot describe
        it differently.
        """
        return conn.execute(
            f"SELECT {self.described}, body FROM {self.table} WHERE {self.key} = ? AND revision = ?", (document, number)
        ).one()

    def summary(self, row: Row) -> dict[str, Any]:
        """What an answer says of a revision besides its text."""
        return {
            "revision": row["revision"],
            "base_revision": row["base_revision"],
            "at": row["at"],
            "size": row["size"],
            "author": author(row),
            **{column: row[column] for column in self.own},
        }

    def page(self, conn: Connection, document: Any, current: int, after_revision: int, limit: int) -> dict[str, Any]:
        """The revisions after `after_revision`, oldest first and without their text.

        `next_revision` is the last one returned, or `after_revision` when the page is empty, so feeding it back
        reads on until nothing is left.
        """
        # A cursor at or past the last revision reads nothing, and one beyond the database's integers cannot be bound.
        rows = (
            []
            if after_revision >= current
            else conn.execute(
                f"SELECT {self.described} FROM {self.table} "
                f"WHERE {self.key} = ? AND revision > ? ORDER BY revision LIMIT ?",
                (document, after_revision, limit),
            ).fetchall()
        )
        return {
            "revisions": [self.summary(row) for row in rows],
            "next_revision": rows[-1]["revision"] if rows else after_revision,
        }
