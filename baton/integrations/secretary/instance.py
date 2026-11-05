"""One installed secretary: configuration, session binding, and delivery receipts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from baton import config, datadir
from baton.agents.secretary import Secretary
from baton.config import ConfigError
from baton.database.files import open_private


@dataclass(frozen=True)
class Instance:
    path: Path
    product: str
    project: Path
    secretary: Secretary
    poll_seconds: float
    budget_seconds: float
    codex: str | None

    @classmethod
    def load(cls, path: Path) -> Instance:
        path = datadir.canonical(path)
        with datadir.opened(path.parent) as directory:
            fd = datadir.open_file(directory, path.name, os.O_RDONLY, directory=path.parent)
            with os.fdopen(fd) as source:
                value = json.load(source)
        if value.get("version") != 1 or value.get("product") not in {"codex", "claude-code"}:
            raise ConfigError("unsupported secretary integration")
        for name in ("project", "config", "policy", "state"):
            if not isinstance(value.get(name), str) or not Path(value[name]).is_absolute():
                raise ConfigError(f"integration {name} must be an absolute path")
        if Path(value["state"]).parent != path.parent:
            raise ConfigError("integration state must be in its private instance directory")
        for name, maximum in (("poll_seconds", 60), ("budget_seconds", 60)):
            if type(value.get(name)) not in (int, float) or not 0 < value[name] <= maximum:
                raise ConfigError(f"{name} must be greater than zero and at most {maximum}")
        executable = value.get("codex")
        if value["product"] == "codex" and (
                not isinstance(executable, str) or not Path(executable).is_absolute()
                or not os.access(executable, os.X_OK)):
            raise ConfigError("codex must name an absolute executable path")
        return cls(path, value["product"], Path(value["project"]),
                   Secretary(config.load(value["config"]), Path(value["state"]), Path(value["policy"])),
                   value["poll_seconds"], value["budget_seconds"], executable)

    @property
    def signal_path(self) -> Path:
        return self.path.parent / "pending.json"

    @property
    def receipts(self) -> Receipts:
        return Receipts(self.path.parent / "delivery.sqlite")


@dataclass(frozen=True)
class Receipts:
    """Small durable host ledger; model assessments belong to the secretary's own store."""

    path: Path

    def connection(self):
        conn = open_private(self.path)
        with conn.transaction():
            conn.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS offered (session TEXT, decision TEXT, "
                         "PRIMARY KEY(session,decision))")
        return conn

    def get(self, name: str, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, name: str, value) -> None:
        with self.connection() as conn, conn.transaction():
            conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                         (name, json.dumps(value)))

    def bind(self, session: str, *, replace: bool = False) -> bool:
        if not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", session):
            raise ConfigError("invalid session id")
        with self.connection() as conn, conn.transaction():
            if replace:
                conn.execute("DELETE FROM settings WHERE name='session'")
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('session',?)", (json.dumps(session),))
            return json.loads(conn.execute("SELECT value FROM settings WHERE name='session'").fetchone()[0]) == session

    def offer(self, session: str, decision: str) -> bool:
        """Each hook offers a summary once per session. This is not delivery acknowledgment."""
        with self.connection() as conn, conn.transaction():
            return bool(conn.execute("INSERT OR IGNORE INTO offered VALUES (?,?)", (session, decision)).rowcount)
