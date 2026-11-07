"""What crosses the MCP boundary into the reference tools: whole numbers that are whole numbers, a request id and a
grant key that the database can take, and every argument as the caller sent it.

The SDK reads a plain `int` argument leniently. Before a body ran it made 0 of `false`, 1 of `true`, 5 of "5" and 5 of
5.0, so the module's own checks, which refuse a bool, never saw what the caller sent. The four whole numbers of the
three tools (`expected_revision`, `revision`, `after_revision` and `limit`) are strict, and a value of another type is
refused by the schema as one out of range is. A core role gets the SDK's validation error and no audit line. A call
that carries a binding token gets the guard's refusal of arguments that do not match the schema. Neither runs a body,
and nothing is written or read.

A request id that holds a lone surrogate, sent as a JSON escape such as `\\ud800`, has no UTF-8 form, and the database
cannot bind it. The edit used to crash on it after the guard had admitted the call: the caller got the SDK's "Error
executing tool" sentence and the call left no audit line. It is refused before the edit's transaction, with the
refusal any other malformed request id gets, and the call has its audit line.

A grant key that holds a lone surrogate is the same kind of argument. The check every grant tool shares hashed it, so a
grantee's `edit_reference`, `create_topic` and `set_topic` each crashed the same way, and `edit_reference` had spent
the grantee's hourly post besides. A key that opens nothing has always been refused as an unknown key, whatever its
format. One with no UTF-8 form is refused as that now, before it is hashed, and it leaves what any such key leaves.

The SDK also reads a string argument as the JSON it spells, and a string that spells null, a list or an object is no
longer that string. `read_reference` took the name "null" for no name and listed the topic's references, a revision
beside it failed with "revision needs a name", an edit's request id "null" became a made-up one, so its retry came
back stale, and one like "[1]" was refused by the schema. The three tools take every argument as sent, and the string
"null" for a whole number is refused as the string it is. Nothing else about how they are called changes.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import replace

import pytest
import reference_support as refs
import topic_grant_tools_support as support
from reference_support import (
    BINDING_GONE,
    BOARDS,
    F13,
    NAME,
    NEEDS_BINDING,
    READ_KEYS,
    Anonymous,
    Bound,
    Core,
    Keyed,
    Staging,
    as_the_guard_calls,
    board,
    body_of,
    core_edit,
    direct,
    edit_arguments,
    grantee_edit,
    read_reference,
    reference_history,
    refused_by_schema,
    write_revisions,
)
from topic_grant_tools_support import play, succeeded, thread

from baton.errors import Forbidden, InvalidRequest
from baton.topics import grants as topic_grants

build_scene = refs.build_scene  # the fixture, under the name the tests ask for

# What the SDK used to read as a number: a bool, a numeral in a string, a float with no fraction.
EXPECTED = [False, True, "0", "1", 0.0, 1.0]
REVISION = [True, False, "1", 1.0]
LIMIT = [True, "5", 5.0]
CURSOR = [False, True, "0", 0.0]
MALFORMED_ID = {"error": "invalid_request", "detail": F13}


def audit_lines(scene):
    """The audit file's lines as strict UTF-8, split on newlines alone: a JSON string never holds a raw one."""
    path = scene.host.config.audit_path
    return path.read_bytes().decode("utf-8").split("\n")[:-1] if path.exists() else []


def refused_as_mistyped(result, argument, value):
    """`refused_by_schema`, and by the SDK's `int_type` error, not a bound's. `revision=false` was refused before this
    change too, as 0 is below 1, so only the kind of error shows the type was looked at. Add the rejected input to any
    assertion's traceback notes."""
    try:
        refused_by_schema(result, argument)
        assert "int_type" in str(result.get("unparsed", result)), result
    except AssertionError as error:
        error.add_note(f"{argument}={value!r}")
        raise


def refused_as_text(result, argument):
    """`refused_as_mistyped`, and for a string the schema saw as one: its error says `input_type=str`. Read as the JSON
    it spells, "null" is refused as a null (`NoneType`) or, for `revision`, not refused at all."""
    refused_as_mistyped(result, argument, "null")
    assert "input_type=str" in str(result.get("unparsed", result)), result


