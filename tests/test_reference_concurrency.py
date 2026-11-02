"""Races on one document and a grant revoked while an edit starts: the compare-and-swap and the grant check are
decided under the database's write lock, and nothing is read before that lock is held.

How the races are staged. Two kinds of test, and neither sleeps.

* Staged without a thread. `reference_support.Staging` swaps the connection each tool body gets for one with a trace
  callback and a 2 s busy timeout. SQLite calls the callback just before it runs a statement, and at a chosen moment
  the callback runs one action on a second, plain connection with a 50 ms busy timeout: another writer inserts the next
  revision, or the owner revokes the grant. The moments are "before the body's first write-like statement" and "before
  the first write-like statement that follows a read of a reference table or of the grants". A body that takes its
  write lock first (BEGIN IMMEDIATE) and reads after it has the first action committed before it looks, and the second
  one waits its 50 ms and fails with "database is locked". A body that read before it took the lock has read what the
  first action then changes, and writes on it; and one that holds no lock lets the second action commit between its
  read and its write. Both end in an answer that is not a clean refusal (a key collision, or a write under a revoked
  grant), which the tests reject. The second connection gives up after its timeout whatever the tool does, so a
  mistake fails in a fraction of a second and cannot hang.
* Eight threads, each with a connection of its own opened beforehand with a 2 s busy timeout, released together by a
  barrier, calling the tool body directly. Exactly one wins; the others get a clean `stale_revision`. This is the
  end-to-end check, and it can only show a race that happens to occur, so the staged tests are what kill the mutants.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill.
"""

from __future__ import annotations

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    NAME,
    ROOT,
    Staging,
    as_the_guard_calls,
    board,
    body_method,
    body_of,
    core_edit,
    f5,
    grantee_edit,
    other_writer_adds_revision,
    owner_revokes,
    request_id,
    revoked,
    run_together,
    write_revisions,
)
from topic_grant_tools_support import only, play, refused, succeeded

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

OTHER_WRITER = "text from the writer who got there first"


def core_job(expected, number, token):
    return lambda tools: tools.edit_reference("planner", topic=BOARDS, name=NAME, body=f"writer {number}",
                                              expected_revision=expected, request_id=f"race-{token}-{number}")


def prepared(scene, revisions):
    """The board, and `revisions` revisions of the document, written before the race starts."""

    async def setup(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, revisions)

    play(scene.host, setup)


def is_stale(kind, error, current):
    """Whether a loser of a race was told what it must be: stale, with the number the winner made."""
    return kind == "refused" and (error.code, error.extra) == ("stale_revision", {"current_revision": current})


# 1. Writers racing on threads ---------------------------------------------------------------------------------

@pytest.mark.parametrize("expected", [0, 3])
def test_eight_writers_who_read_the_same_revision_make_exactly_one_new_one(build_scene, monkeypatch, expected):
    """R11. Eight threads, all with expected_revision N on the same document (once creating it, once on its third
    revision). Exactly one returns revision N+1; the other seven are refused `stale_revision` with `current_revision`
    N+1; exactly one new row exists; nothing else escapes: no key collision and no "database is locked"."""
    # Mutant 3 or 4: the read of the current revision before the lock, or no write lock at all.
    scene = build_scene()
    body_method("edit_reference")
    prepared(scene, expected)
    token = request_id()
    results = run_together(scene, monkeypatch, [core_job(expected, number, token) for number in range(8)])
    assert [error for kind, error in results if kind == "escaped"] == []
    wins = [answer for kind, answer in results if kind == "ok"]
    assert len(wins) == 1 and (wins[0]["revision"], wins[0]["created"]) == (expected + 1, True), results
    losers = [(kind, error) for kind, error in results if kind != "ok"]
    assert len(losers) == 7 and all(is_stale(kind, error, expected + 1) for kind, error in losers), results
    rows = scene.revisions(BOARDS, NAME)
    assert [row["revision"] for row in rows] == list(range(1, expected + 2))
    assert rows[-1]["request_id"] == wins[0]["request_id"] and rows[-1]["body"].startswith("writer ")
    assert len(scene.docs()) == 1


