"""The secretary thread's read path: the messages one principal can see, text included, after a position or by id.

It reads through the kernel's read-only opener with the kernel's own visibility rule, and writes nothing. Every
read here goes through `baton secretary read` or `python -m baton.secretary_read`, as the thread runs it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from secretary_support import ALIAS_ID, conversation, make_world, say, table_hashes

import baton
from baton import cli, conversations, messages, secretary, secretary_store, store
from baton.store import transaction

SECRET = "the private phrase"
RELAYED = [{"source": "upstream", "at": "2026-11-02T00:00:00Z", "text": "Relayed words"}]


def _read(world, capsys, *args: str) -> tuple[int, dict]:
    """`baton secretary --policy FILE read ...` with no --state: its exit code and the JSON it printed."""
    try:
        code = cli.main(["--config", str(world.config_path), "secretary", "--policy", str(world.policy_path),
                         "read", *args])
    except SystemExit as stopped:  # argparse refuses an unknown action or option
        code = stopped.code
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def _ids(page: dict) -> list[str]:
    return [message["id"] for message in page.get("messages", [])]


def _stream_end(world, principal: str = "planner") -> int:
    """The last event in the principal's stream: one it received or sent."""
    return world.baton.execute("SELECT MAX(pos) FROM events WHERE audience=? OR actor=?",
                               (principal, principal)).fetchone()[0]


def _kernel_message_ids(world) -> set[str]:
    """What the kernel's own scan records for the principal, the visibility rule the read path must match."""
    secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
    state = secretary_store.open_state(world.state_path, world.config.data_dir)
    try:
        envelopes = [json.loads(row["envelope"]) for row in state.execute("SELECT envelope FROM items")]
    finally:
        state.close()
    return {envelope["message_id"] for envelope in envelopes if envelope["event_kind"] == "message"}


