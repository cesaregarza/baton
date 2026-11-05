"""The audit line and everything else a call to the reference tools leaves behind, and what must never be in any of it.

What this pins:

* The per-call audit line of the three tools: the topic the call named (only if it is a well-formed path), the tool, the
  outcome, the role, the credential and, for an alias, its id and code; `request_id` and `expected_revision` as for any
  tool; on success also the topic's id, the reference's name (only if it is well-formed) and, for an edit, the revision
  written or replayed, and for a grantee's edit the grant. A refusal has the topic and name the call gave and no id.
* A body, a grant key and a binding token are in no audit line, event, refusal text, response or table (but the
  revision's own body column), including when a caller puts a key or a token in the topic, the name or the body of a
  call that is refused.
* A successful edit writes the revision and nothing more: no message, event, notice, topic change, grant event or wake.
* Only the grant refusals that name a grant (S2's 3, 4 and 5) write a `refused_use` event; no other refusal does.

Mutant numbers in comments refer to the spec's list of one-line mutants the tests must kill. Q numbers refer to its open
questions: the test pins the question's default, so a ruling changes only the tests that carry that number.
"""

from __future__ import annotations

import json
import logging
import secrets

import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BOARDS,
    LEADS,
    NAME,
    Anonymous,
    Bound,
    Core,
    Keyed,
    board,
    body_of,
    bound_guest,
    core_edit,
    edit_arguments,
    grantee_edit,
    read_reference,
    reference_history,
    request_id,
    write_revisions,
)
from topic_grant_support import every_cell
from topic_grant_tools_support import play, succeeded, tool

build_scene = refs.build_scene  # the fixture, under the name the tests ask for


def without_time(line):
    return {key: value for key, value in line.items() if key != "at"}


def core_line(name, **extra):
    return {
        "role": "planner",
        "credential_role": "planner",
        "credential_id": "planner-token",
        "tool": name,
        "outcome": "ok",
        **extra,
    }


def guest_line(held, name, **extra):
    return {
        "role": "guest",
        "credential_role": "guest",
        "credential_id": "guest-token",
        "tool": name,
        "alias_id": held.alias_id,
        "alias_code": held.code,
        "outcome": "ok",
        **extra,
    }


# 1. What the line holds ----------------------------------------------------------------------------------------


def test_a_core_edit_audits_its_topic_its_id_the_reference_the_revision_and_the_request(build_scene):
    """R21. The line of a core success has the path the call named, the topic's id, the document's name, the revision
    written, the request id and the expected revision, `outcome` ok, and no grant."""
    # Mutant 20: reference written without the name check, or the body written.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert without_time(scene.audit()[-1]) == core_line(
            "edit_reference",
            topic=BOARDS,
            topic_id=scene.topic_id(BOARDS),
            reference=NAME,
            revision=1,
            request_id=rid,
            expected_revision=0,
        )

    play(scene.host, scenario)


def test_a_grantees_edit_audits_the_same_with_the_alias_and_the_grant(build_scene):
    """R21. A grantee's success has the same fields, the alias's id and code, and the grant it acted under."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert without_time(scene.audit()[-1]) == guest_line(
            held,
            "edit_reference",
            topic=BOARDS,
            topic_id=scene.topic_id(BOARDS),
            reference=NAME,
            revision=1,
            request_id=rid,
            expected_revision=0,
            grant_id=held.grant_id,
        )

    play(scene.host, scenario)


def test_a_replay_audits_the_revision_it_returned_not_the_current_one(build_scene):
    """R21. The retry's line says revision 1 though the document is at 3, because that is the revision it answered."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        first = without_time(scene.audit()[-1])
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(2), 1))
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(3), 2))
        replay = succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        assert replay["created"] is False
        assert without_time(scene.audit()[-1]) == first and first["revision"] == 1

    play(scene.host, scenario)


def test_a_stale_refusal_audits_the_topic_and_the_reference_and_no_id_revision_or_grant(build_scene):
    """R21. A refusal has only the topic and the name the call gave, with the request and the expected revision as
    usual: no `topic_id`, no `revision`, no `grant_id`, though the grantee's alias fields are there."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        rid = request_id()
        stale = await grantee_edit(client, held, BOARDS, NAME, "text", 1, request_id=rid)
        assert stale.get("error") == "stale_revision", stale
        line = without_time(scene.audit()[-1])
        assert line == guest_line(
            held, "edit_reference", topic=BOARDS, reference=NAME, request_id=rid, expected_revision=1
        ) | {"outcome": "stale_revision"}
        assert not {"topic_id", "revision", "grant_id"} & set(line)

    play(scene.host, scenario)


def test_a_refusal_for_a_grant_that_does_not_cover_the_topic_audits_no_id_and_no_grant(build_scene):
    """R21. Refusal 5 names a grant in its text and in the grant's log, but the line has only what the call gave."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        rid = request_id()
        assert (await grantee_edit(client, held, "resume", NAME, "text", 0, request_id=rid)).get("error") == "forbidden"
        line = without_time(scene.audit()[-1])
        assert line == guest_line(
            held, "edit_reference", topic="resume", reference=NAME, request_id=rid, expected_revision=0
        ) | {"outcome": "forbidden"}

    play(scene.host, scenario)


