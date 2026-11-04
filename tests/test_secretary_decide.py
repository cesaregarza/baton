"""One callback's decision: what is new since the last delivered brief, and whether it wakes the principal."""

from __future__ import annotations

import hashlib
import json

import pytest
from secretary_support import conversation, make_world, policy, say, table_hashes

from baton import cli, conversations, events, jobs, messages, secretary, secretary_decide, secretary_store
from baton.config import ConfigError
from baton.errors import InvalidRequest
from baton.store import transaction

SECRET = "the secret phrase"
NEW_KIND = "synthetic.notice"  # a kind of delivery no secretary code names


def _catch_up(world) -> None:
    secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)


def _decide(world, **options) -> dict:
    return secretary_decide.decide(world.config, world.state_path, world.policy_path,
                                   budget_seconds=options.pop("budget_seconds", 10), **options)


def _after(result: dict, **options) -> dict:
    """The baseline a caller passes when nothing was delivered: the same floor and since."""
    return {"since": result["since"], "floor": result["floor"], **options}


def _delivered(result: dict, **options) -> dict:
    """The baseline a caller passes once the result's brief or digest was delivered: since moves to what it covered."""
    return {"since": result["covered_through"], "floor": result["floor"], **options}


def _direct(world, text: str) -> tuple[dict, str]:
    sent = messages.send(world.baton, sender="worker", recipient="planner", text=text)
    return sent, f"message:{sent['seq']}"


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def _last_pos(world) -> int:
    return world.baton.execute("SELECT MAX(pos) FROM events").fetchone()[0]


def _append(world, kind: str, *, job_id: str | None = None) -> int:
    """An event of a new kind delivered to the principal, appended as a future producer would."""
    with transaction(world.baton):
        events.append(world.baton, job_id=job_id, seq=0 if job_id is None else None, actor="worker",
                      audience="planner", kind=kind, summary=f"{kind} for planner", data={})
    return _last_pos(world)


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


def test_a_first_decision_killed_mid_scan_leaves_its_retry_the_same_activation_floor(tmp_path, monkeypatch) -> None:
    """The host's timeout can kill the first decide after a scan batch committed. Baton's retry must keep the floor
    where the catch-up stopped, or the owed message that batch scanned would count as before activation."""
    with make_world(tmp_path) as world:
        activation = secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)["through_pos"]
        sent, trigger = _direct(world, "Please decide")
        scan = secretary.scan

        def scan_then_killed(*args, **kwargs) -> None:
            scan(*args, **kwargs)  # the batch commits with its position
            raise TimeoutError("killed by the host's timeout")

        monkeypatch.setattr(secretary, "scan", scan_then_killed)
        with pytest.raises(TimeoutError):
            _decide(world, trigger=trigger)
        monkeypatch.undo()
        retry = _decide(world, trigger=trigger)
        assert retry["floor"] == retry["since"] == activation
        assert (retry["decision"], retry["evidence"]) == ("wake", f"message:{sent['message_id']}")
        digest = _decide(world, **_after(retry, hourly=True))
        assert sent["message_id"] in digest["brief"] and "0 opened before activation" in digest["brief"]


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


def test_a_new_kind_reaches_the_decision_as_a_delivery_and_never_wakes_by_its_kind(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        made = jobs.create(world.baton, world.config.jobs, request_id="job-kind", actor="worker", title="Task")
        created = _last_pos(world)
        loose = _append(world, NEW_KIND)  # neither a message nor a job id
        on_job = _append(world, "job.synthetic", job_id=made["job_id"])  # the job's second event
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="planner")
        moved = _last_pos(world)
        messages.send(world.baton, sender="planner", recipient="worker", text="Own words")
        own = _last_pos(world)
        _, trigger = _direct(world, "Please decide")
        result = _decide(world, trigger=trigger)
        new = {item["pos"]: item for item in result["new"]}
        assert {pos: item.get("delivered") for pos, item in new.items()} == {
            created: True, loose: True, on_job: True, moved: False, own: False, _last_pos(world): True}
        assert [new[loose][key] for key in ("kind", "message_id", "job_id", "decision")] == [NEW_KIND, None, None,
                                                                                               "keep"]
        assert [new[on_job][key] for key in ("kind", "job_id", "decision")] == ["job.synthetic", made["job_id"], "keep"]
        assert result["decision"] == "wake" and result["trigger_item"].get("delivered") is True
        own_wake = _decide(world, **_after(result, trigger=f"job:{made['job_id']}:2"))  # the new kind's own wake
        assert own_wake["trigger_item"]["source_key"] == f"event:{on_job}"
        assert own_wake["trigger_item"].get("delivered") is True and own_wake["decision"] == "hold"