def schema_refusal(tool_name):
    """The guard's answer to a call that carries a binding token and arguments the schema does not take."""
    return {"error": "invalid_request", "detail": f"{tool_name} arguments do not match the tool schema"}


def what_it_raised(call):
    """The exception `call` raised, or a failed assertion if it raised none, so that a crash fails by an assertion
    that shows it."""
    try:
        call()
    except Exception as error:
        return error
    raise AssertionError("nothing was raised")


# 1. Whole numbers ---------------------------------------------------------------------------------------------


def test_mistyped_and_null_numbers_are_refused_by_the_schema_and_write_or_read_nothing(build_scene):
    """Each whole number is a whole number and nothing else, and the string "null" is refused as the string it is.
    `expected_revision`: `false`, `true`, "0", "1", 0.0, 1.0 and "null" are each refused by the schema, whether the
    reference is new or already at revision 1, and so is leaving it out: it is not defaulted to anything. The
    compare-and-swap must not run on a number the caller did not send. `revision`: `true`, `false`, "1", 1.0 and "null"
    are refused and no text comes back; `true` used to read revision 1, and "null" read as no revision read the current
    text. `limit`: `true`, "5", 5.0 and "null"; `after_revision`: `false`, `true`, "0", 0.0 and "null"; each is refused
    and lists no revision. No call writes a document, a revision or an audit line."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        who = Core(scene.host, "worker")

        async def refused_each(state, argument, values, call):
            tables, lines = scene.tables(), audit_lines(scene)
            for wrong in values:
                sent = f"{argument}={wrong!r} on {state}"
                result = await call(wrong)
                if wrong == "null":
                    refused_as_text(result, argument)
                else:
                    refused_as_mistyped(result, argument, wrong)
                assert "board list" not in str(result).lower(), f"{sent} returned text: {result!r}"
                assert scene.tables() == tables, f"{sent} changed tables"
                assert audit_lines(scene) == lines, f"{sent} wrote an audit line"

        def edit(wrong):
            return core_edit(client, scene, BOARDS, NAME, "text", wrong)

        def read(wrong):
            return read_reference(client, who, BOARDS, NAME, wrong)

        await refused_each("a new reference", "expected_revision", [*EXPECTED, "null"], edit)
        tables, lines = scene.tables(), audit_lines(scene)
        missing = await Core(scene.host).call(client, "edit_reference", topic=BOARDS, name=NAME, body="text")
        refused_by_schema(missing, "expected_revision")
        assert scene.tables() == tables and audit_lines(scene) == lines, "a missing expected_revision wrote something"
        await write_revisions(client, scene, BOARDS, NAME, 1)
        await refused_each("revision 1", "expected_revision", [*EXPECTED, "null"], edit)
        await refused_each("revision 1", "revision", ["null"], read)
        succeeded(await core_edit(client, scene, BOARDS, NAME, body_of(2), 1))
        await refused_each("revision 2", "revision", REVISION, read)
        for argument, values in (("limit", [*LIMIT, "null"]), ("after_revision", [*CURSOR, "null"])):
            await refused_each(
                "revision 2",
                argument,
                values,
                lambda wrong, argument=argument: reference_history(client, who, BOARDS, NAME, **{argument: wrong}),
            )

    play(scene.host, scenario)


def test_a_call_with_a_binding_token_is_refused_a_mistyped_number_as_it_is_an_out_of_range_one(build_scene):
    """A grantee's edit and a bound guest's reads meet the guard's own check of the schema before the SDK's, and an
    out-of-range number has always been answered with its one refusal of arguments that do not match the tool schema.
    The first value of each list is out of range, and every mistyped value must be answered exactly as it is. Nothing
    is written and no hourly post is spent. `false` is not tried as a `revision`, which the old schema refused too.
    The guard must read the arguments as the SDK will, so "null" for each whole number is one more mistyped value:
    checked with the SDK's reading, `revision` passed the guard, and the SDK's own error came back instead."""
    scene = build_scene()
    guest_calls = {
        "edit_reference": (edit_arguments(BOARDS, NAME, "text", 2), {"expected_revision": [-1, *EXPECTED, "null"]}),
        "read_reference": ({"topic": BOARDS, "name": NAME}, {"revision": [0, True, "1", 1.0, "null"]}),
        "reference_history": (
            {"topic": BOARDS, "name": NAME},
            {"limit": [0, *LIMIT, "null"], "after_revision": [-1, *CURSOR, "null"]},
        ),
    }

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 2)
        tables, posts = scene.tables(), scene.posts_counted()
        for tool_name, (fixed, varied) in guest_calls.items():
            who = Keyed(held) if tool_name == "edit_reference" else Bound.of(held)
            for argument, values in varied.items():
                for value in values:
                    answer = await who.call(client, tool_name, **{**fixed, argument: value})
                    assert answer == schema_refusal(tool_name), (tool_name, argument, value, answer)
        assert scene.tables() == tables and scene.posts_counted() == posts

    play(scene.host, scenario)


# 2. A request id with no UTF-8 form -------------------------------------------------------------------------


def test_a_request_id_with_no_utf8_form_is_refused_before_the_transaction_and_the_call_is_audited(
    build_scene, monkeypatch
):
    """A request id that is a lone surrogate, sent as a JSON escape, is answered with the refusal any malformed request
    id gets, not the SDK's execution error, for a core role and for a grantee. The edit runs no statement and writes
    nothing. It leaves exactly one audit line, which is valid UTF-8, parses as JSON, names the tool and the outcome, and
    holds the request id as sent. A grantee's attempt still spends its hourly post, as every refused edit does."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        staging = Staging(monkeypatch)
        attempts = {
            "core role": (lambda: core_edit(client, scene, BOARDS, NAME, "text", 0, request_id="\ud800"), 0),
            "grantee": (lambda: grantee_edit(client, held, BOARDS, NAME, "text", 0, request_id="\ud800"), 1),
        }
        for caller, (edit, spent) in attempts.items():
            tables, lines, posts = scene.tables(), audit_lines(scene), scene.posts_counted()
            staging.forget()
            answer = await edit()
            assert answer == MALFORMED_ID, (caller, answer)
            assert scene.tables() == tables and staging.texts() == [], (caller, staging.texts())
            written = audit_lines(scene)[len(lines) :]
            assert len(written) == 1, (caller, written)
            line = json.loads(written[0])
            assert (line["tool"], line["outcome"], line["request_id"]) == (
                "edit_reference",
                "invalid_request",
                "\ud800",
            ), (caller, line)
            assert scene.posts_counted() == posts + spent, caller

    play(scene.host, scenario)


