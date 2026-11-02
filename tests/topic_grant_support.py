"""Shared setup for the topic grant tests: a database with aliases and topics, a clock, and checks that
say what is missing instead of raising.

`baton/topic_grants.py` and its tables may not exist yet. Nothing here imports the module at the top
level: `grants_module()` asserts it exists first, so against a base without it every test that needs it
fails on that assertion and never as an ImportError, a collection error or an OperationalError.

The clock is patched into the module's `_now()`, as the module's contract says. The digest and the alias
code are recomputed here from the spec, not by the code under test, so a digest that drifts from the
definition is caught.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from test_topic_containers import ALIAS, OTHER_ALIAS, seed_aliases, topic_config
from topic_backing import back_topics

from baton.config import Config
from baton.errors import BatonError
from baton.store import connect

START = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
CAPABILITIES = ("reference.edit", "thread.organise", "topic.create_sub")
# A root, a subtopic of it, a root whose name merely starts with the first one's, and a subtopic of that.
ROOT, CHILD, LOOKALIKE, LOOKALIKE_CHILD = "job-hunt", "job-hunt/x", "job-hunting", "job-hunting/x"
GRANT_COLUMNS = {"id", "alias_id", "topic_id", "capabilities", "inherit", "expires_at", "proposed_by", "proposed_at",
                 "proposal_expires_at", "nonce", "digest", "authorized_by", "authorized_at", "authorized_digest",
                 "proof_digest", "revoked_at", "revoked_by", "revoke_reason", "alias_name_seen", "alias_code_seen",
                 "topic_path_seen"}
PROPOSAL_COLUMNS = ("id", "alias_id", "topic_id", "capabilities", "inherit", "expires_at", "proposed_by",
                    "proposed_at", "proposal_expires_at", "nonce", "digest", "alias_name_seen", "alias_code_seen",
                    "topic_path_seen")
AUTHORIZATION_COLUMNS = ("authorized_by", "authorized_at", "authorized_digest", "proof_digest")


def grants_module() -> ModuleType:
    """The module under test, or a failed assertion saying it is not there."""
    assert importlib.util.find_spec("baton.topic_grants") is not None, "baton/topic_grants.py does not exist yet"
    module = importlib.import_module("baton.topic_grants")
    for name in ("propose", "authorize", "revoke", "list_grants", "covering_grant", "_now"):
        assert hasattr(module, name), f"baton.topic_grants has no {name}"
    return module


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def require_tables(db: sqlite3.Connection) -> None:
    for name in ("topic_grants", "topic_grant_events"):
        assert table_exists(db, name), f"there is no {name} table"


def alias_code(alias_id: str) -> str:
    """The alias's 4-hex code, as `baton-alias-code-v1` defines it."""
    return hashlib.sha256(b"baton-alias-code-v1\x00" + alias_id.encode()).hexdigest()[:4]


def canonical_digest(row: sqlite3.Row | dict[str, Any]) -> str:
    """The digest the spec defines, from a grant row's own columns."""
    obj = {"alias_id": row["alias_id"], "alias_name": row["alias_name_seen"], "alias_code": row["alias_code_seen"],
           "topic_id": row["topic_id"], "topic_path": row["topic_path_seen"],
           "capabilities": json.loads(row["capabilities"]), "inherit": row["inherit"],
           "expires_at": row["expires_at"], "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
           "nonce": row["nonce"]}
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(text).hexdigest()


def every_cell(db: sqlite3.Connection) -> list[str]:
    """Every value in every ordinary table, as text: for proving a secret is stored nowhere."""
    cells = []
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                              " AND name NOT LIKE '%\\_fts%' ESCAPE '\\'").fetchall():
        for row in db.execute(f"SELECT * FROM {name}"):
            cells.extend(str(value) for value in tuple(row) if value is not None)
    return cells


def refusal(operation: Any, *args: Any, **kwargs: Any) -> BatonError:
    """Run an operation that must be refused, and hand back the refusal."""
    with pytest.raises(BatonError) as raised:
        operation(*args, **kwargs)
    return raised.value


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


