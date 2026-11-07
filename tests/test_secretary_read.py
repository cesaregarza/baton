"""The secretary read path: everything delivered to one principal, whatever its kind, and the context
beside it, after a position; or the messages it can see by id. Message text is included.

It reads through the read-only opener with the policy's visibility rule, and writes nothing. Every read here goes
through `baton secretary read` or the standalone entry behind `python -m baton.secretary_read`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from secretary_support import conversation, make_world, say, table_hashes

import baton
import baton.agents.secretary.inbox.read as read_module
from baton import cli, conversations, events, jobs, messages
from baton.database import store
from baton.database.store import transaction

SECRET = "the private phrase"
RELAYED = [{"source": "upstream", "at": "2026-11-02T00:00:00Z", "text": "Relayed words"}]
NEW_KIND = "synthetic.notice"  # a kind of delivery no secretary code names


@pytest.mark.parametrize("action", [["read", "--after", "0"], ["show"], ["decide", "--budget-seconds", "1"]])
def test_secretary_commands_refuse_remote_host_before_access(tmp_path, capsys, action) -> None:
    code = cli.main(
        [
            "--host",
            "http://invalid.example",
            "secretary",
            "--policy",
            str(tmp_path / "missing.json"),
            "--state",
            str(tmp_path / "private.sqlite"),
            *action,
        ]
    )
    assert code == 2
    assert "host-local" in json.loads(capsys.readouterr().out)["detail"]
    assert not (tmp_path / "private.sqlite").exists()


def _read(world, capsys, *args: str) -> tuple[int, dict]:
    """`baton secretary --policy FILE read ...` with no --state: its exit code and the JSON it printed."""
    try:
        code = cli.main(
            ["--config", str(world.config_path), "secretary", "--policy", str(world.policy_path), "read", *args]
        )
    except SystemExit as stopped:  # argparse refuses an unknown action or option
        code = stopped.code
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def _entries(page: dict) -> list[dict]:
    """An after-position page's deliveries and context together, in stream order."""
    return sorted(page.get("deliveries", []) + page.get("context", []), key=lambda entry: entry["pos"])


def _messages(page: dict) -> list[dict]:
    """The messages a page lists: by id, or carried by the events of an after-position page."""
    if "missing" in page:
        return page.get("messages", [])
    return [entry["message"] for entry in _entries(page) if entry.get("message")]


def _ids(page: dict) -> list[str]:
    return [message["id"] for message in _messages(page)]


def _kinds(page: dict, part: str) -> list[tuple[str, int]]:
    return [(entry["kind"], entry["pos"]) for entry in page.get(part, [])]


def _append(
    world,
    kind: str,
    *,
    actor: str = "worker",
    audience: str | None = "planner",
    job_id: str | None = None,
    data: dict | None = None,
) -> int:
    """An event of any kind, appended as a future producer would: nothing in the secretary is told about it."""
    with transaction(world.baton):
        events.append(
            world.baton,
            job_id=job_id,
            seq=0 if job_id is None else None,
            actor=actor,
            audience=audience,
            kind=kind,
            summary=f"{kind} for {audience}",
            data=data or {},
        )
    return world.baton.execute("SELECT MAX(pos) FROM events").fetchone()[0]


def _last_pos(world) -> int:
    return world.baton.execute("SELECT MAX(pos) FROM events").fetchone()[0]


def _stream_end(world, principal: str = "planner") -> int:
    """The last event in the principal's stream: one it received or sent."""
    return world.baton.execute(
        "SELECT MAX(pos) FROM events WHERE audience=? OR actor=?", (principal, principal)
    ).fetchone()[0]


