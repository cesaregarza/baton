"""One callback's decision: what is new since the last delivered brief, and whether it wakes the principal."""

from __future__ import annotations

import hashlib
import json

import pytest
from secretary_support import conversation, make_world, policy, say, table_hashes

from baton import cli, conversations, jobs, messages, secretary, secretary_decide, secretary_store
from baton.config import ConfigError
from baton.errors import InvalidRequest

SECRET = "the secret phrase"


def _catch_up(world) -> None:
    secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)


def _decide(world, **options) -> dict:
    return secretary_decide.decide(world.config, world.state_path, world.policy_path,
                                   budget_seconds=options.pop("budget_seconds", 10), **options)


def _after(result: dict, **options) -> dict:
    """The baseline a caller passes once a brief was delivered or nothing was: the same floor and since."""
    return {"since": result["since"], "floor": result["floor"], **options}


def _direct(world, text: str) -> tuple[dict, str]:
    sent = messages.send(world.baton, sender="worker", recipient="planner", text=text)
    return sent, f"message:{sent['seq']}"


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_decide_refuses_a_state_without_the_catch_up_scan(tmp_path) -> None:
    with make_world(tmp_path) as world:
        with pytest.raises(ConfigError, match="catch-up"):
            _decide(world)
        assert not world.state_path.exists()


