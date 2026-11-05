"""Private model assessments and acknowledged delivery positions; no message-body archive."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from baton import datadir
from baton.config import ConfigError
from baton.database.files import open_private
from baton.errors import InvalidRequest


@dataclass(frozen=True)
class AttentionLog:
    path: Path
    data_dir: Path
    principal: str

    @contextmanager
    def connection(self):
        path, data_dir = datadir.canonical(self.path), datadir.canonical(self.data_dir)
        if data_dir in path.parents and path.parent != data_dir:
            raise ConfigError("secretary state inside data_dir must be a direct child file")
        with open_private(path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and not {"attention_version", "principals", "assessments"} <= tables:
                raise ConfigError("model secretary needs a separate version-2 state file; "
                                  "this file is legacy or unrelated")
            if not tables:
                with conn.transaction():
                    conn.execute("CREATE TABLE attention_version (version INTEGER NOT NULL)")
                    conn.execute("INSERT INTO attention_version VALUES (1)")
                    conn.execute("CREATE TABLE principals (principal TEXT PRIMARY KEY, epoch TEXT NOT NULL, "
                                 "floor INTEGER NOT NULL, delivered INTEGER NOT NULL)")
                    conn.execute("CREATE TABLE assessments (principal TEXT NOT NULL, id TEXT NOT NULL, "
                                 "result TEXT NOT NULL, PRIMARY KEY(principal,id))")
            elif conn.execute("SELECT version FROM attention_version").fetchone()[0] != 1:
                raise ConfigError("unsupported model secretary state version")
            yield conn

    def positions(self) -> tuple[int, int]:
        with self.connection() as conn:
            row = conn.execute("SELECT delivered,floor FROM principals WHERE principal=?", (self.principal,)).fetchone()
            return (row[0], row[1]) if row else (0, 0)

    def bind(self, epoch: str, floor: int) -> None:
        with self.connection() as conn, conn.transaction():
            conn.execute("INSERT OR IGNORE INTO principals VALUES (?,?,?,?)", (self.principal, epoch, floor, floor))
            row = conn.execute("SELECT epoch,floor FROM principals WHERE principal=?", (self.principal,)).fetchone()
            if row[0] != epoch or row[1] != floor:
                raise ConfigError("secretary epoch or activation floor changed; use a new state file")

    def get(self, decision_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result FROM assessments WHERE principal=? AND id=?",
                               (self.principal, decision_id)).fetchone()
            return json.loads(row[0]) if row else None

    def remember(self, result: dict) -> dict:
        with self.connection() as conn, conn.transaction():
            conn.execute("INSERT OR IGNORE INTO assessments VALUES (?,?,?)",
                         (self.principal, result["decision_id"], json.dumps(result, ensure_ascii=False)))
            # Concurrent callbacks converge on the first completed assessment of this exact window.
            return json.loads(conn.execute("SELECT result FROM assessments WHERE principal=? AND id=?",
                                           (self.principal, result["decision_id"])).fetchone()[0])

    def acknowledge(self, decision_id: str) -> int:
        with self.connection() as conn, conn.transaction():
            row = conn.execute("SELECT result FROM assessments WHERE principal=? AND id=?",
                               (self.principal, decision_id)).fetchone()
            if row is None:
                raise InvalidRequest("unknown secretary decision")
            result = json.loads(row[0])
            if not result["brief"]:
                raise InvalidRequest("this decision has no summary to acknowledge")
            conn.execute("UPDATE principals SET delivered=MAX(delivered,?) WHERE principal=?",
                         (result["covered_through"], self.principal))
            return conn.execute("SELECT delivered FROM principals WHERE principal=?", (self.principal,)).fetchone()[0]

    def recent(self) -> list[dict]:
        with self.connection() as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT result FROM assessments WHERE principal=? ORDER BY rowid DESC LIMIT 100", (self.principal,))]
