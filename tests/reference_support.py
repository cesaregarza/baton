"""Shared setup for the reference tests: the same hosts, scenes, grantees and grant clock as the topic grant tests, plus
what references add.

Nothing here imports `baton.topics.references` or touches a reference table at the top level. `references_module()`,
`body_method()` and `require_reference_tables()` assert that the module, the tool and the tables exist first, as S1's
`grants_module()` does, so against a base without them a test fails on that assertion and never as an ImportError, an
AttributeError or an OperationalError.

What is in here:

* the refusal texts of the spec's table (F1-F15) as functions of what they name, written out once;
* `RefScene`, a `Scene` that also reads the two reference tables and counts every table;
* callers for reads (`Core`, `Bound`, `Anonymous`), so a read can be made by anyone the spec lists;
* `Staging`, which stages a race without threads or sleeps (explained at the class);
* `run_together`, which runs several tool calls on their own threads and connections, released by a barrier.

Nothing secret is written here: keys, binding tokens and passphrases are made at run time.
"""

from __future__ import annotations

import importlib
import importlib.util
import queue
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any

import pytest
import topic_grant_tools_support as support
from topic_grant_support import Clock, grants_module, table_exists
from topic_grant_tools_support import Grantee, Scene, build_host, tool

from baton import aliases
from baton import app as app_module
from baton.app import guard, tools
from baton.database import store
from baton.errors import BatonError

REFERENCE_TABLES = ("reference_docs", "reference_revisions")
BOARDS, LEADS, ROOT = "job-hunt/boards", "job-hunt/leads", "job-hunt"
NAME = "job-board"
MAX_BODY = 32768  # the spec's limit, not read from the module under test
MAX_DOCS = 20
# What an edit's response carries, exactly, and the author block's keys for an alias-bound call.
EDIT_KEYS = {
    "topic",
    "topic_id",
    "name",
    "revision",
    "base_revision",
    "size",
    "at",
    "author",
    "grant_id",
    "request_id",
    "created",
}
READ_KEYS = {
    "topic",
    "topic_id",
    "name",
    "revision",
    "current_revision",
    "body",
    "size",
    "base_revision",
    "at",
    "author",
    "grant_id",
}
LIST_KEYS = {"topic", "topic_id", "references"}
LISTED_KEYS = {"name", "current_revision", "at", "size", "author", "grant_id"}
HISTORY_KEYS = {"topic", "topic_id", "name", "current_revision", "revisions", "next_revision"}
HISTORY_ENTRY_KEYS = {"revision", "base_revision", "at", "size", "author", "grant_id"}

NEEDS_BINDING = "a topic grant key works only with the alias's binding token"
BINDING_GONE = "alias binding is missing, invalid or expired"
F2 = "guest may not call edit_reference"
F4 = "name must be 1-40 characters of a-z, 0-9 and inner hyphens, such as job-board"
F6 = "expected_revision is required: 0 to create the reference, otherwise the revision you read"
F7 = "body must be text of 1-32768 bytes of UTF-8"
F12 = "revision needs a name"
F13 = "request_id is 1-200 characters"
F15 = "guest posting is over its hourly limit"


def f1(role: str) -> str:
    return f"{role} may not edit references (roles.{role}.can_create_topics)"


def f5(name: str, path: str, current: int, expected: int) -> str:
    return f"reference {name!r} in {path!r} is at revision {current}, not {expected}; read it again before editing it"


def f7b(size: int) -> str:
    return f"a reference body is at most 32768 bytes of UTF-8; this one is {size}"


def f7c(path: str) -> str:
    return f"topic {path!r} already holds 20 references; edit one of them"


def f8(request_id: str) -> str:
    return f"request_id {request_id} was already used with a different body"


def f9(path: str) -> str:
    return f"no topic {path!r}"


def f10(name: str, path: str) -> str:
    return f"no reference {name!r} in {path!r}"


def f11(name: str, path: str, revision: int, current: int) -> str:
    return f"reference {name!r} in {path!r} has no revision {revision}; its current revision is {current}"