def test_eight_grantees_of_two_aliases_race_for_one_revision_and_one_wins(build_scene, monkeypatch):
    """R11. Four threads for each of two aliases, each with its own grant and key, all with expected_revision 3: the
    same result. The winner's row names the alias and the grant of whichever won."""
    # Mutant 3 or 4.
    scene = build_scene()
    body_method("edit_reference")
    state = {}

    async def setup(client):
        state["first"] = await board(client, scene)
        state["second"] = await scene.grantee(client, "lead-scout", ROOT, ("reference.edit",), inherit=True)
        await write_revisions(client, scene, BOARDS, NAME, 3)

    play(scene.host, setup)
    token = request_id()

    def grantee_job(held, number):
        inputs = as_the_guard_calls(scene, held)
        return lambda tools: tools.edit_reference("guest", topic=BOARDS, name=NAME, body=f"writer {number}",
                                                  expected_revision=3, request_id=f"race-{token}-{number}", **inputs)

    jobs = [grantee_job(state["first" if number % 2 == 0 else "second"], number) for number in range(8)]
    results = run_together(scene, monkeypatch, jobs)
    assert [error for kind, error in results if kind == "escaped"] == []
    wins = [answer for kind, answer in results if kind == "ok"]
    assert len(wins) == 1 and wins[0]["revision"] == 4, results
    assert all(is_stale(kind, error, 4) for kind, error in results if kind != "ok"), results
    row = scene.revisions(BOARDS, NAME)[-1]
    assert (row["grant_id"], row["author_alias_id"]) == (wins[0]["grant_id"], wins[0]["author"]["alias_id"])
    assert row["grant_id"] in (state["first"].grant_id, state["second"].grant_id)
    assert len(scene.revisions(BOARDS, NAME)) == 4


def test_eight_simultaneous_retries_of_one_request_write_one_revision_and_answer_alike(build_scene, monkeypatch):
    """R12. The same request, the same arguments, from eight threads at once: one revision is written, one answer is
    `created` and seven are replays of it, and none is refused as stale, though the revision they all expected is
    behind the current one by the time the later ones run."""
    # Mutant 7: the replay lookup below the compare-and-swap. Mutant 3 or 4.
    scene = build_scene()
    body_method("edit_reference")
    prepared(scene, 2)
    rid = request_id()
    jobs = [lambda tools: tools.edit_reference("planner", topic=BOARDS, name=NAME, body="one request",
                                               expected_revision=2, request_id=rid) for _ in range(8)]
    results = run_together(scene, monkeypatch, jobs)
    assert [kind for kind, _ in results] == ["ok"] * 8, results
    answers = [answer for _, answer in results]
    assert sorted(answer["created"] for answer in answers) == [False] * 7 + [True]
    assert {answer["revision"] for answer in answers} == {3}
    assert len({tuple(sorted((key, str(value)) for key, value in answer.items() if key != "created"))
                for answer in answers}) == 1
    assert [row["revision"] for row in scene.revisions(BOARDS, NAME)] == [1, 2, 3]


# 2. A writer that gets in before the edit's transaction -------------------------------------------------------

@pytest.mark.parametrize("expected", [0, 2])
def test_a_writer_that_commits_before_the_edits_transaction_is_seen_by_the_edit(build_scene, monkeypatch, expected):
    """R10, R11. Another writer inserts revision N+1 on a second connection right before the edit runs its first write
    statement. The edit read nothing before its transaction, so it sees N+1 and is refused as stale with it, and
    writes nothing; it neither collides on the key nor overwrites. Once for a new document, once for an existing one."""
    # Mutant 3: the read of the current revision above transaction(conn). Mutant 4: no write lock.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, expected)
        tables = scene.tables()
        staging = Staging(monkeypatch).at("first_write", other_writer_adds_revision(scene, BOARDS, NAME, expected + 1))
        result = await core_edit(client, scene, BOARDS, NAME, "text from the slow writer", expected)
        assert staging.fired, "the edit ran no write statement"
        assert staging.outcome == "landed", staging.outcome
        assert result == {"error": "stale_revision", "detail": f5(NAME, BOARDS, expected + 1, expected),
                          "current_revision": expected + 1}, result
        rows = scene.revisions(BOARDS, NAME)
        assert [row["revision"] for row in rows] == list(range(1, expected + 2))
        assert rows[-1]["body"] == OTHER_WRITER
        assert scene.tables()["reference_revisions"][:expected] == tables["reference_revisions"]
        assert len(scene.docs()) == 1

    play(scene.host, scenario)


@pytest.mark.parametrize("expected", [0, 2])
def test_a_writer_cannot_commit_between_the_edits_read_and_its_write(build_scene, monkeypatch, expected):
    """R10. Another writer tries to insert revision N+1 right before the first write statement that follows the edit's
    read of the document. The edit holds the write lock by then, so the attempt waits its 50 ms and fails with
    "database is locked", and the edit writes revision N+1 itself."""
    # Mutant 3 or 4: a read outside the lock lets the other writer commit between the read and the write.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, expected)
        staging = Staging(monkeypatch).at("write_after_read",
                                          other_writer_adds_revision(scene, BOARDS, NAME, expected + 1))
        made = succeeded(await core_edit(client, scene, BOARDS, NAME, "text from the edit", expected))
        assert staging.fired, "the edit never wrote after reading the document"
        assert staging.outcome == "locked", f"another writer got in while the edit held its lock: {staging.outcome}"
        assert (made["revision"], made["base_revision"]) == (expected + 1, expected)
        rows = scene.revisions(BOARDS, NAME)
        assert [row["revision"] for row in rows] == list(range(1, expected + 2))
        assert rows[-1]["body"] == "text from the edit"

    play(scene.host, scenario)