def test_the_window_lists_what_is_outstanding_since_the_last_brief_however_little_this_call_scanned(
        tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        first = _decide(world)
        own = messages.send(world.baton, sender="planner", recipient="worker", text="Own words")
        sent = [_direct(world, "Please decide")[0] for _ in range(3)]
        trigger = f"message:{sent[0]['seq']}"
        _decide(world, **_after(first, trigger=trigger))
        retry = _decide(world, **_after(first, trigger=trigger))  # nothing left to scan, nothing delivered yet
        assert retry["new"] == []
        window = retry.get("window") or {}
        assert [item["source_key"] for item in window.get("deliveries", [])] == [
            f"message:{message['message_id']}" for message in sent]
        assert [item["source_key"] for item in window.get("context", [])] == [f"message:{own['message_id']}"]
        assert [item["delivered"] for item in window["deliveries"] + window["context"]] == [True, True, True, False]
        assert (window["more_deliveries"], window["more_context"]) == (0, 0)
        monkeypatch.setattr(secretary_decide, "LISTED", 2)
        bounded = _decide(world, **_after(first, trigger=trigger))["window"]
        assert [item["source_key"] for item in bounded["deliveries"]] == [
            item["source_key"] for item in window["deliveries"][:2]]
        assert (bounded["more_deliveries"], len(bounded["context"]), bounded["more_context"]) == (1, 1, 0)


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


def test_brief_bound_keeps_the_oldest_whole_items_and_lists_the_most_pressing_first(tmp_path) -> None:
    with make_world(tmp_path) as world:
        policy(world, brief_bytes=768)
        room = conversation(world)
        _catch_up(world)
        asks = [say(world, room, "planner", f"Please check {index}", attention=["worker"]) for index in range(2)]
        answers = [say(world, room, "worker", "PASS", reply_to=ask["message_id"]) for ask in asks]
        owed = [_direct(world, f"Please decide {index}")[0] for index in range(5)]
        result = _decide(world, trigger=f"message:{answers[0]['seqs']['planner']}")
        brief = result["brief"]
        assert result["decision"] == "wake" and len(brief.encode()) <= 768
        # In stream order: the two answers the principal awaited, then five messages it owes a response to.
        stream = [sent["message_id"] for sent in answers + owed]
        listed = [next(message_id for message_id in stream if message_id in line) for line in _lines(brief)]
        assert 2 < len(listed) < len(stream)
        assert sorted(listed, key=stream.index) == stream[:len(listed)]  # the oldest, as whole lines
        assert listed == stream[2:len(listed)] + stream[:2]  # what the principal owes before what it awaited
        assert f"{len(stream) - len(listed)} more not shown (bound 768 bytes)" in brief
        position = {item["message_id"]: item["pos"] for item in result["new"]}
        assert result.get("covered_through") == position[stream[len(listed)]] - 1  # just before the first left out


def test_what_a_bounded_brief_leaves_out_comes_in_the_next_digest_and_never_twice(tmp_path) -> None:
    """30 owed messages overflow the default 2,048-byte brief. Once it is delivered, the next digest lists exactly
    the ones it left out, and once that digest is delivered, none of them comes again."""
    with make_world(tmp_path) as world:
        _catch_up(world)
        sent = [_direct(world, f"Please decide {index}") for index in range(30)]
        owed = [message["message_id"] for message, _ in sent]
        brief = _decide(world, trigger=sent[0][1])
        shown = [message_id for message_id in owed if message_id in brief["brief"]]
        assert brief["decision"] == "wake" and len(brief["brief"].encode()) <= 2048
        assert 0 < len(shown) < len(owed) and shown == owed[:len(shown)]
        position = {item["message_id"]: item["pos"] for item in brief["new"]}
        assert brief.get("covered_through") == position[owed[len(shown)]] - 1
        digest = _decide(world, **_delivered(brief, hourly=True))
        assert [message_id for message_id in owed if message_id in digest["brief"]] == owed[len(shown):]
        after = _decide(world, **_delivered(digest, hourly=True))
        assert not any(message_id in after["brief"] for message_id in owed)


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