def not_covered(grant_id: str, path: str, capability: str = "reference.edit") -> str:
    """S2's refusal 5, with the capability this change asks for."""
    return f"topic grant {grant_id} does not allow {capability} on {path}"


def revoked(grant_id: str) -> str:
    return f"topic grant {grant_id} is revoked"


# Finding the new module, tool and tables, or failing in words -------------------------------------------------


def references_module() -> ModuleType:
    """The module under test, or a failed assertion saying it is not there."""
    assert importlib.util.find_spec("baton.topics.references") is not None, (
        "baton/topics/references.py does not exist yet"
    )
    module = importlib.import_module("baton.topics.references")
    for name in ("edit", "read", "history", "MAX_BODY_BYTES", "MAX_DOCS_PER_TOPIC", "MAX_NAME", "MAX_HISTORY_PAGE"):
        assert hasattr(module, name), f"baton.topics.references has no {name}"
    return module


def reference_tools_module() -> ModuleType:
    """The module the three tool methods live in, or a failed assertion saying it is not there."""
    assert importlib.util.find_spec("baton.app.reference_tools") is not None, (
        "baton/app/reference_tools.py does not exist yet"
    )
    module = importlib.import_module("baton.app.reference_tools")
    for name in ("ReferenceTools", "REFERENCE_TOOLS", "REFERENCE_GUEST_TOOLS"):
        assert hasattr(module, name), f"baton.app.reference_tools has no {name}"
    return module


def body_method(name: str) -> Callable[..., Any]:
    """A tool's body as a plain function of (tools, role, **arguments), or a failed assertion saying it is missing."""
    assert hasattr(app_module.Tools, name), f"baton.app.Tools has no {name}: the tool is not there yet"
    return getattr(app_module.Tools, name)


def require_reference_tables(db: sqlite3.Connection) -> None:
    for name in REFERENCE_TABLES:
        assert table_exists(db, name), f"there is no {name} table"


def tables_of(db: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# Shapes of what the tools answer ----------------------------------------------------------------------------


def author_of(held: Grantee) -> dict[str, str]:
    """The author block of a call made through this alias, which acts as a guest."""
    return {"role": "guest", "alias_id": held.alias_id, "alias": held.name, "code": held.code}


def body_of(number: int) -> str:
    return f"The board list, as of revision {number}.\n"


def request_id() -> str:
    return "req-" + secrets.token_hex(4)


def collapsed(text: str) -> str:
    """Whitespace folded to single spaces: a description wrapped across docstring lines, in either Python version."""
    return " ".join(text.split())


def edit_arguments(topic: str, name: str, body: str, expected: int, **extra: Any) -> dict[str, Any]:
    return {"topic": topic, "name": name, "body": body, "expected_revision": expected, **extra}


def refused_by_schema(result: dict[str, Any], argument: str) -> None:
    """The SDK refused the arguments before the tool ran: an error that names the argument and says it was validated."""
    text = str(result.get("unparsed", result))
    assert "validation error" in text and argument in text, result


# The scene --------------------------------------------------------------------------------------------------


@dataclass
class RefScene(Scene):
    """A `Scene` that reads the reference tables and every table's row count. Read access asserts the tables exist."""

    def docs(self) -> list[sqlite3.Row]:
        with self.db() as conn:
            require_reference_tables(conn)
            return conn.execute("SELECT * FROM reference_docs ORDER BY rowid").fetchall()

    def revisions(self, path: str | None = None, name: str | None = None) -> list[sqlite3.Row]:
        """Every revision row in storage order, or those of the document `name` in the topic now at `path`."""
        with self.db() as conn:
            require_reference_tables(conn)
            if path is None:
                return conn.execute("SELECT * FROM reference_revisions ORDER BY rowid").fetchall()
            return conn.execute(
                "SELECT r.* FROM reference_revisions r JOIN reference_docs d ON d.id = r.doc_id"
                " JOIN topics t ON t.id = d.topic_id WHERE t.path = ? AND d.name = ? ORDER BY r.revision",
                (path, name),
            ).fetchall()

    def tables(self) -> dict[str, list[tuple[Any, ...]]]:
        """Both reference tables, every row and every column, as plain tuples: for byte-for-byte comparisons."""
        with self.db() as conn:
            require_reference_tables(conn)
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in REFERENCE_TABLES
            }

    def counts(self) -> dict[str, int]:
        """Row counts of every ordinary table (the search indexes' shadow tables left out)."""
        with self.db() as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    " AND name NOT LIKE '%\\_fts%' ESCAPE '\\' ORDER BY name"
                )
            ]
            return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}

    def posts_counted(self) -> int:
        return len(self.query("SELECT 1 FROM alias_posts"))

    def refused_uses(self) -> list[sqlite3.Row]:
        return self.events("refused_use")

    def set_role(self, role: str, **flags: bool) -> None:
        """Change a configured role's flags for the calls that follow: the app reads its config on every call."""
        self.host.config.roles[role] = replace(self.host.config.roles[role], **flags)

    def limit_posts(self, per_alias: int | None = None, per_credential: int | None = None) -> None:
        """Set the guest hourly post limits. Call before `play`: the app is built from the host's config then."""
        limits = {
            key: value
            for key, value in (
                ("max_posts_per_alias_per_hour", per_alias),
                ("max_posts_per_credential_per_hour", per_credential),
            )
            if value is not None
        }
        self.host.config = replace(self.host.config, aliases=replace(self.host.config.aliases, **limits))