def test_the_module_refuses_a_request_id_with_no_utf8_form_before_it_opens_a_transaction(build_scene, monkeypatch):
    """Called directly, past the guard and the SDK, the edit itself raises the refusal any malformed request id gets,
    runs no statement and writes nothing: the guard's audit line depends on the refusal being raised, not on the SDK."""
    scene = build_scene()
    staging = Staging(monkeypatch)

    for bad in ("\ud800", "\udfff", "id-\udc00-id"):
        tables = scene.tables()
        raised = what_it_raised(
            lambda bad=bad: direct(
                scene,
                "edit_reference",
                "planner",
                topic=BOARDS,
                name=NAME,
                body="text",
                expected_revision=0,
                request_id=bad,
            )
        )
        assert isinstance(raised, InvalidRequest) and str(raised) == F13, repr(raised)
        assert scene.tables() == tables and staging.texts() == [], repr(bad)


def test_a_request_id_is_refused_for_having_no_utf8_form_and_never_for_its_letters(build_scene):
    """The line is the encoding, not ASCII. After a lone surrogate is refused, request ids of accented letters and of
    characters beyond the basic plane are kept as given, up to 200 characters, and the retry of each replays."""
    scene = build_scene()
    kept = ["caf\u00e9-\u00fc", "clef-\U0001d11e", "\U0001f600" * 200]

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        assert await core_edit(client, scene, BOARDS, NAME, "text", 0, request_id="\ud800") == MALFORMED_ID
        for number, rid in enumerate(kept):
            first = succeeded(await core_edit(client, scene, BOARDS, NAME, "text", number, request_id=rid))
            again = succeeded(await core_edit(client, scene, BOARDS, NAME, "text", number, request_id=rid))
            assert (first["request_id"], first["created"], again["created"]) == (rid, True, False), rid
        assert [row["request_id"] for row in scene.revisions(BOARDS, NAME)] == kept

    play(scene.host, scenario)