def test_the_read_tools_audit_the_topic_its_id_and_the_name_and_no_revision_or_grant(build_scene):
    """R21. A read's line has the topic, its id and, when a name was given, the reference, whatever the mode; a revision
    asked for, a page size and a cursor are not in it. An alias-bound reader's line has the alias; a refused read has
    the topic and name but no id."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        await write_revisions(client, scene, BOARDS, NAME, 3)
        guest = await bound_guest(client, scene)
        who = Core(scene.host, "worker")
        topic = {"topic": BOARDS, "topic_id": scene.topic_id(BOARDS)}
        worker = {"role": "worker", "credential_role": "worker", "credential_id": "worker-token", "outcome": "ok"}
        for name, make, extra in (
            ("read_reference", lambda: read_reference(client, who, BOARDS), {}),
            ("read_reference", lambda: read_reference(client, who, BOARDS, NAME), {"reference": NAME}),
            ("read_reference", lambda: read_reference(client, who, BOARDS, NAME, 2), {"reference": NAME}),
            (
                "reference_history",
                lambda: reference_history(client, who, BOARDS, NAME, limit=2, after_revision=1),
                {"reference": NAME},
            ),
        ):
            succeeded(await make())
            assert without_time(scene.audit()[-1]) == {**worker, "tool": name, **topic, **extra}
        succeeded(await read_reference(client, guest, BOARDS, NAME, 1))
        assert without_time(scene.audit()[-1]) == {
            "role": "guest",
            "credential_role": "guest",
            "credential_id": "guest-token",
            "tool": "read_reference",
            "alias_id": guest.alias_id,
            "alias_code": guest.code,
            "outcome": "ok",
            **topic,
            "reference": NAME,
        }
        assert (await read_reference(client, who, BOARDS, "no-such")).get("error") == "unknown_reference"
        assert without_time(scene.audit()[-1]) == {
            **worker,
            "tool": "read_reference",
            "topic": BOARDS,
            "reference": "no-such",
            "outcome": "unknown_reference",
        }

    play(scene.host, scenario)


# 2. What must never be in it ----------------------------------------------------------------------------------


def test_a_body_is_in_no_audit_line_event_response_or_refusal_text(build_scene, caplog):
    """R21. A unique marker in a body is written to the revision, and to nowhere else: not the audit line, a grant
    event, a refused use, a refusal's text or any other table, whether the call succeeded, was stale, was refused
    for its shape, its size, its role, or by a revoked grant."""
    # Mutant 20: the audit line writing the body argument.
    caplog.set_level(logging.DEBUG)
    scene = build_scene()
    marker = "marker-" + secrets.token_hex(8)

    async def scenario(client):
        held = await board(client, scene)
        text = f"{marker} written by the planner"
        answers = [
            await core_edit(client, scene, BOARDS, NAME, text, 0),
            await core_edit(client, scene, BOARDS, NAME, text, 0),  # stale
            await core_edit(client, scene, BOARDS, "Upper", text, 0),  # name
            await core_edit(client, scene, BOARDS, NAME, marker + "x" * 40000, 1),  # size
            await core_edit(client, scene, BOARDS, "other", text, 0, role="worker"),  # role
            await grantee_edit(client, held, BOARDS, NAME, text, 1),
        ]
        scene.revoke(held.grant_id)
        answers.append(await grantee_edit(client, held, BOARDS, NAME, text, 2))  # revoked
        assert [one.get("error") for one in answers] == [
            None,
            "stale_revision",
            "invalid_request",
            "limit_exceeded",
            "forbidden",
            None,
            "forbidden",
        ]
        assert marker not in json.dumps(answers)
        assert marker not in scene.audit_text() and marker not in caplog.text
        with scene.db() as conn:
            for table in [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
                if table in ("reference_revisions",) or "_fts" in table:
                    continue
                for row in conn.execute(f"SELECT * FROM {table}"):
                    assert marker not in " ".join(str(value) for value in tuple(row)), table
        assert len(scene.refused_uses()) == 1

    play(scene.host, scenario)


def test_the_grant_key_is_in_none_of_it_even_when_passed_as_the_topic_the_name_or_the_body_of_a_refused_call(
    build_scene, caplog
):
    """R22. A key mistakenly put in `topic`, `name` or the `body` of a call that is then refused appears in no response,
    audit line, event, table cell or log record, whether the caller is a grantee or a core role that sends the key
    along as its `grant_token`. The topic and the name it was given in are dropped from the line, not written."""
    # Q11: S3 relies on the guard's redaction of the key in refusal text. Mutant 20.
    caplog.set_level(logging.DEBUG)
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        answers = []
        for key_as in ({"topic": held.key}, {"name": held.key}, {"body": held.key, "expected_revision": 0}):
            arguments = {**edit_arguments(BOARDS, NAME, "text", 2), **key_as}
            answers.append(
                await tool(
                    client, scene.host, "edit_reference", {**arguments, "token": held.token, "grant_token": held.key}
                )
            )
            answers.append(
                await tool(client, scene.host, "edit_reference", {**arguments, "grant_token": held.key}, role="planner")
            )
            line = scene.audit()[-1]
            assert held.key not in json.dumps(line)
            assert ("topic" in line, "reference" in line) == ("topic" not in key_as, "name" not in key_as), line
        assert all(one.get("error") for one in answers), answers
        assert held.key not in json.dumps(answers)
        assert held.key not in scene.audit_text() and held.key not in caplog.text
        with scene.db() as conn:
            assert not [cell for cell in every_cell(conn) if held.key in cell]

    play(scene.host, scenario)


def test_a_schema_error_on_a_call_that_carries_a_key_and_no_binding_token_does_not_repeat_the_key(build_scene):
    """R22. The SDK's own error for arguments the schema refuses repeats the input it was given, cut in the middle, so
    the head and the tail of a key sent with it come back. The guard already checks the arguments of a call that
    carries a binding token before the SDK can do that; a call that carries a key and no token (a core role's, which
    ignores the key, or a guest's, which is refused for it) must be checked the same way. It is refused as a malformed
    request, and no part of the key is in the answer."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        arguments = {"topic": BOARDS, "name": NAME, "body": "text", "grant_token": held.key}  # no expected_revision
        for who in (Anonymous(scene.host), Core(scene.host)):
            result = await who.call(client, "edit_reference", **arguments)
            assert result.get("error") == "invalid_request", result
            text = json.dumps(result)
            assert held.key[:8] not in text and held.key[-12:] not in text, text
        assert scene.docs() == [] and scene.refused_uses() == []

    play(scene.host, scenario)


def test_a_key_sent_to_a_read_tool_that_declares_none_appears_in_no_line_or_answer(build_scene, caplog):
    """Q15. The read tools declare no `grant_token`, and whether the SDK drops or refuses an extra argument is not this
    change's business. What is: whichever it does, the key is not in the answer, the audit line or the log."""
    caplog.set_level(logging.DEBUG)
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 1)
        answers = [
            await Bound.of(held).call(client, "read_reference", topic=BOARDS, name=NAME, grant_token=held.key),
            await Bound.of(held).call(client, "reference_history", topic=BOARDS, name=NAME, grant_token=held.key),
            await Core(scene.host).call(client, "read_reference", topic=BOARDS, grant_token=held.key),
        ]
        assert held.key not in json.dumps(answers)
        assert held.key not in scene.audit_text() and held.key not in caplog.text

    play(scene.host, scenario)


