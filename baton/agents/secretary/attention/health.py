"""The latest engine check and retained sanitized failure history for one secretary."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .state import AttentionLog


@dataclass(frozen=True)
class EngineHealth:
    log: AttentionLog

    def read(self) -> dict:
        with self.log.connection() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_health'").fetchone()
            row = (
                conn.execute("SELECT result FROM engine_health WHERE principal=?", (self.log.principal,)).fetchone()
                if exists
                else None
            )
            return json.loads(row[0]) if row else {"status": "not_used", "last_failure": None, "checked_at": None}

    def record(self, health: dict) -> dict:
        with self.log.connection() as conn, conn.transaction():
            conn.execute("CREATE TABLE IF NOT EXISTS engine_health (principal TEXT PRIMARY KEY, result TEXT NOT NULL)")
            row = conn.execute("SELECT result FROM engine_health WHERE principal=?", (self.log.principal,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            now = time.time()
            result = {
                "status": health["status"],
                "last_failure": health["last_failure"] or previous.get("last_failure"),
                "last_failure_at": now if health["last_failure"] else previous.get("last_failure_at"),
                "checked_at": now,
            }
            conn.execute(
                "INSERT INTO engine_health VALUES (?,?) ON CONFLICT(principal) DO UPDATE SET result=excluded.result",
                (self.log.principal, json.dumps(result)),
            )
        return result