# 3. A grant key with no UTF-8 form --------------------------------------------------------------------------


def rows_of_every_table(scene):
    """Every row of every ordinary table but the hourly post counter, which a test counts apart."""
    with scene.db() as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " AND name NOT LIKE '%\\_fts%' ESCAPE '\\' AND name != 'alias_posts' ORDER BY name"
            )
        ]
        return {name: sorted(repr(tuple(row)) for row in conn.execute(f"SELECT * FROM {name}")) for name in names}


async def left_behind(client, scene, call):
    """Make one call and report all it did: its answer, the audit lines it wrote (without their times), the grant
    events and hourly posts it added, and whether any other table changed."""
    tables, lines = rows_of_every_table(scene), audit_lines(scene)
    events, posts = len(scene.events()), scene.posts_counted()
    answer = await call()
    written = [json.loads(line) for line in audit_lines(scene)[len(lines) :]]
    return {
        "answer": answer,
        "audit lines": [{key: value for key, value in line.items() if key != "at"} for line in written],
        "grant events": len(scene.events()) - events,
        "hourly posts": scene.posts_counted() - posts,
        "a table changed": rows_of_every_table(scene) != tables,
    }


def test_a_grant_key_with_no_utf8_form_gets_what_any_key_that_opens_nothing_gets(build_scene, caplog):
    """A grantee that presents a lone surrogate as its grant key, sent as a JSON escape, bare or inside a key shape, is
    answered, audited and counted exactly as one that presents an ASCII key of the wrong format: the same refusal, the
    same one audit line, no grant event, the same hourly posts (one for an edit, none for the topic tools) and no table
    changed. The three tools share the check, so each is tried. A key-shaped string no grant has, and letters outside
    ASCII that do have a UTF-8 form, get the same. The key, as sent and as JSON writes it, is in no answer, audit line
    or log."""
    caplog.set_level(logging.DEBUG)
    scene = build_scene()
    pieces = [secrets.token_urlsafe(12), secrets.token_urlsafe(12)]
    lones = {"bare": "\ud800", "in a key shape": f"tgk_{pieces[0]}\udfff{pieces[1]}"}
    unknown_key = {"error": "forbidden", "detail": "unknown topic grant key"}

    async def scenario(client):
        held = await board(client, scene)
        await support.stage(client, scene.host, paths=("job-hunt/leads",))
        moving = await thread(client, scene.host, "job-hunt/leads")
        calls = {
            "edit_reference": lambda who: grantee_edit(client, who, BOARDS, NAME, "text", 0),
            "create_topic": lambda who: who.call(client, "create_topic", path="job-hunt/new"),
            "set_topic": lambda who: who.call(client, "set_topic", conversation_id=moving, topic="job-hunt/boards"),
        }
        answers = []
        for tool_name, call in calls.items():
            for shape, lone in lones.items():

                async def present(key, call=call):
                    return await left_behind(client, scene, lambda: call(replace(held, key=key)))

                wrong = await present("not-a-grant-key")
                assert wrong["answer"] == unknown_key, (tool_name, wrong)
                assert [line["outcome"] for line in wrong["audit lines"]] == ["forbidden"], (tool_name, wrong)
                assert (wrong["grant events"], wrong["a table changed"]) == (0, False), (tool_name, wrong)
                assert wrong["hourly posts"] == (1 if tool_name == "edit_reference" else 0), (tool_name, wrong)
                for what, key in (
                    ("a lone surrogate", lone),
                    ("a key-shaped string no grant has", support.new_key()),
                    ("letters outside ASCII", "caf\xe9-\U0001f600"),
                ):
                    got = await present(key)
                    for field in wrong:
                        assert got[field] == wrong[field], (tool_name, shape, what, field, got[field], wrong[field])
                    if what == "a lone surrogate":
                        answers.append(got["answer"])
        seen = {
            "the answers": "".join(json.dumps(answer) + repr(answer) for answer in answers),
            "the audit file": "\n".join(audit_lines(scene)),
            "the log": caplog.text,
        }
        for where, text in seen.items():
            for form in (*lones.values(), *(json.dumps(lone)[1:-1] for lone in lones.values()), *pieces):
                assert form not in text, (where, form)

    play(scene.host, scenario)


