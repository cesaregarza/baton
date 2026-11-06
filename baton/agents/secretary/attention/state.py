"""Private model assessments and acknowledged delivery positions; no message-body archive."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from baton import datadir
from baton.config import ConfigError
from baton.database import Connection
from baton.database.files import open_private
from baton.errors import InvalidRequest

_BASE_TABLES = {"attention_version", "principals", "assessments"}
_KNOWN_TABLES = _BASE_TABLES | {
    "acknowledgments",
    "backlog_settings",
    "backlog_windows",
    "engine_health",
    "model_health",
}


def _create_acknowledgments(conn: Connection) -> None:
    conn.execute(
        "CREATE TABLE acknowledgments (principal TEXT NOT NULL, decision_id TEXT NOT NULL, "
        "since INTEGER NOT NULL, covered_through INTEGER NOT NULL, "
        "PRIMARY KEY(principal,decision_id))"
    )
    conn.execute("CREATE INDEX acknowledgments_coverage ON acknowledgments(principal, covered_through, since)")


@dataclass(frozen=True)
class AttentionLog:
    path: Path
    data_dir: Path
    principal: str

    @contextmanager
    def connection(self) -> Generator[Connection]:
        path, data_dir = datadir.canonical(self.path), datadir.canonical(self.data_dir)
        if data_dir in path.parents and path.parent != data_dir:
            raise ConfigError("secretary state inside data_dir must be a direct child file")
        with open_private(path) as conn:
            # Schema inspection and first creation must share the same write lock:
            # two fresh openers otherwise both observe an empty database.
            with conn.transaction():
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and (not _BASE_TABLES <= tables or not tables <= _KNOWN_TABLES):
                    raise ConfigError(
                        "secretary state has an obsolete or unrelated schema; use a new private state file"
                    )
                if not tables:
                    conn.execute("CREATE TABLE attention_version (version INTEGER NOT NULL)")
                    conn.execute("INSERT INTO attention_version VALUES (2)")
                    conn.execute(
                        "CREATE TABLE principals (principal TEXT PRIMARY KEY, epoch TEXT NOT NULL, "
                        "floor INTEGER NOT NULL, delivered INTEGER NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE assessments (principal TEXT NOT NULL, id TEXT NOT NULL, "
                        "result TEXT NOT NULL, PRIMARY KEY(principal,id))"
                    )
                    _create_acknowledgments(conn)
                else:
                    version = conn.execute("SELECT version FROM attention_version").one()[0]
                    if version == 1:
                        raise ConfigError("secretary state version 1 is unsupported; use a new private state file")
                    if version != 2 or "acknowledgments" not in tables:
                        raise ConfigError("unsupported model secretary state version")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS acknowledgments_coverage ON acknowledgments"
                        "(principal, covered_through, since)"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS assessments_window ON assessments(principal,"
                    "json_extract(result,'$.policy_digest'),json_extract(result,'$.since'),"
                    "json_extract(result,'$.through_pos'),json_extract(result,'$.floor'),json_extract(result,'$.mode'))"
                )
            yield conn

    def positions(self) -> tuple[int, int]:
        with self.connection() as conn:
            row = conn.execute("SELECT delivered,floor FROM principals WHERE principal=?", (self.principal,)).fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def bind(self, epoch: str, floor: int) -> None:
        _position(floor, "floor")
        if not isinstance(epoch, str) or not epoch:
            raise InvalidRequest("secretary epoch must be a non-empty string")
        with self.connection() as conn, conn.transaction():
            conn.execute("INSERT OR IGNORE INTO principals VALUES (?,?,?,?)", (self.principal, epoch, floor, floor))
            row = conn.execute("SELECT epoch,floor FROM principals WHERE principal=?", (self.principal,)).one()
            if row[0] != epoch or row[1] != floor:
                raise ConfigError("secretary epoch or activation floor changed; use a new state file")

    def activate(self, epoch: str, head: int) -> tuple[int, int]:
        _position(head, "head")
        if not isinstance(epoch, str) or not epoch:
            raise InvalidRequest("secretary epoch must be a non-empty string")
        with self.connection() as conn, conn.transaction():
            conn.execute("INSERT OR IGNORE INTO principals VALUES (?,?,?,?)", (self.principal, epoch, head, head))
            row = conn.execute(
                "SELECT epoch,delivered,floor FROM principals WHERE principal=?", (self.principal,)
            ).one()
            if row[0] != epoch:
                raise ConfigError("secretary epoch changed; use a new state file")
            return row[1], row[2]

    def get(self, decision_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT result FROM assessments WHERE principal=? AND id=?", (self.principal, decision_id)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def remember(self, result: dict) -> dict:
        with self.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT OR IGNORE INTO assessments VALUES (?,?,?)",
                (self.principal, result["decision_id"], json.dumps(result, ensure_ascii=False)),
            )
            # Concurrent callbacks converge on the first completed assessment of this exact window.
            return json.loads(
                conn.execute(
                    "SELECT result FROM assessments WHERE principal=? AND id=?", (self.principal, result["decision_id"])
                ).one()[0]
            )

    def for_window(self, policy_digest: str, since: int, through: int, floor: int, mode: str) -> list[dict]:
        """Find completed results before retrying a page interrupted between persistence and queue completion."""
        with self.connection() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT result FROM assessments WHERE principal=? AND json_extract(result,'$.policy_digest')=? "
                    "AND json_extract(result,'$.since')=? AND json_extract(result,'$.through_pos')=? "
                    "AND json_extract(result,'$.floor')=? AND json_extract(result,'$.mode')=? ORDER BY rowid",
                    (self.principal, policy_digest, since, through, floor, mode),
                )
            ]

    def acknowledge(self, decision_id: str) -> int:
        with self.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT result FROM assessments WHERE principal=? AND id=?", (self.principal, decision_id)
            ).fetchone()
            if row is None:
                raise InvalidRequest("unknown secretary decision")
            result = json.loads(row[0])
            if result.get("coverage") != "complete":
                raise InvalidRequest("this decision does not have complete coverage")
            if not result["brief"]:
                raise InvalidRequest("this decision has no summary to acknowledge")
            since, covered_through = result.get("since"), result.get("covered_through")
            _position(since, "since")
            _position(covered_through, "covered_through")
            if covered_through < since:
                raise InvalidRequest("covered_through must be greater than or equal to since")
            conn.execute(
                "INSERT OR IGNORE INTO acknowledgments(principal,decision_id,since,covered_through) VALUES (?,?,?,?)",
                (self.principal, decision_id, since, covered_through),
            )
            return _advance_delivered(conn, self.principal)

    def acknowledge_empty(self, since: int, through: int) -> int:
        _position(since, "since")
        _position(through, "through")
        if through < since:
            raise InvalidRequest("through must be greater than or equal to since")
        with self.connection() as conn, conn.transaction():
            row = conn.execute("SELECT floor FROM principals WHERE principal=?", (self.principal,)).fetchone()
            if row is None:
                raise InvalidRequest("secretary principal is not activated")
            if since < row[0]:
                raise InvalidRequest("empty coverage must start at or after the activation floor")
            conn.execute(
                "INSERT OR IGNORE INTO acknowledgments(principal,decision_id,since,covered_through) VALUES (?,?,?,?)",
                (self.principal, f"empty:{since}:{through}", since, through),
            )
            return _advance_delivered(conn, self.principal)

    def is_acknowledged(self, decision_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM acknowledgments WHERE principal=? AND decision_id=?",
                (self.principal, decision_id),
            ).fetchone()
            return row is not None

    def recent(self) -> list[dict]:
        with self.connection() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT result FROM assessments WHERE principal=? ORDER BY rowid DESC LIMIT 100", (self.principal,)
                )
            ]


def _position(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise InvalidRequest(f"{name} must be a non-negative integer")


def _advance_delivered(conn: Connection, principal: str) -> int:
    delivered = conn.execute("SELECT delivered FROM principals WHERE principal=?", (principal,)).one()[0]
    rows = conn.execute(
        "SELECT since,covered_through FROM acknowledgments "
        "WHERE principal=? AND covered_through>? ORDER BY since,covered_through",
        (principal, delivered),
    )
    for row in rows:
        if row[0] > delivered:
            break
        if row[1] > delivered:
            delivered = row[1]
    if delivered:
        conn.execute("UPDATE principals SET delivered=? WHERE principal=?", (delivered, principal))
    return delivered