def test_after_a_position_lists_each_message_the_principal_sees_once_with_its_text(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        direct = messages.send(world.baton, sender="worker", recipient="planner", text="Direct words")
        asked = say(world, room, "planner", "Please check", attention=["worker"])  # one copy per other participant
        reply = say(world, room, "worker", "Reply words", reply_to=asked["message_id"], relayed=RELAYED)
        guest = say(world, room, "guest", "Guest words", identity={"alias_id": ALIAS_ID, "alias": "lane-a",
                                                                     "code": "ab12"})
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0 and page["principal"] == "planner" and page["after"] == 0
        assert _ids(page) == [direct["message_id"], asked["message_id"], reply["message_id"], guest["message_id"]]
        first, own, answered, guested = page["messages"]
        assert (first["sender"], first["to"], first["conversation_id"], first["text"], first["relayed"],
                first["reply_to"]) == ("worker", "planner", None, "Direct words", [], None)
        assert (own["sender"], own["to"], own["conversation_id"], own["attention"]) == ("planner", None, room,
                                                                                       ["worker"])
        assert (answered["reply_to"], answered["text"], answered["relayed"]) == (asked["message_id"], "Reply words",
                                                                                 RELAYED)
        assert guested["sender"] == f"guest:{ALIAS_ID}" and guested["alias"] == {"name": "lane-a", "code": "ab12"}
        assert all(message["at"] and type(message["pos"]) is int for message in page["messages"])
        assert [message["pos"] for message in page["messages"]] == sorted(m["pos"] for m in page["messages"])
        assert (page["through_pos"], page["more"]) == (_stream_end(world), False)
        assert _stream_end(world) < world.baton.execute("SELECT MAX(pos) FROM events").fetchone()[0]  # guest copies


def test_visibility_is_exactly_the_kernels_and_another_principals_items_never_appear(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        room, left = conversation(world), conversation(world)
        seen = [messages.send(world.baton, sender="worker", recipient="planner", text="To the principal"),
                messages.send(world.baton, sender="planner", recipient="worker", text="From the principal"),
                say(world, room, "reviewer", "In a shared room")]
        with transaction(world.baton):
            notice = messages.new_message_id()
            messages.store_copy(world.baton, message_id=notice, sender=None, recipient="planner", text="Host notice",
                                notice={"kind": "limit"})
        hidden = [messages.send(world.baton, sender="worker", recipient="reviewer", text=f"Between them: {SECRET}"),
                  say(world, left, "worker", f"Before it left: {SECRET}")]
        conversations.remove(world.baton, world.config, left, "planner", actor="planner")
        hidden.append(say(world, left, "reviewer", f"After it left: {SECRET}"))
        code, page = _read(world, capsys, "--after", "0")
        assert code == 0
        expected = {sent["message_id"] for sent in seen} | {notice}
        assert set(_ids(page)) == expected == _kernel_message_ids(world)
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
            None, "Visible words", RELAYED, room)
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
            assert code == 0 and len(page["messages"]) <= 2 and page["through_pos"] >= after
            pages.append(page)
            after = page["through_pos"]
            if not page["more"]:
                break
        assert [message for page in pages for message in _ids(page)] == order  # nothing repeated, nothing skipped
        assert [len(page["messages"]) for page in pages] == [2, 1, 1, 1]  # the count bound, then the byte bound
        assert pages[0]["through_pos"] == max(event["pos"] for event in world.baton.execute(
            "SELECT pos, data FROM events WHERE kind='message'") if json.loads(event["data"])["message_id"] == order[1])
        code, alone = _read(world, capsys, "--after", str(pages[1]["through_pos"]), "--max-bytes", "1024")
        assert code == 0 and len(alone["messages"]) == 1 and alone["more"] is True  # one message larger than the bound
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


def test_the_module_entry_prints_the_same_page_as_the_subcommand(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Same words")
        code, from_cli = _read(world, capsys, "--after", "0", "--limit", "5")
        root = Path(baton.__file__).resolve().parents[1]
        run = subprocess.run([sys.executable, "-m", "baton.secretary_read", "--config", str(world.config_path),
                              "--policy", str(world.policy_path), "--after", "0", "--limit", "5"],
                             capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(root)}, check=False)
        assert (code, run.returncode) == (0, 0) and json.loads(run.stdout) == from_cli
        refused = subprocess.run([sys.executable, "-m", "baton.secretary_read", "--config", str(world.config_path),
                                  "--policy", str(world.policy_path), "--after", "0", "--limit", "0"],
                                 capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(root)},
                                 check=False)
        assert refused.returncode == 1 and json.loads(refused.stdout)["error"] == "invalid_request"


@pytest.mark.parametrize("args", [
    [],                                                  # neither a position nor ids
    ["--after", "0", "--ids", "msg_" + "1" * 16],        # both
    ["--after", "-1"],
    ["--after", "0", "--limit", "0"],
    ["--after", "0", "--limit", "101"],
    ["--after", "0", "--max-bytes", "1023"],
    ["--after", "0", "--max-bytes", str(1024 * 1024 + 1)],
    ["--ids", "not-a-message-id"],
    ["--ids", "msg_" + "1" * 16 + ",msg_" + "1" * 16],    # the same id twice
    ["--ids", ",".join(f"msg_{number:016x}" for number in range(101))],
], ids=["neither", "both", "negative", "limit-0", "limit-101", "bytes-low", "bytes-high", "bad-id", "repeat",
        "too-many-ids"])
def test_a_read_outside_its_bounds_is_refused_before_anything_is_read(tmp_path, capsys, args) -> None:
    with make_world(tmp_path) as world:
        code, out = _read(world, capsys, *args)
        assert (code, out.get("error")) == (1, "invalid_request")


def test_the_other_secretary_actions_still_require_their_state(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        code, page = _read(world, capsys, "--after", "0")
        assert (code, page.get("messages")) == (0, [])
        for action in (["scan"], ["replay"], ["show"], ["decide", "--budget-seconds", "5"]):
            with pytest.raises(SystemExit) as absent:
                cli.main(["--config", str(world.config_path), "secretary", "--policy", str(world.policy_path),
                          *action])
            assert absent.value.code == 2