def test_the_grant_check_refuses_a_key_with_no_utf8_form_before_it_hashes_it_or_asks_the_database(build_scene):
    """Called directly, `grant_for_use` raises the refusal any key that opens nothing gets, a plain `Forbidden` that
    names no grant, and the database sees no statement for it, while it does see one for an ASCII key. With no
    binding the refusal is still the binding's own."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        identity = as_the_guard_calls(scene, held)["identity"]
        for bad in ("\ud800", "\udfff", "tgk_a\udc00b"):
            with scene.db() as conn:
                statements = []
                conn.set_trace_callback(statements.append)
                raised = what_it_raised(lambda bad=bad: topic_grants.grant_for_use(conn, identity, bad))
                asked = len(statements)
                ordinary = what_it_raised(lambda: topic_grants.grant_for_use(conn, identity, "not-a-grant-key"))
                conn.set_trace_callback(None)
                unbound = what_it_raised(lambda bad=bad: topic_grants.grant_for_use(conn, None, bad))
            assert type(raised) is Forbidden and str(raised) == "unknown topic grant key", repr(bad)
            assert asked == 0, (bad, statements)
            assert (type(ordinary), str(ordinary)) == (Forbidden, "unknown topic grant key") and len(statements) > 0
            assert type(unbound) is Forbidden and str(unbound) == NEEDS_BINDING, repr(bad)

    play(scene.host, scenario)


# 4. Arguments taken as sent ---------------------------------------------------------------------------------


def test_a_reference_named_null_is_read_by_name_and_not_listed(build_scene):
    """`null` is a name under the rules. A reference called that is read by its name, as its current text and as
    revision 1, and its history is listed, by a core role and by a guest holding only a binding. The SDK took the
    string for an omitted `name`, so a read listed the topic's references, and with a revision it answered "revision
    needs a name": the text could not be reached."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, "null", 2)
        succeeded(await core_edit(client, scene, BOARDS, NAME, "another one", 0))
        for who in (Core(scene.host, "worker"), Bound.of(held)):
            current = succeeded(await read_reference(client, who, BOARDS, "null"))
            assert set(current) == READ_KEYS, current
            assert (current["name"], current["revision"], current["body"]) == ("null", 2, body_of(2)), current
            first = succeeded(await read_reference(client, who, BOARDS, "null", 1))
            assert (first["name"], first["revision"], first["body"]) == ("null", 1, body_of(1)), first
            history = succeeded(await reference_history(client, who, BOARDS, "null"))
            assert history["name"] == "null" and [one["revision"] for one in history["revisions"]] == [1, 2], history
        listing = succeeded(await read_reference(client, Core(scene.host, "worker"), BOARDS))
        assert [one["name"] for one in listing["references"]] == [NAME, "null"], listing

    play(scene.host, scenario)


@pytest.mark.parametrize("caller", ["core role", "grantee"])
def test_a_request_id_that_reads_as_json_is_kept_as_sent_and_its_retry_replays(build_scene, caller):
    """A request id is any text of 1 to 200 characters. `null`, `[1]` and `{"a": 1}` are each kept as sent, each on a
    reference of its own. The identical retry gets the stored revision with `created` false and writes nothing, and
    both calls' audit lines record the id as sent. The SDK turned "null" into a made-up id, so the retry was stale, and
    the schema refused the others."""
    scene = build_scene()

    async def scenario(client):
        held = await board(client, scene)
        for name, rid in (("rid-null", "null"), ("rid-list", "[1]"), ("rid-object", '{"a": 1}')):

            async def edit(name=name, rid=rid):
                if caller == "core role":
                    return await core_edit(client, scene, BOARDS, name, "text", 0, request_id=rid)
                return await grantee_edit(client, held, BOARDS, name, "text", 0, request_id=rid)

            lines = len(audit_lines(scene))
            first = succeeded(await edit())
            assert (first["request_id"], first["created"]) == (rid, True), (rid, first)
            tables = scene.tables()
            retry = succeeded(await edit())
            assert retry == {**first, "created": False}, (rid, retry)
            assert scene.tables() == tables, rid
            written = [json.loads(line) for line in audit_lines(scene)[lines:]]
            edits = [(one["outcome"], one.get("request_id")) for one in written if one.get("tool") == "edit_reference"]
            assert edits == [("ok", rid), ("ok", rid)], (rid, edits)

    play(scene.host, scenario)


