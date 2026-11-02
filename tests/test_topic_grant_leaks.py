"""Three places a refusal or a log could say more than the caller gave, found in review, driven over MCP.

* `set_topic` refuses a thread whose current topic the grant does not cover. The caller did not supply that topic
  and may not be able to read the thread, so the refusal must not name it, and an uncovered topic, a thread with
  no topic and a stale label must read alike.
* A call refused by the server middleware before the guard takes `grant_token` out is audited from its raw
  arguments, so the audit line must drop an argument value holding the key there too.
* A call that reaches the SDK without a required argument gets a validation error that echoes part of its
  input, so with a key present the arguments are checked first and the error says nothing of them.
"""

from __future__ import annotations

import topic_grant_tools_support as support
from topic_grant_tools_support import BOARD, play, refused, stage, thread, tool

build_scene = support.build_scene  # the fixture, under the name the tests ask for

PRIVATE = "private-room"
WINDOW = 8


def windows(key: str) -> list[str]:
    return [key[start:start + WINDOW] for start in range(len(key) - WINDOW + 1)]


def test_a_refused_move_never_names_the_threads_current_topic(build_scene):
    """The three ways the source can fail the grant read the same, and none names it. The destination is the
    caller's own word and is still named."""
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host, (*BOARD, PRIVATE))
        held = await scene.grantee(client, "lane-a", "resume", ("topic.create_sub", "thread.organise"))
        uncovered = await thread(client, scene.host, PRIVATE)
        bare = await thread(client, scene.host, None)
        stale = await thread(client, scene.host, "job-hunt/leads")
        with scene.db() as conn:
            conn.execute("UPDATE conversations SET topic = 'ghost-room' WHERE id = ?", (stale,))
        details = []
        for moving in (uncovered, bare, stale):
            result = await held.call(client, "set_topic", conversation_id=moving, topic="resume")
            refused(result, "forbidden")
            for hidden in (PRIVATE, "ghost-room"):
                assert hidden not in str(result), result
            details.append(result["detail"])
        assert len(set(details)) == 1, details
        assert details[0].startswith(f"topic grant {held.grant_id} does not allow thread.organise on "), details
        elsewhere = await held.call(client, "set_topic", conversation_id=uncovered, topic="job-hunt")
        assert "job-hunt" in str(elsewhere) or "this thread" in str(elsewhere), elsewhere

    play(scene.host, scenario)


def test_a_call_refused_before_the_guard_leaves_the_key_out_of_the_audit_line(build_scene):
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        result = await held.call(client, "create_topic", path="job-hunt/new", request_id=held.key, from_role="x")
        assert result.get("error") == "forbidden_field", result
        assert held.key not in scene.audit_text()

    play(scene.host, scenario)


def test_a_validation_error_never_echoes_part_of_the_key(build_scene):
    scene = build_scene()

    async def scenario(client):
        await stage(client, scene.host)
        held = await scene.grantee(client, "lane-a", "job-hunt", inherit=True)
        for name in ("create_topic", "set_topic"):
            for presented in ({"grant_token": held.key}, {"grant_token": held.key, "token": held.token}):
                result = await tool(client, scene.host, name, presented)
                echoed = [one for one in windows(held.key) if one in str(result)]
                assert echoed == [], (name, sorted(presented), result)
                assert held.key not in scene.audit_text()

    play(scene.host, scenario)
