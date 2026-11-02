"""Shared setup for the tests of the topic grant check and the scoped topic tools, all driven over MCP.

A `Scene` is one host with a guest login, a few core roles and a patched grant clock. Its grants are real:
proposed and authorized through `baton.topic_grants`, never inserted as a row, so every key is one the module
minted. A `Grantee` is a claimed alias holding one such grant, and presents both halves of what a grantee carries:
the alias's binding token (`token`, who it is) and the grant key (`grant_token`, which grant it uses).

Nothing secret is written here. Passphrases, binding tokens and grant keys are made at run time. The board is
a small invented tree, with a topic whose name merely begins with another's (`job-hunting`), to catch a prefix test.

Helpers fail by assertion, never by raising from inside a fixture or on a missing key: `succeeded` and `only`
turn "the call was refused" and "there is no such row" into a failed assertion that shows the result.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any

import pytest
from conftest import make_config
from test_aliases import dispatch, payload, rpc
from topic_grant_support import Clock, grants_module

from baton import auth, store

PASSPHRASE = "synthetic-" + secrets.token_urlsafe(12)
ALL = ("reference.edit", "thread.organise", "topic.create_sub")
BOARD = ("job-hunt", "job-hunt/boards", "job-hunt/leads", "job-hunting", "resume")
MEMBERS = ["planner", "worker"]

ROLES = """
[roles.steward]
description = "A core role an alias can be enrolled as"
job_authority = false
can_create_conversations = true
can_create_topics = true
can_manage_participants = true
can_view_all_conversations = true
[roles.guest]
description = "Shared login"
can_view_all_conversations = true
[conversations]
enabled = true
[aliases]
max_claims_per_alias_per_hour = 100
"""


def build_host(tmp_path: Any) -> Any:
    """A host with a planner that runs topics, a worker that does not, an enrollable steward and a guest login."""
    made = {role: auth.new_token() for role in ("steward", "guest")}
    entries = "".join(f'[[auth.tokens]]\nname = "{role}-token"\nrole = "{role}"\nsha256 = "{digest}"\n'
                      for role, (_, digest) in made.items())
    built = make_config(tmp_path, extra=ROLES + entries)
    built.tokens.update({role: token for role, (token, _) in made.items()})
    built.config_path = tmp_path / "baton.toml"
    manager = {"can_create_conversations": True, "can_create_topics": True, "can_manage_participants": True,
               "can_view_all_conversations": True}
    built.config = replace(built.config, search=replace(built.config.search, enabled=True),
                           roles={**built.config.roles, "planner": replace(built.config.roles["planner"], **manager)})
    return built


def play(host: Any, scenario: Any) -> None:
    """dispatch(), but a failed assertion in the scenario surfaces as itself, not inside an ExceptionGroup."""
    failures: list[BaseException] = []

    async def guarded(client: Any) -> None:
        try:
            await scenario(client)
        except BaseException as error:  # noqa: BLE001 -- re-raised below, once the host has shut down
            failures.append(error)

    dispatch(host, guarded)
    if failures:
        raise failures[0]


async def tool(client: Any, host: Any, name: str, arguments: dict[str, Any] | None = None,
               role: str = "guest") -> dict[str, Any]:
    """A tool's result, or the refusal as given. A reply that is not JSON comes back as {"unparsed": text}."""
    response = await rpc(client, host, "tools/call", {"name": name, "arguments": arguments or {}}, role=role)
    try:
        return payload(response)
    except (ValueError, KeyError, IndexError):
        return {"unparsed": str(response)}


