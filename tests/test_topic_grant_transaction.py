"""The grant check runs inside the operation's write transaction, so a revoke cannot land between the check and
the write.

How the race is staged without threads, sleeps or a chance of hanging. The tool's own database connection is given
a trace callback. The callback watches the statements it runs, and the moment the first write-like statement
(`BEGIN`, `INSERT`, `UPDATE` or `DELETE`) follows a statement that read `topic_grants`, it attempts a revoke on a second
connection with a 50 ms busy timeout, then returns. SQLite calls the callback just before it runs that statement,
which is "right after the check passed, before the write":

* Check inside the write transaction: `BEGIN IMMEDIATE` came first and holds the write lock, so the second
  connection's `UPDATE` waits its 50 ms and fails with "database is locked". The operation then succeeds.
* Check outside it, whether before the transaction or in a transaction of its own: the next write-like statement is
  the `BEGIN` of the operation, no lock is held, the revoke lands, and an operation that goes on to write has
  written under a revoked grant. The test fails.

Nothing here waits on a thread or on the tool: the second connection gives up after its timeout, whatever the
tool does, and the callback runs once. The test does not name any function of the grant module, so it holds
whatever the check is called and wherever it lives. If the check never reads `topic_grants` before a write, the test
says so and fails.
"""

from __future__ import annotations

import sqlite3

import topic_grant_tools_support as support
from topic_grant_tools_support import play, refused, stage, succeeded, thread

from baton import app as app_module
from baton import store
from baton.app import guard, tools

build_scene = support.build_scene  # the fixture, under the name the tests ask for

WRITES = ("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE")


class Interleaver:
    """Attempts one revoke of `grant_id` on a second connection, at the moment described in the file docstring."""

    def __init__(self, path, grant_id: str, at: str) -> None:
        self.path, self.grant_id, self.at = path, grant_id, at
        self.armed = False
        self.read_grants = False
        self.outcome: str | None = None

    def trace(self, statement: str) -> None:
        text = statement.lstrip().upper()
        if not self.armed or self.outcome is not None:
            return
        if text.startswith(("SELECT", "WITH")) and "TOPIC_GRANTS" in text:
            self.read_grants = True
        elif self.read_grants and text.startswith(WRITES):
            self.outcome = self.revoke_from_another_connection()

    def revoke_from_another_connection(self) -> str:
        """"landed" if the revoke committed, "locked" if the write lock refused it, else what went wrong."""
        other = sqlite3.connect(self.path, timeout=0.05, isolation_level=None)
        try:
            other.execute("UPDATE topic_grants SET revoked_at = ?, revoked_by = 'owner', revoke_reason = 'raced'"
                          " WHERE id = ?", (self.at, self.grant_id))
            return "landed"
        except sqlite3.OperationalError as error:
            return "locked" if "locked" in str(error) else f"error: {error}"
        finally:
            other.close()


def watch(scene, monkeypatch, grant_id: str) -> Interleaver:
    """Give every connection the tools open the interleaver's trace callback."""
    interleaver = Interleaver(scene.host.config.db_path, grant_id, scene.clock().isoformat())
    real_connect = store.connect

    def traced(path):
        conn = real_connect(path)
        conn.set_trace_callback(interleaver.trace)
        return conn

    for module in (app_module, guard, tools):
        monkeypatch.setattr(module, "connect", traced)
    return interleaver


def test_the_race_staging_can_revoke_when_no_lock_is_held(build_scene):
    """GUARD. Control for the tests below: with nothing holding the write lock the second connection's revoke
    does land, so "locked" in those tests really means the operation held the lock."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt")
        interleaver = Interleaver(scene.host.config.db_path, held.grant_id, scene.clock().isoformat())
        assert interleaver.revoke_from_another_connection() == "landed"
        assert scene.state_of(held.grant_id) == "revoked"

    play(scene.host, scenario)


def test_a_revoke_cannot_land_between_the_check_and_create_topics_write(build_scene, monkeypatch):
    """Right after the check passes, a revoke is attempted on a second connection. It must wait on the lock and
    fail, and the create then succeeds; or the create must be refused. A topic created under a revoked grant is
    the failure."""
    # Mutant 4: the grant check outside the write transaction.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",))
        interleaver = watch(scene, monkeypatch, held.grant_id)
        interleaver.armed = True
        result = await held.call(client, "create_topic", path="job-hunt/raced")
        interleaver.armed = False
        assert interleaver.outcome is not None, \
            "the call never read topic_grants before its first write, so no grant check ran in its transaction"
        if interleaver.outcome == "locked":
            succeeded(result)
            assert "job-hunt/raced" in scene.topic_paths()
            assert scene.state_of(held.grant_id) == "active"
        else:
            assert interleaver.outcome == "landed", interleaver.outcome
            refused(result, "forbidden")
            assert "job-hunt/raced" not in scene.topic_paths(), "the topic was written under a revoked grant"

    play(scene.host, scenario)


def test_a_revoke_cannot_land_between_the_check_and_set_topics_write(build_scene, monkeypatch):
    """The same race for a thread move: the move either holds the revoke off, or is refused and moves nothing."""
    # Mutant 4: the grant check outside the write transaction.
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("thread.organise",), inherit=True)
        moving = await thread(client, scene.host, "job-hunt/leads")
        interleaver = watch(scene, monkeypatch, held.grant_id)
        interleaver.armed = True
        result = await held.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards")
        interleaver.armed = False
        assert interleaver.outcome is not None, \
            "the call never read topic_grants before its first write, so no grant check ran in its transaction"
        if interleaver.outcome == "locked":
            succeeded(result)
            assert scene.thread_topic(moving) == "job-hunt/boards"
            assert scene.state_of(held.grant_id) == "active"
        else:
            assert interleaver.outcome == "landed", interleaver.outcome
            refused(result, "forbidden")
            assert scene.thread_topic(moving) == "job-hunt/leads", "the thread moved under a revoked grant"

    play(scene.host, scenario)


def test_a_write_that_committed_before_the_revoke_stands(build_scene):
    """A call whose write committed first stands: the revoke ends the grant for the next call only, and the
    topic it made, and its history row naming the grant, stay."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", ("topic.create_sub",))
        succeeded(await held.call(client, "create_topic", path="job-hunt/before"))
        scene.revoke(held.grant_id)
        refused(await held.call(client, "create_topic", path="job-hunt/after"), "forbidden",
                f"topic grant {held.grant_id} is revoked")
        assert "job-hunt/before" in scene.topic_paths() and "job-hunt/after" not in scene.topic_paths()
        rows = [row for row in scene.changes("create_topic") if row["grant_id"] == held.grant_id]
        assert len(rows) == 1, [tuple(row) for row in scene.changes()]

    play(scene.host, scenario)