class World:
    """A database with two aliases and a small tree of topics, a patched clock, and short names for the operations."""

    def __init__(self, config: Config, db: sqlite3.Connection, tg: ModuleType, clock: Clock) -> None:
        self.config, self.db, self.tg, self.clock = config, db, tg, clock
        self.alias_a, self.alias_b = ALIAS, OTHER_ALIAS

    def topic_id(self, path: str) -> str:
        return self.db.execute("SELECT id FROM topics WHERE path = ?", (path,)).fetchone()["id"]

    def propose(self, alias: str = "lane-a", topic: str = ROOT, capabilities: Any = ("reference.edit",),
                inherit: bool = False, days: int = 7) -> dict[str, Any]:
        return self.tg.propose(self.db, alias, topic, list(capabilities), inherit, days)

    def authorize(self, proposed: dict[str, Any]) -> dict[str, Any]:
        return self.tg.authorize(self.db, proposed["id"], proposed["digest"])

    def activate(self, **kwargs: Any) -> dict[str, Any]:
        """Propose and authorize in one go; the result is the authorization, key included."""
        return self.authorize(self.propose(**kwargs))

    def revoke(self, grant_id: str, reason: str = "no longer needed") -> Any:
        return self.tg.revoke(self.db, grant_id, reason)

    def row(self, grant_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM topic_grants WHERE id = ?", (grant_id,)).fetchone()

    def rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM topic_grants ORDER BY rowid").fetchall()

    def events(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM topic_grant_events ORDER BY seq").fetchall()

    def state(self, grant_id: str) -> str:
        [found] = [one for one in self.tg.list_grants(self.db, include_all=True) if one["id"] == grant_id]
        return found["state"]

    def covering(self, topic: str, capability: str = "reference.edit", alias_id: str | None = None) -> str | None:
        found = self.tg.covering_grant(self.db, alias_id or self.alias_a["alias_id"], topic, capability)
        return None if found is None else found["id"]

    def reid(self, proposal: dict[str, Any], new_id: str) -> dict[str, Any]:
        """Give a pending proposal a chosen id, so a test of ordering by id does not depend on random ones."""
        self.db.execute("UPDATE topic_grants SET id = ? WHERE id = ?", (new_id, proposal["id"]))
        self.db.execute("UPDATE topic_grant_events SET grant_id = ? WHERE grant_id = ?", (new_id, proposal["id"]))
        return {**proposal, "id": new_id}

    def edit(self, grant_id: str, **columns: Any) -> None:
        """Change a stored grant behind the module's back, as a tamperer with the database file would."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.db.execute(f"UPDATE topic_grants SET {assignments} WHERE id = ?", (*columns.values(), grant_id))


@pytest.fixture
def build_world(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A factory, called inside the test. Its checks are assertions in the test body: an assertion inside a
    fixture would be reported as a setup error, not as the failure it is."""
    opened: list[sqlite3.Connection] = []

    def build() -> World:
        module = grants_module()
        clock = Clock()
        monkeypatch.setattr(module, "_now", clock)
        config = topic_config(tmp_path)
        db = connect(config.db_path)
        opened.append(db)
        require_tables(db)
        seed_aliases(db, ALIAS, OTHER_ALIAS)
        back_topics(db, ROOT, CHILD, LOOKALIKE, LOOKALIKE_CHILD, "reviews")
        return World(config, db, module, clock)

    yield build
    for db in opened:
        db.close()


def bring_to(w: World, name: str) -> str:
    """A grant for lane-a on the root, left in the named state by the clock at its current reading."""
    if name == "pending":
        return w.propose()["id"]
    if name == "lapsed":
        made = w.propose()["id"]
        w.clock.advance(hours=24)
        return made
    if name == "active":
        return w.activate()["id"]
    if name == "expired":
        made = w.activate(days=1)["id"]
        w.clock.advance(days=1)
        return made
    if name == "invalid":
        made = w.activate()["id"]
        w.edit(made, capabilities=json.dumps(list(CAPABILITIES)))
        return made
    raise AssertionError(f"no such state to bring a grant to: {name}")
