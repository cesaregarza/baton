"""Durable scan windows for the secretary worker."""

from __future__ import annotations

import math

from baton.errors import InvalidRequest

from .state import AttentionLog, _position


class Backlog:
    """Persist scan progress and retryable attention windows beside the model log."""

    def __init__(self, log: AttentionLog) -> None:
        self.log = log

    def _initialize(self, conn) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS backlog_settings (principal TEXT PRIMARY KEY, scanned INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS backlog_windows ("
            "principal TEXT NOT NULL, since INTEGER NOT NULL, through INTEGER NOT NULL, "
            "next_attempt REAL NOT NULL, decision TEXT, PRIMARY KEY(principal,since,through))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO backlog_settings(principal,scanned) "
            "SELECT principal,delivered FROM principals WHERE principal=?",
            (self.log.principal,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO backlog_settings(principal,scanned) VALUES (?,0)",
            (self.log.principal,),
        )

    def scanned(self) -> int:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            return conn.execute("SELECT scanned FROM backlog_settings WHERE principal=?", (self.log.principal,)).one()[
                0
            ]

    def record(self, since: int, through: int, *, decision: str | None, now: float) -> None:
        _window(since, through, decision, now)
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            conn.execute(
                "UPDATE backlog_settings SET scanned=MAX(scanned,?) WHERE principal=?",
                (through, self.log.principal),
            )
            conn.execute(
                "INSERT INTO backlog_windows(principal,since,through,next_attempt,decision) "
                "VALUES (?,?,?,?,?) ON CONFLICT(principal,since,through) DO UPDATE SET "
                "next_attempt=excluded.next_attempt, decision=COALESCE(excluded.decision,backlog_windows.decision)",
                (self.log.principal, since, through, now + 3600, decision),
            )

    def due(self, now: float) -> tuple[int, int] | None:
        _time(now)
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            row = conn.execute(
                "SELECT w.since,w.through FROM backlog_windows AS w "
                "LEFT JOIN acknowledgments AS a ON a.principal=w.principal AND a.decision_id=w.decision "
                "WHERE w.principal=? AND w.decision IS NULL AND w.next_attempt<=? AND a.decision_id IS NULL "
                "ORDER BY w.since,w.through LIMIT 1",
                (self.log.principal, now),
            ).fetchone()
            return (row[0], row[1]) if row else None

    def ready(self) -> list[str]:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            return [
                row[0]
                for row in conn.execute(
                    "SELECT w.decision FROM backlog_windows AS w "
                    "LEFT JOIN acknowledgments AS a ON a.principal=w.principal AND a.decision_id=w.decision "
                    "WHERE w.principal=? AND w.decision IS NOT NULL AND a.decision_id IS NULL "
                    "ORDER BY w.since,w.through",
                    (self.log.principal,),
                )
            ]

    def invalidate(self, decision: str, now: float) -> None:
        if not isinstance(decision, str) or not decision:
            raise InvalidRequest("decision must be a non-empty string")
        _time(now)
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            conn.execute(
                "UPDATE backlog_windows SET decision=NULL,next_attempt=? WHERE principal=? AND decision=? "
                "AND NOT EXISTS (SELECT 1 FROM acknowledgments WHERE principal=? AND decision_id=?)",
                (now, self.log.principal, decision, self.log.principal, decision),
            )


def _window(since: int, through: int, decision: str | None, now: float) -> None:
    _position(since, "since")
    _position(through, "through")
    if through < since:
        raise InvalidRequest("through must be greater than or equal to since")
    if decision is not None and (not isinstance(decision, str) or not decision):
        raise InvalidRequest("decision must be a non-empty string or None")
    _time(now)


def _time(now: float) -> None:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise InvalidRequest("now must be a finite timestamp")