def test_a_binding_token_in_the_topic_or_the_name_is_dropped_from_the_line(build_scene):
    """R21. The binding token as `topic` and as `name` of a refused call is written nowhere: the guard drops any audited
    value that holds it, and a token-shaped value is not a topic path or a name in any case."""
    # Mutant 20: reference written without the name-grammar check.
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for bad in ({"topic": held.token}, {"name": held.token}):
            result = await Keyed(held).call(
                client, "edit_reference", **{**edit_arguments(BOARDS, NAME, "text", 0), **bad}
            )
            assert result.get("error") == "invalid_request", result
            assert held.token not in json.dumps(result) and held.token not in scene.audit_text()
        shaped = secrets.token_urlsafe(32)
        for bad in ({"topic": shaped}, {"name": shaped}):
            assert (await core_edit(client, scene, **{"topic": BOARDS, "name": NAME, **bad}, body="x", expected=0)).get(
                "error"
            ) == "invalid_request"
            assert shaped not in scene.audit_text()

    play(scene.host, scenario)


def test_a_topic_or_name_that_is_not_well_formed_is_left_out_of_the_line_and_the_other_is_kept(build_scene):
    """R21. The line carries the topic only if it is a well-formed path and the reference only if it is a well-formed
    name, each on its own: a refused call with a good topic and a bad name has the topic and not the reference."""
    # Mutant 20: reference written without the name-grammar check.
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host)
        for topic, name, body, expect in (
            (BOARDS, "Not A Name", "x", {"topic": BOARDS}),
            ("Not/A/Topic", NAME, "x", {"reference": NAME}),
            ("Upper", "Upper", "x", {}),
            (BOARDS, NAME, "", {"topic": BOARDS, "reference": NAME}),
        ):
            result = await core_edit(client, scene, topic, name, body, 0)
            assert result.get("error") == "invalid_request", result
            line = scene.audit()[-1]
            kept = {key: line[key] for key in ("topic", "reference") if key in line}
            assert kept == expect, (topic, name, line)

    play(scene.host, scenario)