def test_new_owed_message_wakes_with_a_bounded_brief_and_no_message_text(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        sent, trigger = _direct(world, f"Please decide {SECRET}")
        result = _decide(world, trigger=trigger)
        assert result["principal"] == "planner" and result["mode"] == "callback"
        assert (result["decision"], result["rule"]) == ("wake", "wake:brief")
        assert result["evidence"] == f"message:{sent['message_id']}"
        assert result["coverage"] == "current" and result["covers_trigger"] is True
        brief = result["brief"]
        assert brief.split("\n")[2] == (f"New since position {result['since']}: 1 needing you, 0 can wait, 0 kept"
                                        " without waking. Open: 1 owed by you, 0 awaited by you.")
        assert brief.startswith("\n" + secretary_decide.BRIEF_START + "\n")
        assert brief.endswith("\n" + secretary_decide.BRIEF_END)
        assert len(brief.encode()) <= 2048
        assert sent["message_id"] in brief and SECRET not in brief
        assert [(item["source_key"], item["decision"], item["rule"]) for item in result["new"]] == [
            (f"message:{sent['message_id']}", "wake", "wake:brief")]


def test_acknowledgment_is_held_and_listed_in_the_hourly_digest(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _catch_up(world)
        ask = say(world, room, "planner", "Please check this", attention=["worker"])
        ack = say(world, room, "worker", "received", reply_to=ask["message_id"])
        result = _decide(world, trigger=f"message:{ack['seqs']['planner']}")
        assert (result["decision"], result["rule"], result["brief"]) == ("hold", "hold:digest", "")
        assert result["evidence"] == f"message:{ack['message_id']}"
        digest = _decide(world, **_after(result, hourly=True))
        assert digest["mode"] == "hourly" and digest["decision"] is None
        assert digest["brief"].startswith("\n" + secretary_decide.DIGEST_START + "\n")
        held = [line for line in _lines(digest["brief"]) if ack["message_id"] in line]
        assert len(held) == 1 and held[0].startswith("- hold")
        assert digest["brief"].split("\n")[2] == (
            f"New since position {result['since']}: 0 needing you, 1 can wait, 1 kept without waking."
            " Open: 0 owed by you, 1 awaited by you, 0 opened before activation (counted only).")
        assert "your helper's read still returns every message, and nothing here marks one delivered" in digest["brief"]


def test_routine_post_is_kept_and_only_counted(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _catch_up(world)
        post = say(world, room, "worker", "Progress update")
        result = _decide(world, trigger=f"message:{post['seqs']['planner']}")
        assert (result["decision"], result["rule"], result["brief"]) == ("keep", "keep:retain", "")
        digest = _decide(world, **_after(result, hourly=True))
        assert post["message_id"] not in digest["brief"] and not _lines(digest["brief"])
        assert "1 kept without waking" in digest["brief"]


def test_items_before_activation_never_wake_and_are_only_counted(tmp_path) -> None:
    with make_world(tmp_path) as world:
        before, _ = _direct(world, "Please decide before activation")
        _catch_up(world)
        first = _decide(world)
        assert first["since"] == first["floor"] == first["through_pos"]
        assert (first["decision"], first["rule"], first["new"]) == ("keep", "keep:nothing-new", [])
        after, trigger = _direct(world, "Please decide after activation")
        result = _decide(world, **_after(first, trigger=trigger))
        assert result["decision"] == "wake" and result["evidence"] == f"message:{after['message_id']}"
        assert before["message_id"] not in result["brief"]
        digest = _decide(world, **_after(first, hourly=True))
        assert before["message_id"] not in digest["brief"]
        assert "1 opened before activation" in digest["brief"]


def test_job_events_never_wake_unless_the_policy_key_allows(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        made = jobs.create(world.baton, world.config.jobs, request_id="job-wake", actor="worker", title="Task")
        trigger = f"job:{made['job_id']}:1"
        held = _decide(world, trigger=trigger)
        assert (held["decision"], held["rule"]) == ("hold", "hold:job-brief")
        assert held["covers_trigger"] is True and held["brief"] == ""
        policy(world, job_events_wake=True)
        woken = _decide(world, **_after(held, trigger=trigger))
        assert (woken["decision"], woken["rule"]) == ("wake", "wake:job-brief")
        assert made["job_id"] in woken["brief"]


def test_a_job_wake_never_wakes_for_a_message_on_its_own(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        sent, _ = _direct(world, "Please decide")
        made = jobs.create(world.baton, world.config.jobs, request_id="job-only", actor="worker", title="Task")
        result = _decide(world, trigger=f"job:{made['job_id']}:1")
        assert (result["decision"], result["rule"]) == ("hold", "hold:non-message-trigger")
        assert result["evidence"] == f"message:{sent['message_id']}" and result["brief"] == ""
        again = _decide(world, **_after(result, trigger="conversation"))
        assert again["decision"] == "hold"


def test_behind_scan_names_its_position_and_whether_it_covers_the_trigger(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _catch_up(world)
        first, first_trigger = _direct(world, "Please decide one")
        for index in range(4):
            say(world, room, "worker", f"Filler {index}")
        last, last_trigger = _direct(world, "Please decide two")
        monkeypatch.setattr(secretary_decide, "BATCH", 2)
        behind = _decide(world, trigger=first_trigger, budget_seconds=0.001)
        assert behind["coverage"] == "behind" and behind["covers_trigger"] is True
        assert behind["through_pos"] < behind["head"]
        assert behind["decision"] == "wake" and f"behind, Baton head {behind['head']}" in behind["brief"]
        short = _decide(world, **_after(behind, trigger=last_trigger, budget_seconds=0.001))
        assert short["coverage"] == "behind" and short["covers_trigger"] is False
        caught_up = _decide(world, **_after(behind, trigger=last_trigger))
        assert caught_up["coverage"] == "current" and caught_up["covers_trigger"] is True
        assert last["message_id"] in caught_up["brief"] and first["message_id"] in caught_up["brief"]


def test_uncertain_item_is_held_and_tagged_with_its_evidence_id(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _catch_up(world)
        say(world, room, "planner", "Please check this", attention=["worker"])
        say(world, room, "planner", "Please check that", attention=["worker"])
        ambiguous = say(world, room, "worker", "received")
        result = _decide(world, trigger=f"message:{ambiguous['seqs']['planner']}")
        assert (result["decision"], result["rule"]) == ("hold", "hold:reconcile")
        digest = _decide(world, **_after(result, hourly=True))
        line = next(line for line in _lines(digest["brief"]) if ambiguous["message_id"] in line)
        assert line.startswith(f"- hold, uncertain, evidence message:{ambiguous['message_id']}:")


def test_brief_bound_drops_whole_items_from_the_end_awaited_first(tmp_path) -> None:
    with make_world(tmp_path) as world:
        policy(world, brief_bytes=512)
        room = conversation(world)
        _catch_up(world)
        asks = [say(world, room, "planner", f"Please check {index}", attention=["worker"]) for index in range(2)]
        answers = [say(world, room, "worker", "PASS", reply_to=ask["message_id"]) for ask in asks]
        owed = [_direct(world, f"Please decide {index}")[0] for index in range(5)]
        result = _decide(world, trigger=f"message:{answers[0]['seqs']['planner']}")
        brief = result["brief"]
        assert result["decision"] == "wake" and len(brief.encode()) <= 512
        shown = _lines(brief)
        assert 0 < len(shown) < len(answers) + len(owed)
        assert all(any(sent["message_id"] in line for sent in owed) for line in shown)
        assert f"{len(answers) + len(owed) - len(shown)} more not shown" in brief
        assert all(line.endswith("(principal owes a response)") for line in shown)


def test_digest_never_cites_a_source_the_principal_can_no_longer_see(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world, ["planner", "worker", "reviewer"])
        _catch_up(world)
        sent = say(world, room, "worker", "Please look", attention=["planner"])
        result = _decide(world, trigger=f"message:{sent['seqs']['planner']}")
        assert result["decision"] == "wake" and sent["message_id"] in result["brief"]
        conversations.remove(world.baton, world.config, room, "planner", actor="planner")
        digest = _decide(world, **_after(result, hourly=True))
        assert sent["message_id"] not in digest["brief"] and room not in digest["brief"]
        assert "1 no longer visible" in digest["brief"]


def test_each_decision_is_recorded_with_the_kernel_state(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        sent, trigger = _direct(world, f"Please decide {SECRET}")
        result = _decide(world, trigger=trigger)
        digest = _decide(world, **_after(result, hourly=True))
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            rows = [dict(row) for row in state.execute(
                "SELECT * FROM decisions WHERE principal='planner' ORDER BY seq")]
        finally:
            state.close()
        assert [row["seq"] for row in rows] == [result["decision_seq"], digest["decision_seq"]]
        assert [(row["mode"], row["decision"], row["trigger"]) for row in rows] == [
            ("callback", "wake", trigger), ("hourly", None, None)]
        assert rows[0]["since_pos"] == result["since"] and rows[0]["through_pos"] == result["through_pos"]
        assert rows[0]["brief_sha256"] == hashlib.sha256(result["brief"].encode()).hexdigest()
        assert SECRET not in json.dumps(rows)


def test_decide_leaves_every_baton_table_unchanged(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        _, trigger = _direct(world, "Please decide")
        before = table_hashes(world.baton)
        result = _decide(world, trigger=trigger)
        _decide(world, **_after(result, hourly=True))
        assert table_hashes(world.baton) == before


def test_policy_accepts_the_two_decide_keys_and_refuses_bad_values(tmp_path) -> None:
    with make_world(tmp_path) as world:
        loaded = secretary.load_policy(world.config, world.baton, world.policy_path)
        assert (loaded.job_events_wake, loaded.brief_bytes) == (False, 2048)
        policy(world, job_events_wake=True, brief_bytes=4096)
        loaded = secretary.load_policy(world.config, world.baton, world.policy_path)
        assert (loaded.job_events_wake, loaded.brief_bytes) == (True, 4096)
        for bad in ({"job_events_wake": "yes"}, {"brief_bytes": 511}, {"brief_bytes": 16385},
                    {"brief_bytes": True}):
            policy(world, **{"job_events_wake": False, "brief_bytes": 2048, **bad})
            with pytest.raises(ConfigError):
                secretary.load_policy(world.config, world.baton, world.policy_path)


def test_arguments_are_checked_before_anything_is_scanned(tmp_path) -> None:
    with make_world(tmp_path) as world:
        caught_up = secretary.scan(world.config, world.state_path, world.policy_path)["through_pos"]
        _direct(world, "Please decide")
        for options in ({"trigger": "message:0"}, {"trigger": "job:job_1:1"}, {"since": 3},
                        {"since": 2, "floor": 3}, {"budget_seconds": 0}, {"hourly": True, "trigger": "conversation"}):
            with pytest.raises(InvalidRequest):
                _decide(world, **options)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            assert state.execute("SELECT through_pos FROM sources WHERE principal='planner'").fetchone()[0] == caught_up
        finally:
            state.close()


def test_cli_subcommand_and_module_entry_print_the_same_decision(tmp_path, capsys) -> None:
    with make_world(tmp_path) as world:
        paths = ["--config", str(world.config_path), "--state", str(world.state_path), "--policy",
                 str(world.policy_path)]
        assert cli.main([paths[0], paths[1], "secretary", *paths[2:], "scan"]) == 0
        capsys.readouterr()
        _, trigger = _direct(world, "Please decide")
        options = ["--budget-seconds", "5", "--since", "0", "--floor", "0", "--trigger", trigger]
        assert cli.main([paths[0], paths[1], "secretary", *paths[2:], "decide", *options]) == 0
        from_cli = json.loads(capsys.readouterr().out)
        assert secretary_decide.main([*paths, *options]) == 0
        from_module = json.loads(capsys.readouterr().out)
        assert from_cli["decision"] == from_module["decision"] == "wake"
        assert from_module["decision_seq"] == from_cli["decision_seq"] + 1
        assert secretary_decide.main([*paths, "--budget-seconds", "5", "--trigger", "bogus"]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "invalid_request"
        policy(world, version=999)
        assert secretary_decide.main([*paths, "--budget-seconds", "5"]) == 2
        assert json.loads(capsys.readouterr().out)["error"] == "config"