@pytest.fixture
def build_scene(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A factory, called inside the test, so a missing module is a failed assertion in the test body."""

    def build() -> RefScene:
        module = grants_module()
        clock = Clock()
        monkeypatch.setattr(module, "_now", clock)
        return RefScene(build_host(tmp_path), module, clock)

    return build


# Callers ----------------------------------------------------------------------------------------------------


@dataclass
class Core:
    """A configured role calling with its own credential."""

    host: Any
    role: str = "planner"

    async def call(self, client: Any, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.host, tool_name, arguments, role=self.role)


@dataclass
class Bound:
    """A guest holding a binding token and no grant: an alias that can read and post, nothing more."""

    host: Any
    name: str
    token: str
    alias_id: str
    code: str

    async def call(self, client: Any, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.host, tool_name, {**arguments, "token": self.token})

    @classmethod
    def of(cls, held: Grantee) -> Bound:
        """A grantee calling without its key, as it does to read."""
        return cls(held.host, held.name, held.token, held.alias_id, held.code)


@dataclass
class Keyed:
    """A grantee calling with both halves of what it carries: its binding token and its grant key.

    `Grantee.call` takes the tool's name as a parameter called `name`, which a reference tool's own `name` argument
    would collide with, so reference tests call through this.
    """

    held: Grantee

    async def call(self, client: Any, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.held.host, tool_name, self.held.arguments(**arguments))


@dataclass
class Anonymous:
    """The shared guest login with no binding token at all."""

    host: Any

    async def call(self, client: Any, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        return await tool(client, self.host, tool_name, arguments)


async def claim_again(client: Any, scene: Scene, alias: str = "job-search") -> dict[str, Any]:
    """Claim an alias with its passphrase, which is not secret: a fresh binding token for whoever knows it."""
    claimed = await tool(client, scene.host, "claim_alias", {"name": alias, "passphrase": support.PASSPHRASE})
    assert claimed.get("binding_token"), claimed
    return claimed


async def bound_guest(client: Any, scene: Scene, alias: str = "lead-scout") -> Bound:
    claimed = await claim_again(client, scene, alias)
    return Bound(scene.host, alias, claimed["binding_token"], claimed["alias_id"], claimed["code"])


def bring_to_state(scene: Scene, held: Grantee, state: str) -> None:
    """Bring a grant to the named state the way the topic grant tests do: through the module and the clock, or by
    editing the stored row behind the module's back, which is what an invalid, pending or lapsed grant is."""
    if state == "revoked":
        scene.revoke(held.grant_id)
    elif state == "expired":
        scene.clock.advance(days=7)  # a grant lasts until, not through, its last instant
    elif state == "invalid":
        scene.edit_grant(held.grant_id, proposed_by="edited after authorization")  # a field the digest covers
    else:
        assert state in ("pending", "lapsed"), state
        scene.edit_grant(held.grant_id, authorized_digest=None)
        if state == "lapsed":
            scene.clock.advance(hours=25)


async def board(
    client: Any,
    scene: Scene,
    capabilities: Any = support.ALL,
    inherit: bool = True,
    topic: str = ROOT,
    alias: str = "job-search",
) -> Grantee:
    """The staged board, and a grantee holding one grant on `topic`."""
    await support.stage(client, scene.host)
    return await scene.grantee(client, alias, topic, capabilities, inherit)


async def core_edit(
    client: Any, scene: Scene, topic: str, name: str, body: str, expected: int, role: str = "planner", **extra: Any
) -> dict[str, Any]:
    return await Core(scene.host, role).call(
        client, "edit_reference", **edit_arguments(topic, name, body, expected, **extra)
    )


async def grantee_edit(
    client: Any, held: Grantee, topic: str, name: str, body: str, expected: int, **extra: Any
) -> dict[str, Any]:
    return await Keyed(held).call(client, "edit_reference", **edit_arguments(topic, name, body, expected, **extra))


async def write_revisions(
    client: Any, scene: Scene, topic: str, name: str, count: int, role: str = "planner"
) -> list[dict[str, Any]]:
    """Revisions 1..count of a document written by a core role, each with its own text; every one must succeed."""
    written = []
    for number in range(1, count + 1):
        written.append(
            support.succeeded(await core_edit(client, scene, topic, name, body_of(number), number - 1, role=role))
        )
    return written


async def rename(
    client: Any, scene: Scene, path: str, new_path: str, acknowledge: list[str] | None = None
) -> dict[str, Any]:
    """A core role renames a topic, naming the active grants the rename changes the reach of, when there are any."""
    arguments: dict[str, Any] = {"path": path, "new_path": new_path}
    if acknowledge is not None:
        arguments["acknowledge_topic_grants"] = acknowledge
    return await tool(client, scene.host, "rename_topic", arguments, role="planner")


async def open_topic(client: Any, scene: Scene, path: str) -> dict[str, Any]:
    """A core role opens a topic, with the members the staged board's topics have."""
    made = await tool(client, scene.host, "create_topic", {"path": path, "members": support.MEMBERS}, role="planner")
    assert made.get("created") is True, made
    return made


async def read_reference(
    client: Any, who: Any, topic: str, name: str | None = None, revision: int | None = None
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"topic": topic}
    if name is not None:
        arguments["name"] = name
    if revision is not None:
        arguments["revision"] = revision
    return await who.call(client, "read_reference", **arguments)


async def reference_history(client: Any, who: Any, topic: str, name: str, **paging: int) -> dict[str, Any]:
    return await who.call(client, "reference_history", topic=topic, name=name, **paging)


def direct(scene: Scene, tool_name: str, role: str, /, **arguments: Any) -> Any:
    """A tool body called as the guard calls it, minus the guard: `Tools(config).<tool_name>(role, **arguments)`.

    This is how a test reaches what the schema would keep out (a missing or boolean `expected_revision`) and how
    threads each get a connection of their own. A refusal is raised as a `BatonError`.
    """
    return body_method(tool_name)(app_module.Tools(scene.host.config), role, **arguments)


def as_the_guard_calls(scene: Scene, held: Grantee) -> dict[str, Any]:
    """The `identity` and `call` the guard hands a body for a grantee: its alias's binding and its key."""
    conn = store.connect(scene.host.config.db_path)
    try:
        identity = aliases.resolve_binding(conn, held.token)
    finally:
        conn.close()
    call = {
        "credential_role": "guest",
        "credential_id": "guest-token",
        "authority": {},
        "role": "guest",
        "identity": identity,
        "grant_key": held.key,
    }
    return {"identity": identity, "call": call}


# Staging a race, with no thread and no sleep -----------------------------------------------------------------

WRITE_WORDS = ("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE")
WATCHED = ("REFERENCE_REVISIONS", "REFERENCE_DOCS", "TOPIC_GRANTS")


class Staging:
    """Traces every statement a tool body's connection runs, and can run one action at a chosen moment.

    How a race is staged. Each tool body gets its connection from `Tools._conn()`, which this swaps for one that
    carries a trace callback and a 2 s busy timeout. SQLite calls the callback just before it runs a statement.
    When the moment comes, the callback runs `action` on a second, plain connection with a 50 ms busy timeout, then
    returns, and the statement goes ahead. The moments:

    * `first_write`: before the first `BEGIN`, `INSERT`, `UPDATE`, `DELETE` or `REPLACE` the body runs. A body that
      takes its write lock first runs `BEGIN IMMEDIATE` here, before it has read anything, so whatever the action
      commits is there for its reads to see. A body that read something before it took the lock read it before
      the action committed, and then writes on what it read.
    * `write_after_read`: before the first such statement that follows a read of a reference table or of
      `topic_grants`. A body that holds the write lock from its first statement still holds it here, so the second
      connection's write waits its 50 ms and fails with "database is locked". A body that does not hold it lets the
      action commit between its read and its write.

    Nothing waits on a thread or on the tool: the second connection gives up after its timeout, whatever the tool
    does, and the action runs at most once. The statements are kept, each with whether a transaction was open when
    it was about to run, so a test can also say where a body's reads and writes happened.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, everything: bool = False) -> None:
        self.statements: list[tuple[str, bool]] = []
        self.moment: str | None = None
        self.action: Callable[[], Any] | None = None
        self.fired = False
        self.outcome: Any = None
        self._read_watched = False
        staging = self
        if everything:  # every connection the app opens, the guard's included
            real_connect = app_module.connect

            def traced_connect(path: Any) -> sqlite3.Connection:
                conn = real_connect(path)
                conn.set_trace_callback(lambda sql: staging._trace(sql, conn))
                return conn

            for module in (app_module, guard, tools):
                monkeypatch.setattr(module, "connect", traced_connect)
            return

        @contextmanager
        def traced_conn(tools: Any) -> Iterator[sqlite3.Connection]:
            conn = store.connect(tools._config.db_path)
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.set_trace_callback(lambda sql: staging._trace(sql, conn))
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)
                conn.close()

        monkeypatch.setattr(app_module.Tools, "_conn", traced_conn)

    def at(self, moment: str, action: Callable[[], Any]) -> Staging:
        assert moment in ("first_write", "write_after_read"), moment
        self.moment, self.action, self.fired, self.outcome, self._read_watched = moment, action, False, None, False
        return self

    def _trace(self, sql: str, conn: sqlite3.Connection) -> None:
        text = sql.lstrip().upper()
        self.statements.append((sql, conn.in_transaction))
        if self.moment is None or self.fired:
            return
        if text.startswith(("SELECT", "WITH")) and any(word in text for word in WATCHED):
            self._read_watched = True
        if text.startswith(WRITE_WORDS) and (self.moment == "first_write" or self._read_watched):
            self.fired = True
            assert self.action is not None
            self.outcome = self.action()

    def forget(self) -> Staging:
        """Start the record again, for a test that looks at one call after another."""
        self.statements.clear()
        return self

    def texts(self) -> list[str]:
        return [sql.lstrip() for sql, _ in self.statements]

    def outside_a_transaction(self) -> list[str]:
        """Every statement but a `BEGIN` that ran with no transaction open."""
        return [
            sql.lstrip() for sql, open_ in self.statements if not open_ and not sql.lstrip().upper().startswith("BEGIN")
        ]

    def writes_to_reference_tables_other_than_inserts(self) -> list[str]:
        found = []
        for sql, _ in self.statements:
            text = " ".join(sql.upper().split())
            if any(table.upper() in text for table in REFERENCE_TABLES) and (
                text.startswith(("UPDATE", "DELETE", "REPLACE")) or "OR REPLACE" in text
            ):
                found.append(sql)
        return found


def second_connection(scene: Scene, timeout: float = 0.05) -> sqlite3.Connection:
    """A plain connection to the same file with a short busy timeout, so a lock fails fast instead of hanging."""
    conn = sqlite3.connect(scene.host.config.db_path, timeout=timeout, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def other_writer_adds_revision(scene: Scene, path: str, name: str, revision: int) -> Callable[[], str]:
    """An action: another writer commits `revision` of the document directly, creating the document when it is new.

    "landed" when it committed, "locked" when the write lock was held, else what went wrong.
    """

    def act() -> str:
        conn = second_connection(scene)
        try:
            require_reference_tables(conn)
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                return "locked" if "locked" in str(error) else f"error: {error}"
            try:
                topic_id = conn.execute("SELECT id FROM topics WHERE path = ?", (path,)).fetchone()["id"]
                doc = conn.execute(
                    "SELECT id FROM reference_docs WHERE topic_id = ? AND name = ?", (topic_id, name)
                ).fetchone()
                if doc is None:
                    doc_id = "ref_" + secrets.token_hex(8)
                    conn.execute(
                        "INSERT INTO reference_docs (id, topic_id, name, created_at) VALUES (?, ?, ?, ?)",
                        (doc_id, topic_id, name, "2026-11-03T12:00:00+00:00"),
                    )
                else:
                    doc_id = doc["id"]
                conn.execute(
                    "INSERT INTO reference_revisions (doc_id, revision, body, author_role, author_alias_id,"
                    " author_alias_name, author_alias_code, grant_id, request_id, request_digest, base_revision, at)"
                    " VALUES (?, ?, ?, 'planner', NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (
                        doc_id,
                        revision,
                        "text from the writer who got there first",
                        request_id(),
                        secrets.token_hex(32),
                        revision - 1,
                        "2026-11-03T12:00:00+00:00",
                    ),
                )
                conn.execute("COMMIT")
                return "landed"
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    return act


def owner_revokes(scene: Scene, grant_id: str) -> Callable[[], str]:
    """An action: the owner revokes the grant through the grant module, on a connection with a 50 ms busy timeout."""

    def act() -> str:
        conn = second_connection(scene)
        try:
            scene.tg.revoke(conn, grant_id, "revoked while an edit was starting")
            return "landed"
        except sqlite3.OperationalError as error:
            return "locked" if "locked" in str(error) else f"error: {error}"
        finally:
            conn.close()

    return act


# Threads, each with a connection of its own -----------------------------------------------------------------


def run_together(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, jobs: list[Callable[[Any], Any]]
) -> list[tuple[str, Any]]:
    """Run every job on its own thread with its own connection, released together by a barrier. No sleeps.

    A job is called with a `Tools`. Each gets a connection opened beforehand, with a 2 s busy timeout, so a lock
    that is not released fails the test in seconds instead of hanging it. Each result is ("ok", the answer),
    ("refused", the BatonError) or ("escaped", whatever else was raised): a refusal is a clean answer, and an
    exception that escapes the tool is what a race must never produce.
    """
    pool: queue.Queue[sqlite3.Connection] = queue.Queue()
    for _ in jobs:
        conn = store.connect(scene.host.config.db_path)
        conn.execute("PRAGMA busy_timeout = 2000")
        pool.put(conn)

    @contextmanager
    def pooled(tools: Any) -> Iterator[sqlite3.Connection]:
        conn = pool.get_nowait()
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(app_module.Tools, "_conn", pooled)
    barrier = threading.Barrier(len(jobs))
    results: list[tuple[str, Any]] = [("escaped", "never ran")] * len(jobs)

    def run(index: int, job: Callable[[Any], Any]) -> None:
        tools = app_module.Tools(scene.host.config)
        try:
            barrier.wait(timeout=10)
            results[index] = ("ok", job(tools))
        except BatonError as error:
            results[index] = ("refused", error)
        except BaseException as error:
            results[index] = ("escaped", repr(error))

    threads = [threading.Thread(target=run, args=(index, job), daemon=True) for index, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not [thread for thread in threads if thread.is_alive()], "a call is stuck"
    return results


def dump(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