def succeeded(result: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in result and "unparsed" not in result, result
    return result


def refused(result: dict[str, Any], code: str, detail: str | None = None) -> dict[str, Any]:
    assert result.get("error") == code, result
    if detail is not None:
        assert result.get("detail") == detail, result
    return result


def only(rows: list[Any], what: str) -> Any:
    assert len(rows) == 1, f"expected exactly one {what}, found {len(rows)}: {[tuple(row) for row in rows]}"
    return rows[0]


def new_key() -> str:
    """A key-shaped string no grant has."""
    return "tgk_" + secrets.token_urlsafe(32)


@dataclass
class Grantee:
    """A claimed alias holding one grant: both halves of what a guest presents to use it."""

    host: Any
    name: str
    token: str
    alias_id: str
    code: str
    grant_id: str
    key: str

    @property
    def actor(self) -> str:
        """The name topic history gives a guest acting through this alias."""
        return f"guest via alias {self.alias_id}"

    def arguments(self, **arguments: Any) -> dict[str, Any]:
        return {**arguments, "token": self.token, "grant_token": self.key}

    async def call(self, client: Any, name: str, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.host, name, self.arguments(**arguments))

    async def call_keyless(self, client: Any, name: str, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.host, name, {**arguments, "token": self.token})


@dataclass
class Scene:
    """One host, the grant module with its clock patched, and short names for what the tests read back."""

    host: Any
    tg: ModuleType
    clock: Clock

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        conn = store.connect(self.host.config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def query(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        with self.db() as conn:
            return conn.execute(sql, params).fetchall()

    def grant(self, alias: str, topic: str, capabilities: Any = ALL, inherit: bool = False,
              days: int = 7) -> dict[str, Any]:
        """Propose and authorize a grant through the module; the result carries the key, shown once."""
        with self.db() as conn:
            proposal = self.tg.propose(conn, alias, topic, list(capabilities), inherit, days)
            return self.tg.authorize(conn, proposal["id"], proposal["digest"])

    def revoke(self, grant_id: str, reason: str = "no longer needed") -> None:
        with self.db() as conn:
            self.tg.revoke(conn, grant_id, reason)

    def state_of(self, grant_id: str) -> str:
        with self.db() as conn:
            [found] = [one for one in self.tg.list_grants(conn, include_all=True) if one["id"] == grant_id]
        return found["state"]

    def edit_grant(self, grant_id: str, **columns: Any) -> None:
        """Change a stored grant behind the module's back, as a tamperer with the database file would."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.db() as conn:
            conn.execute(f"UPDATE topic_grants SET {assignments} WHERE id = ?", (*columns.values(), grant_id))

    async def grantee(self, client: Any, alias: str, topic: str, capabilities: Any = ALL, inherit: bool = False,
                      days: int = 7) -> Grantee:
        claimed = await tool(client, self.host, "claim_alias", {"name": alias, "passphrase": PASSPHRASE})
        assert claimed.get("binding_token"), claimed
        made = self.grant(alias, topic, capabilities, inherit, days)
        return Grantee(self.host, alias, claimed["binding_token"], claimed["alias_id"], claimed["code"],
                       made["id"], made["grant_key"])

    def another_grant(self, held: Grantee, topic: str, capabilities: Any = ALL, inherit: bool = False) -> Grantee:
        """A second grant for the same alias, so the alias holds two, each with its own key."""
        made = self.grant(held.name, topic, capabilities, inherit)
        return replace(held, grant_id=made["id"], key=made["grant_key"])

    def topic_id(self, path: str) -> str:
        return only(self.query("SELECT id FROM topics WHERE path = ?", path), f"topic at {path}")["id"]

    def topic_paths(self) -> list[str]:
        return [row["path"] for row in self.query("SELECT path FROM topics ORDER BY path")]

    def members(self, path: str) -> list[str]:
        return json.loads(only(self.query("SELECT members FROM topics WHERE path = ?", path), path)["members"])

    def changes(self, operation: str | None = None) -> list[sqlite3.Row]:
        rows = self.query("SELECT * FROM topic_changes ORDER BY rowid")
        return [row for row in rows if operation in (None, row["operation"])]

    def events(self, event: str | None = None, grant_id: str | None = None) -> list[sqlite3.Row]:
        rows = self.query("SELECT * FROM topic_grant_events ORDER BY seq")
        return [row for row in rows if event in (None, row["event"]) and grant_id in (None, row["grant_id"])]

    def thread_topic(self, conversation_id: str) -> str | None:
        return only(self.query("SELECT topic FROM conversations WHERE id = ?", conversation_id),
                    "conversation")["topic"]

    def moved_notices(self, conversation_id: str) -> list[tuple[str, dict[str, Any], str]]:
        """Every topic_moved notice stored for a thread: (recipient, notice, text), in recipient order."""
        rows = self.query("SELECT recipient, notice, text, sender FROM messages WHERE conversation_id = ?"
                          " AND notice IS NOT NULL ORDER BY recipient, seq", conversation_id)
        return [(row["recipient"], json.loads(row["notice"]), row["text"]) for row in rows
                if json.loads(row["notice"]).get("event") == "topic_moved"]

    def audit(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.host.config.audit_path.read_text().splitlines()]

    def audit_text(self) -> str:
        return self.host.config.audit_path.read_text()


@pytest.fixture
def build_scene(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A factory, called inside the test: a missing module is then a failed assertion in the test body."""

    def build() -> Scene:
        module = grants_module()
        clock = Clock()
        monkeypatch.setattr(module, "_now", clock)
        return Scene(build_host(tmp_path), module, clock)

    return build


async def stage(client: Any, host: Any, paths: tuple[str, ...] = BOARD) -> None:
    """Open the board's topics as the planner, parents first, each with the two members a thread here goes to."""
    for path in paths:
        made = await tool(client, host, "create_topic", {"path": path, "members": MEMBERS}, role="planner")
        assert made.get("created") is True, made


async def thread(client: Any, host: Any, topic: str | None = None, title: str = "A thread") -> str:
    """A thread of the planner and the worker, filed in `topic` when one is given."""
    arguments: dict[str, Any] = {"title": title, "participants": MEMBERS}
    if topic is not None:
        arguments["topic"] = topic
    made = await tool(client, host, "create_conversation", arguments, role="planner")
    assert made.get("created") is True, made
    return made["conversation_id"]