def test_a_body_that_reads_as_json_is_stored_and_read_back_as_sent(build_scene):
    """PIN. An edit's text is a plain string argument, which the SDK never read as JSON, so `null`, `[1]` and
    `{"a": 1}` are each stored and read back verbatim. It passes before and after the change, and keeps the three
    tools' other string arguments honest about what taking them as sent means."""
    scene = build_scene()

    async def scenario(client):
        await support.stage(client, scene.host, paths=refs.REFERENCE_TOPICS)
        for number, text in enumerate(["null", "[1]", '{"a": 1}']):
            made = succeeded(await core_edit(client, scene, BOARDS, NAME, text, number))
            assert made["size"] == len(text.encode()), made
            read = succeeded(await read_reference(client, Core(scene.host, "worker"), BOARDS, NAME))
            assert (read["revision"], read["body"]) == (number + 1, text), read

    play(scene.host, scenario)


@pytest.mark.parametrize("argument", ["token", "grant_token"])
def test_the_string_null_as_a_token_or_a_grant_key_is_refused_like_any_other(build_scene, argument):
    """The string "null" as `token` or as `grant_token` is a token or a key that opens nothing, and is refused as any
    other is, with the same answer, audit lines, grant events, hourly posts and tables: on the shared guest login
    (an unknown binding), on a core credential (which takes no binding token), and for a key, on a guest with a
    binding. A core role ignores a key, whatever it says. The SDK dropped "null" as an omitted argument, so a core
    role's call went through with its token ignored, and an edit by a guest was refused as if it had sent none. The
    read tools declare no `grant_token`, so for them that case is a PIN: any key is dropped there."""
    scene = build_scene()
    other = {"token": "not-a-token", "grant_token": "not-a-grant-key"}[argument]
    refusals = {
        ("the shared guest login", "token"): BINDING_GONE,
        ("a core credential", "token"): "a binding token is accepted only on a guest credential",
    }

    async def scenario(client):
        held = await board(client, scene)
        await write_revisions(client, scene, BOARDS, NAME, 1)
        for tool_name in ("edit_reference", "read_reference", "reference_history"):
            arguments = (
                edit_arguments(BOARDS, NAME, "text", 0)
                if tool_name == "edit_reference"
                else {"topic": BOARDS, "name": NAME}
            )  # an edit that is stale when it gets that far writes nothing
            planner = Core(scene.host, "planner")
            if argument == "token":
                callers = {"the shared guest login": (Anonymous(scene.host), {}), "a core credential": (planner, {})}
                if tool_name == "edit_reference":
                    callers["the shared guest login, with a real grant key"] = (
                        Anonymous(scene.host),
                        {"grant_token": held.key},
                    )
            else:
                callers = {"a guest with a binding": (Bound.of(held), {}), "a core credential": (planner, {})}

            async def present(who, extra, value, tool_name=tool_name, arguments=arguments):
                return await left_behind(
                    client, scene, lambda: who.call(client, tool_name, **arguments, **extra, **{argument: value})
                )

            for name, (who, extra) in callers.items():
                asked, wrong = await present(who, extra, "null"), await present(who, extra, other)
                for field in wrong:
                    assert asked[field] == wrong[field], (tool_name, name, field, asked[field], wrong[field])
                expected = refusals.get((name.split(",")[0], argument))
                if expected is not None:
                    assert wrong["answer"] == {"error": "forbidden", "detail": expected}, (tool_name, name, wrong)
            if argument == "grant_token" and tool_name == "edit_reference":
                guest = await present(Bound.of(held), {}, other)
                assert guest["answer"] == {"error": "forbidden", "detail": "unknown topic grant key"}, guest

    play(scene.host, scenario)