# 3. What a call leaves behind ----------------------------------------------------------------------------------


def test_a_successful_edit_writes_the_revision_and_nothing_more(build_scene):
    """R18. By a core role and by a grantee: the revision (and the document when it is new) is written, and no message,
    job event, topic change, grant event, notice, change-log entry or wake is. The grantee's hourly counter row is the
    one other thing, written by the guard."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        before = scene.counts()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0))
        middle = scene.counts()
        succeeded(await grantee_edit(client, held, BOARDS, NAME, body_of(2), 1))
        after = scene.counts()
        assert {key: middle[key] - before[key] for key in before if middle[key] != before[key]} == {
            "reference_docs": 1,
            "reference_revisions": 1,
        }
        assert {key: after[key] - middle[key] for key in middle if after[key] != middle[key]} == {
            "reference_revisions": 1,
            "alias_posts": 1,
        }

    play(scene.host, scenario)


def test_only_the_refusals_that_name_a_grant_write_a_refused_use_event(build_scene):
    """R18. S2's refusals 3, 4 and 5 each write exactly one `refused_use` event; refusals 1 and 2, a stale edit, a
    refusal for the shape, the size or the role, a conflict of request ids and the guard's own refusal write none."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        other = await scene.grantee(client, "lead-scout", BOARDS, ("reference.edit",), inherit=False)
        doomed = await scene.grantee(client, "board-bot", LEADS, ("reference.edit",), inherit=False)
        scene.revoke(doomed.grant_id)
        rid = request_id()
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(1), 0, request_id=rid))
        mine = edit_arguments(BOARDS, NAME, "text", 1, token=held.token, grant_token=held.key)
        kinds = [
            ("1 no binding", 0, {**mine, "token": None}),
            ("2 unknown key", 0, {**mine, "grant_token": support.new_key()}),
            ("3 another alias's key", 1, {**mine, "grant_token": other.key}),
            ("4 not active", 1, edit_arguments(LEADS, NAME, "text", 0, token=doomed.token, grant_token=doomed.key)),
            ("5 not covered", 1, {**mine, "topic": "resume"}),
            ("stale", 0, {**mine, "expected_revision": 0}),
            ("name", 0, {**mine, "name": "Upper"}),
            ("size", 0, {**mine, "body": "x" * 32769}),
            ("conflict", 0, {**mine, "expected_revision": 0, "body": body_of(1), "request_id": rid}),
            ("guard", 0, {**mine, "grant_token": None}),
        ]
        for label, added, arguments in kinds:
            before = len(scene.refused_uses())
            result = await tool(client, scene.host, "edit_reference", arguments)
            assert result.get("error"), (label, result)
            assert len(scene.refused_uses()) - before == added, (label, result)
        role = await core_edit(client, scene, BOARDS, NAME, "text", 1, role="worker", grant_token=held.key)
        assert role.get("error") == "forbidden" and len(scene.refused_uses()) == 3

    play(scene.host, scenario)


def test_the_read_tools_write_no_grant_event_and_no_hourly_counter_row(build_scene):
    """R2, R20. Reads by every kind of caller leave `topic_grant_events` and `alias_posts` as they were."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        before = (len(scene.events()), scene.posts_counted())
        for who in (Core(scene.host), Bound.of(held), await bound_guest(client, scene)):
            succeeded(await read_reference(client, who, BOARDS, NAME))
            succeeded(await reference_history(client, who, BOARDS, NAME))
        assert (len(scene.events()), scene.posts_counted()) == before

    play(scene.host, scenario)