def test_every_statement_of_an_edit_runs_inside_its_one_write_transaction(build_scene, monkeypatch):
    """R10. For a success by a core role and by a grantee, a stale refusal, a replay and a refusal by the grant: the
    first statement the tool body runs is `BEGIN IMMEDIATE`, no statement runs outside a transaction, and a success
    has exactly one transaction, committed. Nothing is read, from the grants or from the references, before the lock."""
    # Mutants 3, 4 and 5: a read above the transaction, no lock, the grant check above the transaction.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        staging = Staging(monkeypatch)
        rid = request_id()
        calls = [
            ("core success", lambda: core_edit(client, scene, BOARDS, NAME, body_of(1), 0)),
            ("grantee success", lambda: grantee_edit(client, held, BOARDS, NAME, body_of(2), 1, request_id=rid)),
            ("stale", lambda: core_edit(client, scene, BOARDS, NAME, body_of(9), 1)),
            ("replay", lambda: grantee_edit(client, held, BOARDS, NAME, body_of(2), 1, request_id=rid)),
            ("not covered", lambda: grantee_edit(client, held, "resume", NAME, body_of(1), 0)),
        ]
        for label, call in calls:
            staging.forget()
            await call()
            texts = staging.texts()
            assert texts and texts[0].upper().startswith("BEGIN IMMEDIATE"), (label, texts)
            assert staging.outside_a_transaction() == [], (label, staging.outside_a_transaction())
            if label.endswith("success"):
                assert [text.split()[0].upper() for text in texts if text.upper().startswith(("BEGIN", "COMMIT"))] == [
                    "BEGIN", "COMMIT"], (label, texts)

    play(scene.host, scenario)


# 3. A grant revoked while an edit starts ----------------------------------------------------------------------

def test_a_revoke_made_after_a_read_refuses_the_next_edit(build_scene):
    """R17, case 17 (a). The grantee reads revision 5; the owner revokes G1; its edit with expected 5 is refused in S2's
    words, as a refused use, and no row is written."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 5)
        read = succeeded(await refs.read_reference(client, refs.Bound.of(held), BOARDS, NAME))
        scene.revoke(held.grant_id)
        tables = scene.tables()
        refused(await grantee_edit(client, held, BOARDS, NAME, "text", read["current_revision"]), "forbidden",
                revoked(held.grant_id))
        assert scene.tables() == tables

    play(scene.host, scenario)


def test_a_revoke_that_commits_just_before_the_edits_transaction_refuses_the_edit(build_scene, monkeypatch):
    """R10, R17, case 17 (b). The owner revokes G1 at the instant the edit is about to begin its transaction: after any
    check made before it, before any made inside it. The edit must be refused as revoked and write nothing, and the
    revoke stands. A grant check made before the transaction would pass on the active grant and then write."""
    # Mutant 5: the grant check moved above transaction(conn).
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        tables = scene.tables()
        staging = Staging(monkeypatch).at("first_write", owner_revokes(scene, held.grant_id))
        result = await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1)
        assert staging.fired and staging.outcome == "landed", (staging.fired, staging.outcome)
        refused(result, "forbidden", revoked(held.grant_id))
        assert scene.tables() == tables, "a revision was written under a revoked grant"
        assert scene.state_of(held.grant_id) == "revoked"
        assert only(scene.events("revoke"), "revoke event")["grant_id"] == held.grant_id

    play(scene.host, scenario)


def test_an_edit_that_holds_its_lock_keeps_the_revoke_out_until_it_has_committed_and_its_row_keeps_the_grant(
        build_scene, monkeypatch):
    """R17. The owner tries to revoke right before the edit's first write that follows its checks. The edit holds the
    write lock, so the revoke waits its 50 ms and fails with "database is locked"; the edit commits and stands, its row
    keeps the grant's id; and the revoke then succeeds, after which the next edit is refused."""
    # Mutant 5: the grant check above the lock lets the revoke land between the check and the write.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        staging = Staging(monkeypatch).at("write_after_read", owner_revokes(scene, held.grant_id))
        made = succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0))
        assert staging.fired, "the edit never wrote after reading the grant"
        assert staging.outcome == "locked", f"the revoke landed while the edit held its lock: {staging.outcome}"
        assert scene.state_of(held.grant_id) == "active"
        scene.revoke(held.grant_id)
        row = only(scene.revisions(BOARDS, NAME), "revision")
        assert (row["revision"], row["grant_id"], made["grant_id"]) == (1, held.grant_id, held.grant_id)
        refused(await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1), "forbidden", revoked(held.grant_id))
        assert len(scene.revisions(BOARDS, NAME)) == 1

    play(scene.host, scenario)