def test_after_a_position_lists_each_message_the_principal_sees_once_with_its_text(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        direct = messages.send(world.baton, sender="worker", recipient="planner", text="Direct words")
        asked = say(world, room, "planner", "Please check", attention=["worker"])  # one copy per other participant
        reply = say(world, room, "worker", "Reply words", reply_to=asked["message_id"], relayed=RELAYED)
        alias_id = "al_0000000000000001"
        world.baton.execute(
            "INSERT INTO aliases (id,name,skeleton,verifier,created_at) VALUES (?,?,?,?,?)",
            (alias_id, "lane-a", "lane-a", "synthetic-verifier", 0.0),
        )
        guest = say(
            world, room, "guest", "Guest words", identity={"alias_id": alias_id, "alias": "lane-a", "code": "ab12"}
        )
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0 and page["principal"] == "planner" and page["after"] == 0
        assert _ids(page) == [direct["message_id"], asked["message_id"], reply["message_id"], guest["message_id"]]
        first, own, answered, guested = _messages(page)
        assert (
            first["sender"],
            first["to"],
            first["conversation_id"],
            first["text"],
            first["relayed"],
            first["reply_to"],
        ) == ("worker", "planner", None, "Direct words", [], None)
        assert (own["sender"], own["to"], own["conversation_id"], own["attention"]) == (
            "planner",
            None,
            room,
            ["worker"],
        )
        assert (answered["reply_to"], answered["text"], answered["relayed"]) == (
            asked["message_id"],
            "Reply words",
            RELAYED,
        )
        assert guested["sender"] == f"guest:{alias_id}" and guested["alias"] == {"name": "lane-a", "code": "ab12"}
        assert all(message["at"] and type(message["pos"]) is int for message in _messages(page))
        assert [message["pos"] for message in _messages(page)] == sorted(m["pos"] for m in _messages(page))
        assert [entry["message"]["id"] for entry in page.get("context", [])] == [asked["message_id"]]  # its own post
        assert (page["through_pos"], page["more"]) == (_stream_end(world), False)
        assert _stream_end(world) < world.baton.execute("SELECT MAX(pos) FROM events").fetchone()[0]  # guest copies


def test_visibility_matches_the_principals_visible_messages_and_others_never_appear(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room, left = conversation(world), conversation(world)
        seen = [
            messages.send(world.baton, sender="worker", recipient="planner", text="To the principal"),
            messages.send(world.baton, sender="planner", recipient="worker", text="From the principal"),
            say(world, room, "reviewer", "In a shared room"),
        ]
        with transaction(world.baton):
            notice = messages.new_message_id()
            messages.store_copy(
                world.baton,
                message_id=notice,
                sender=None,
                recipient="planner",
                text="Host notice",
                notice={"kind": "limit"},
            )
        hidden = [
            messages.send(world.baton, sender="worker", recipient="reviewer", text=f"Between them: {SECRET}"),
            say(world, left, "worker", f"Before it left: {SECRET}"),
        ]
        conversations.remove(world.baton, world.config, left, "planner", actor="planner")
        hidden.append(say(world, left, "reviewer", f"After it left: {SECRET}"))
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0
        expected = {sent["message_id"] for sent in seen} | {notice}
        assert set(_ids(page)) == expected
        assert not {sent["message_id"] for sent in hidden} & set(_ids(page))
        assert SECRET not in json.dumps(page)
        code, by_id = _read(world, capsys, "--ids", ",".join(sent["message_id"] for sent in hidden))
        assert (code, by_id["messages"], by_id["missing"]) == (0, [], [sent["message_id"] for sent in hidden])
        assert SECRET not in json.dumps(by_id)


def test_reads_by_id_return_what_the_principal_can_see_and_list_the_rest_missing(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        visible = say(world, room, "worker", "Visible words", relayed=RELAYED)
        theirs = messages.send(world.baton, sender="worker", recipient="reviewer", text=f"Theirs: {SECRET}")
        absent = "msg_" + "0" * 16
        code, page = _read(world, capsys, "--ids", ",".join([theirs["message_id"], visible["message_id"], absent]))
        assert code == 0 and _ids(page) == [visible["message_id"]]
        assert page["missing"] == [theirs["message_id"], absent] and page["not_read"] == []
        message = page["messages"][0]
        assert (message["pos"], message["text"], message["relayed"], message["conversation_id"]) == (
            None,
            "Visible words",
            RELAYED,
            room,
        )
        assert SECRET not in json.dumps(page)


def test_a_page_is_bounded_by_count_and_bytes_and_the_next_one_starts_where_it_stopped(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        own = [say(world, room, "planner", f"Own post {number}") for number in range(2)]  # two copies each
        long = [messages.send(world.baton, sender="worker", recipient="planner", text="x" * 3000) for _ in range(3)]
        order = [sent["message_id"] for sent in own + long]
        pages, after = [], 0
        while True:
            code, page = _read(world, capsys, "--after", str(after), "--limit", "2", "--max-bytes", "4000")
            assert code == 0 and len(_entries(page)) <= 2 and page["through_pos"] >= after
            pages.append(page)
            after = page["through_pos"]
            if not page["more"]:
                break
        assert [message for page in pages for message in _ids(page)] == order  # nothing repeated, nothing skipped
        assert [len(_entries(page)) for page in pages] == [2, 1, 1, 1]  # the count bound, then the byte bound
        assert pages[0]["through_pos"] == max(
            event["pos"]
            for event in world.baton.execute("SELECT pos, data FROM events WHERE kind='message'")
            if json.loads(event["data"])["message_id"] == order[1]
        )
        code, alone = _read(world, capsys, "--after", str(pages[1]["through_pos"]), "--max-bytes", "1024")
        assert code == 0 and len(_entries(alone)) == 1 and alone["more"] is True  # one message larger than the bound
        code, by_id = _read(world, capsys, "--ids", ",".join(order[2:]), "--max-bytes", "4000")
        assert code == 0 and _ids(by_id) == order[2:3] and by_id["not_read"] == order[3:]


def test_the_read_path_opens_baton_read_only_and_writes_nothing(tmp_path, capsys, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        sent = messages.send(world.baton, sender="worker", recipient="planner", text="Unread words")
        before, unread = table_hashes(world.baton), messages.unread(world.baton, "planner")
        opened, connect = [], store.connect_read_only

        def watched(path: Path):
            conn = connect(path)
            opened.append((path, conn.execute("PRAGMA query_only").fetchone()[0]))
            return conn

        monkeypatch.setattr(store, "connect_read_only", watched)
        codes = [_read(world, capsys, "--after", "0")[0], _read(world, capsys, "--ids", sent["message_id"])[0]]
        assert codes == [0, 0]
        assert opened == [(world.config.db_path, 1), (world.config.db_path, 1)]
        assert table_hashes(world.baton) == before and messages.unread(world.baton, "planner") == unread
        assert not world.state_path.exists()


def test_the_standalone_entry_prints_the_same_page_and_exit_status_as_the_subcommand(tmp_path, capsys) -> None:
    """The page through the standalone `main`, in this process; then one real `python -m baton.secretary_read` run,
    a refusal, because only a nonzero code shows that the module entry hands main's exit status on."""
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Same words")
        code, from_cli = _read(world, capsys, "--after", "0", "--limit", "5")
        options = ["--config", str(world.config_path), "--policy", str(world.policy_path), "--after", "0"]
        standalone = read_module.main([*options, "--limit", "5"])
        assert (code, standalone) == (0, 0) and json.loads(capsys.readouterr().out) == from_cli
        root = Path(baton.__file__).resolve().parents[1]
        refused = subprocess.run(
            [sys.executable, "-m", "baton.secretary_read", *options, "--limit", "0"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(root)},
            check=False,
        )
        assert refused.returncode == 1 and json.loads(refused.stdout)["error"] == "invalid_request"


def test_a_read_outside_its_bounds_is_refused_before_anything_is_read(tmp_path, capsys) -> None:
    refused = {
        "neither": [],  # neither a position nor ids
        "both": ["--after", "0", "--ids", "msg_" + "1" * 16],
        "negative": ["--after", "-1"],
        "limit-0": ["--after", "0", "--limit", "0"],
        "limit-101": ["--after", "0", "--limit", "101"],
        "bytes-low": ["--after", "0", "--max-bytes", "1023"],
        "bytes-high": ["--after", "0", "--max-bytes", str(1024 * 1024 + 1)],
        "bad-id": ["--ids", "not-a-message-id"],
        "repeat": ["--ids", "msg_" + "1" * 16 + ",msg_" + "1" * 16],  # the same id twice
        "too-many-ids": ["--ids", ",".join(f"msg_{number:016x}" for number in range(101))],
    }
    with make_world(tmp_path) as world:
        before = table_hashes(world.baton)
        for name, args in refused.items():
            code, out = _read(world, capsys, *args)
            assert (code, out.get("error")) == (1, "invalid_request"), name
            assert table_hashes(world.baton) == before, name


def test_stateful_secretary_actions_require_their_state(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        code, page = _read(world, capsys, "--after", "0")
        assert (code, page.get("deliveries"), page.get("context")) == (0, [], [])
        for action in (["show"], ["decide", "--budget-seconds", "5"], ["acknowledge", "dec_0000000000000000"]):
            with pytest.raises(SystemExit) as absent:
                cli.main(["--config", str(world.config_path), "secretary", "--policy", str(world.policy_path), *action])
            assert absent.value.code == 2


def test_every_kind_delivered_to_the_principal_is_read_as_a_delivery_with_or_without_its_ids(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="job-kinds", actor="worker", title="Task")
        loose = _append(world, NEW_KIND, data={"note": "metadata only"})  # neither a message nor a job id
        on_job = _append(world, "job.synthetic", job_id=made["job_id"])  # a job id, no message id
        sent = messages.send(world.baton, sender="worker", recipient="planner", text="Direct words")
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0 and page.get("context") == []
        assert _kinds(page, "deliveries") == [
            ("job.created", loose - 1),
            (NEW_KIND, loose),
            ("job.synthetic", on_job),
            ("message", _last_pos(world)),
        ]
        created, notice, synthetic, message = page["deliveries"]
        assert (notice["actor"], notice["audience"], notice["job_id"], notice["message"], notice["data"]) == (
            "worker",
            "planner",
            None,
            None,
            {"note": "metadata only"},
        )
        assert (synthetic["job_id"], synthetic["message"], created["job_id"]) == (made["job_id"], None, made["job_id"])
        assert notice["summary"] == f"{NEW_KIND} for planner" and notice["at"]
        assert message["message"]["id"] == sent["message_id"] and message["message"]["text"] == "Direct words"
        assert (page["through_pos"], page["more"]) == (_last_pos(world), False)


def test_own_sends_and_job_context_come_out_as_context_and_never_as_deliveries(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="job-context", actor="worker", title="Task")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="planner")  # its own move
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff", actor="worker")  # to someone else
        own = messages.send(world.baton, sender="planner", recipient="worker", text="Own words")
        own_kind = _append(world, NEW_KIND, actor="planner", audience="worker")
        delivered = _append(world, NEW_KIND)
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0
        assert _kinds(page, "deliveries") == [("job.created", 1), (NEW_KIND, delivered)]
        assert [kind for kind, _ in _kinds(page, "context")] == ["job.moved", "job.moved", "message", NEW_KIND]
        assert page["context"][2]["message"]["id"] == own["message_id"] and page["context"][3]["pos"] == own_kind
        assert all(entry["audience"] != "planner" for entry in page["context"])


def test_pages_over_mixed_kinds_keep_their_position_and_repeat_the_same(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        for number in range(3):
            messages.send(world.baton, sender="worker", recipient="planner", text=f"Direct {number}")
            _append(world, NEW_KIND, data={"number": number})
            say(world, room, "planner", f"Own post {number}")  # two copies, listed once, as context
            _append(world, NEW_KIND, actor="planner", audience="reviewer", data={"number": number})
        whole = _read(world, capsys, "--after", "0", "--limit", "100")[1]
        assert [kind for kind, _ in _kinds(whole, "deliveries")] == ["message", NEW_KIND] * 3
        assert [kind for kind, _ in _kinds(whole, "context")] == ["message", NEW_KIND] * 3
        pages, after = [], 0
        while True:
            code, page = _read(world, capsys, "--after", str(after), "--limit", "3")
            assert code == 0 and len(_entries(page)) <= 3 and page["through_pos"] > after
            assert _read(world, capsys, "--after", str(after), "--limit", "3")[1] == page  # same query, same page
            pages.append(page)
            after = page["through_pos"]
            if not page["more"]:
                break
        assert [len(_entries(page)) for page in pages] == [3, 3, 3, 3]
        assert [entry for page in pages for entry in page["deliveries"]] == whole["deliveries"]
        assert [entry for page in pages for entry in page["context"]] == whole["context"]
        assert pages[-1]["through_pos"] == whole["through_pos"] == _last_pos(world) and whole["more"] is False


def test_a_delivery_of_any_kind_stays_within_the_principals_visibility(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        left = conversation(world)
        conversations.remove(world.baton, world.config, left, "planner", actor="planner")
        theirs = _append(world, NEW_KIND, audience="reviewer", data={"note": SECRET})
        limited = _append(world, "conversation.rate_limited", data={"conversation_id": left, "note": SECRET})
        hidden = say(world, left, "reviewer", f"After it left: {SECRET}")
        delivered = _append(world, NEW_KIND, data={"note": "for the principal"})
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0 and _kinds(page, "deliveries") == [(NEW_KIND, delivered)]
        positions = [entry["pos"] for entry in _entries(page)]
        assert theirs not in positions and limited not in positions and hidden["message_id"] not in _ids(page)
        assert SECRET not in json.dumps(page)


def test_read_refuses_to_migrate_an_old_source_database(tmp_path) -> None:
    import sqlite3
    from dataclasses import replace

    from baton.agents.secretary.inbox import read as secretary_read

    with make_world(tmp_path) as world:
        old_dir = tmp_path / "old-schema"
        old_dir.mkdir(mode=0o700)
        old_path = old_dir / "baton.db"
        with sqlite3.connect(old_path) as old:
            old.executescript((Path(__file__).parent / "fixtures" / "schema_v16.sql").read_text())
        old_path.chmod(0o600)
        before = old_path.read_bytes()
        config = replace(world.config, data_dir=old_dir)
        with pytest.raises(store.SchemaMismatch, match="schema"):
            secretary_read.read(config, world.policy_path, after=0)
        assert old_path.read_bytes() == before and not world.state_path.exists()
