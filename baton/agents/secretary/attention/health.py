"""The latest engine check and retained sanitized failure history for one secretary."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .state import AttentionLog

_LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ModelHealth:
    """Track actual model answers under one policy, independently of engine verdicts and cached results."""

    log: AttentionLog
    policy_digest: str

    def _read(self, conn) -> dict:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_health'").fetchone()
        row = (
            conn.execute(
                "SELECT result FROM model_health WHERE principal=? AND policy_digest=?",
                (self.log.principal, self.policy_digest),
            ).fetchone()
            if exists
            else None
        )
        return (
            json.loads(row[0])
            if row
            else {"status": "not_used", "last_failure": None, "checked_at": None, "successes": 0}
        )

    def read(self) -> dict:
        with self.log.connection() as conn:
            return self._read(conn)

    def succeeded(self) -> dict:
        return self._record(None)

    def failed(self, category: str) -> dict:
        _LOGGER.warning("secretary model unavailable: %s", category)
        return self._record(category)

    def _record(self, failure: str | None) -> dict:
        with self.log.connection() as conn, conn.transaction():
            previous = self._read(conn)
            now = time.time()
            result = {
                "status": "unavailable" if failure else "available",
                "last_failure": failure or previous.get("last_failure"),
                "last_failure_at": now if failure else previous.get("last_failure_at"),
                "checked_at": now,
                "successes": previous["successes"] + int(failure is None),
            }
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_health "
                "(principal TEXT PRIMARY KEY, policy_digest TEXT NOT NULL, result TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO model_health VALUES (?,?,?) ON CONFLICT(principal) DO UPDATE SET "
                "policy_digest=excluded.policy_digest,result=excluded.result",
                (self.log.principal, self.policy_digest, json.dumps(result)),
            )
        return result
