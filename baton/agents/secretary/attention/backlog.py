"""Durable pages waiting for assessment, retry, digest, or delivery."""

from __future__ import annotations

from dataclasses import dataclass

from baton.database import Connection
from baton.errors import InvalidRequest

from .state import AttentionLog, _position


@dataclass(frozen=True)
class Page:
    since: int
    through: int
    mode: str = "alert"
    attempts: int = 0
    content_failures: int = 0
    model_successes: int = 0
    model_policy: str = ""


class Backlog:
    """Own pending work; acknowledged assessments and receipts remain durable."""

    def __init__(self, log: AttentionLog):
        self.log = log

    def _initialize(self, conn: Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS backlog_settings (principal TEXT PRIMARY KEY, scanned INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS backlog_windows (principal TEXT NOT NULL, since INTEGER NOT NULL, "
            "through INTEGER NOT NULL, next_attempt REAL NOT NULL, decision TEXT, PRIMARY KEY(principal,since,through))"
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(backlog_windows)")}
        additions = {
            "mode": "TEXT NOT NULL DEFAULT 'alert'",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'retry'",
            "error": "TEXT",
            "content_failures": "INTEGER NOT NULL DEFAULT 0",
            "model_successes": "INTEGER NOT NULL DEFAULT 0",
            "model_policy": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE backlog_windows ADD COLUMN {name} {declaration}")
        if "status" not in columns:
            # Old NULL rows did not distinguish held work from failures. Reassess them promptly.
            conn.execute(
                "UPDATE backlog_windows SET status=CASE WHEN decision IS NULL THEN 'retry' ELSE 'ready' END, "
                "next_attempt=0"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS backlog_due ON backlog_windows(principal,status,next_attempt,since)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS backlog_ready ON backlog_windows(principal,status,since,through) "
            "WHERE status='ready'"
        )
        conn.execute(
            "INSERT OR IGNORE INTO backlog_settings SELECT principal,delivered FROM principals WHERE principal=?",
            (self.log.principal,),
        )

    def scanned(self) -> int:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            row = conn.execute(
                "SELECT scanned FROM backlog_settings WHERE principal=?", (self.log.principal,)
            ).fetchone()
            if row is None:
                raise InvalidRequest("activate the secretary before scanning its backlog")
            return row[0]

    def enqueue(self, page: Page, now: float) -> None:
        _range(page)
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            self._insert(conn, page, now)
            conn.execute(
                "UPDATE backlog_settings SET scanned=MAX(scanned,?) WHERE principal=?",
                (page.through, self.log.principal),
            )

    def _insert(self, conn: Connection, page: Page, now: float) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO backlog_windows"
            "(principal,since,through,next_attempt,mode,attempts,content_failures,model_successes,model_policy,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,'retry')",
            (
                self.log.principal,
                page.since,
                page.through,
                now,
                page.mode,
                page.attempts,
                page.content_failures,
                page.model_successes,
                page.model_policy,
            ),
        )

    def finish(self, page: Page, result: dict, now: float, digest_seconds: float) -> None:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            if result["brief"]:
                status, mode, due, decision = "ready", result["mode"], 0, result["decision_id"]
            else:
                scheduled = conn.execute(
                    "SELECT MIN(next_attempt) FROM backlog_windows WHERE principal=? AND status='held'",
                    (self.log.principal,),
                ).one()[0]
                status, mode, due, decision = "held", "digest", scheduled or now + digest_seconds, None
            conn.execute(
                "UPDATE backlog_windows SET status=?,mode=?,next_attempt=?,decision=?,"
                "attempts=0,content_failures=0,model_successes=0,model_policy='',error=NULL "
                "WHERE principal=? AND since=? AND through=? AND status='retry'",
                (status, mode, due, decision, self.log.principal, page.since, page.through),
            )
            self._prune(conn)

    def model_succeeded(self, conn: Connection, page: Page, successes: int, policy: str) -> None:
        """A page's own answer cannot provide evidence that another page works."""
        self._initialize(conn)
        conn.execute(
            "UPDATE backlog_windows SET model_successes=?,model_policy=? "
            "WHERE principal=? AND since=? AND through=? AND mode=? AND status='retry'",
            (successes, policy, self.log.principal, page.since, page.through, page.mode),
        )

    def failed(
        self,
        page: Page,
        now: float,
        error: str,
        *,
        initial: float,
        maximum: float,
        content: bool = False,
        model_successes: int = 0,
        model_policy: str = "",
    ) -> Page | None:
        attempts = page.attempts + 1
        content_failures = page.content_failures + int(content)
        delay = min(maximum, initial * 2 ** min(attempts - 1, 20))
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            changed = conn.execute(
                "UPDATE backlog_windows SET status='retry',attempts=?,content_failures=?,next_attempt=?,error=?,"
                "model_successes=?,model_policy=? "
                "WHERE principal=? AND since=? AND through=? AND mode=? AND status='retry' "
                "AND attempts=? AND model_successes=? AND model_policy=?",
                (
                    attempts,
                    content_failures,
                    now + delay,
                    error,
                    model_successes,
                    model_policy,
                    self.log.principal,
                    page.since,
                    page.through,
                    page.mode,
                    page.attempts,
                    page.model_successes,
                    page.model_policy,
                ),
            ).rowcount
        if not changed:
            return None
        return Page(page.since, page.through, page.mode, attempts, content_failures, model_successes, model_policy)

    def due(
        self, now: float, *, held: bool = False, limit: int = 1, position: int | None = None, mode: str | None = None
    ) -> list[Page]:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            rows = conn.execute(
                "SELECT since,through,mode,attempts,content_failures,model_successes,model_policy FROM backlog_windows "
                "WHERE principal=? AND status=? "
                "AND next_attempt<=? AND (? IS NULL OR (since<? AND through>=?)) "
                "AND (? IS NULL OR mode=?) ORDER BY next_attempt,since LIMIT ?",
                (self.log.principal, "held" if held else "retry", now, position, position, position, mode, mode, limit),
            )
            return [Page(*row) for row in rows]

    def waiting_retry(self, position: int | None = None, *, mode: str | None = None) -> bool:
        """Whether this trigger belongs to retained failed work, even during its backoff."""
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            return (
                conn.execute(
                    "SELECT 1 FROM backlog_windows WHERE principal=? AND status='retry' "
                    "AND (? IS NULL OR (since<? AND through>=?)) AND (? IS NULL OR mode=?) LIMIT 1",
                    (self.log.principal, position, position, position, mode, mode),
                ).fetchone()
                is not None
            )

    def replace(self, originals: list[Page], replacements: list[Page], now: float) -> None:
        """Partition failed work or combine held pages without deleting assessments."""
        if not originals or not replacements or _spans(originals) != _spans(replacements):
            raise InvalidRequest("replacement pages must cover exactly the same event range")
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            for page in originals:
                conn.execute(
                    "DELETE FROM backlog_windows WHERE principal=? AND since=? AND through=?",
                    (self.log.principal, page.since, page.through),
                )
            for page in replacements:
                self._insert(conn, page, now)

    def discard_empty(self, page: Page) -> None:
        self.log.acknowledge_empty(page.since, page.through)
        with self.log.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM backlog_windows WHERE principal=? AND since=? AND through=?",
                (self.log.principal, page.since, page.through),
            )

    def ready(self) -> list[str]:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            self._prune(conn)
            return [
                row[0]
                for row in conn.execute(
                    "SELECT decision FROM backlog_windows WHERE principal=? AND status='ready' ORDER BY since,through",
                    (self.log.principal,),
                )
            ]

    def invalidate(self, decision: str, now: float) -> None:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            self._prune(conn)
            conn.execute(
                "UPDATE backlog_windows SET decision=NULL,status='retry',attempts=0,content_failures=0,"
                "model_successes=0,model_policy='',next_attempt=? "
                "WHERE principal=? AND decision=?",
                (now, self.log.principal, decision),
            )

    def _prune(self, conn: Connection) -> None:
        conn.execute(
            "DELETE FROM backlog_windows WHERE principal=? AND status='ready' AND EXISTS "
            "(SELECT 1 FROM acknowledgments AS a WHERE a.principal=backlog_windows.principal "
            "AND a.decision_id=backlog_windows.decision)",
            (self.log.principal,),
        )

    def failures(self) -> int:
        with self.log.connection() as conn, conn.transaction():
            self._initialize(conn)
            return conn.execute(
                "SELECT COUNT(*) FROM backlog_windows WHERE principal=? AND status='retry' AND attempts>0",
                (self.log.principal,),
            ).one()[0]


def _range(page: Page) -> None:
    _position(page.since, "since")
    _position(page.through, "through")
    if page.through < page.since or page.mode not in {"alert", "digest"}:
        raise InvalidRequest("invalid secretary page range or mode")


def _spans(pages: list[Page]) -> tuple[int, int]:
    ordered = sorted(pages, key=lambda page: page.since)
    for page in ordered:
        _range(page)
    if any(left.through != right.since for left, right in zip(ordered, ordered[1:], strict=False)):
        raise InvalidRequest("page ranges must be contiguous")
    return ordered[0].since, ordered[-1].through
