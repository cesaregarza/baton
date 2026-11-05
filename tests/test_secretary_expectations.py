"""Outstanding results, conservative correlation, replay, isolation, and bounded briefs."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from secretary_support import conversation, make_world, policy, say

from baton import cli, conversations, jobs, messages
from baton.agents.secretary import sources
from baton.agents.secretary.legacy import expectations
from baton.agents.secretary.legacy import store as secretary_store
from baton.agents.secretary.legacy import tracking as secretary
from baton.app import Tools
from baton.config import JobState, Role
from baton.errors import InvalidRequest

REV1 = "abcdef0111111111"
REV2 = "abcdef0222222222"
REV3 = "abcdef0333333333"
LOW_REV = "0000000abcdef012"


def _same_next_actor_state(world) -> None:
    states = dict(world.config.jobs.states)
    states["working"] = replace(states["working"], next=states["working"].next | {"working2"})
    states["working2"] = JobState("working2", "worker", frozenset({"handoff", "done"}))
    world.config = replace(world.config, jobs=replace(world.config.jobs, states=states))


def _awaited(world, room: str, revision: str = REV2) -> tuple[str, str]:
    ask = say(world, room, "planner", f"Please test repo#7@{revision}", attention=["worker"])
    secretary.scan(world.config, world.state_path, world.policy_path)
    shown = secretary.show(world.config, world.state_path, world.policy_path)
    expected = next(row for row in shown["expectations"] if row["direction"] == "awaited")
    return ask["message_id"], expected["id"]


def _expectation(world, expectation_id: str) -> dict:
    return next(row for row in secretary.show(world.config, world.state_path, world.policy_path)["expectations"]
                if row["id"] == expectation_id)


def _evidence(world, expectation_id: str) -> list[dict]:
    return [row for row in secretary.show(world.config, world.state_path, world.policy_path)["evidence"]
            if row["expectation_id"] == expectation_id]


@pytest.mark.parametrize("with_reply", [False, True])
def test_acknowledgments_never_close_an_awaited_result(tmp_path, with_reply) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        ask, expected_id = _awaited(world, room)
        say(world, room, "worker", "claimed, starting", **({"reply_to": ask} if with_reply else {}))
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "open"
        assert any(row["kind"] == "ack" for row in _evidence(world, expected_id))


def test_artifactless_untagged_ack_needs_one_matching_expectation(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        say(world, room, "planner", "Please check this", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        first = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "awaited")
        single = say(world, room, "worker", "claimed, starting")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, first["id"])["state"] == "open"
        assert any(row["kind"] == "ack" and row["source_key"] == f"message:{single['message_id']}"
                   for row in _evidence(world, first["id"]))
        say(world, room, "planner", "Please check another thing", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        ambiguous = say(world, room, "worker", "received")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        candidates = [row for row in shown["expectations"] if row["direction"] == "awaited"]
        assert len(candidates) == 2 and all(row["state"] == "open" for row in candidates)
        assert any(row["kind"] == "reconcile" and row["source_key"] == f"message:{ambiguous['message_id']}"
                   for row in shown["dispositions"])
        assert not any(row["kind"] == "ack" and row["source_key"] == f"message:{ambiguous['message_id']}"
                       for row in shown["evidence"])
        assert {row["expectation_id"] for row in shown["evidence"]
                if row["kind"] == "uncertain" and row["source_key"] == f"message:{ambiguous['message_id']}"} == {
                    row["id"] for row in candidates}


def test_other_artifact_and_wrong_reply_do_not_satisfy(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        say(world, room, "worker", f"PASS other#9@{REV2}")
        other = say(world, room, "planner", "Different question", attention=["reviewer"])
        say(world, room, "worker", f"PASS repo#7@{REV2}", reply_to=other["message_id"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "open"
        assert not any(row["kind"] == "satisfied" for row in _evidence(world, expected_id))


def test_stale_revision_is_evidence_and_untagged_current_answer_satisfies(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        say(world, room, "worker", f"PASS repo#7@{REV1}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "open"
        assert any(row["kind"] == "stale" for row in _evidence(world, expected_id))
        answer = say(world, room, "worker", f"PASS repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "satisfied"
        assert any(row["kind"] == "satisfied" and row["source_key"] == f"message:{answer['message_id']}"
                   and row["rule_id"] == "answer:pass" for row in _evidence(world, expected_id))


@pytest.mark.parametrize("text", [f"Still checking repo#7@{REV2}", f"PASS FAIL repo#7@{REV2}"])
def test_uncertain_or_conflicting_words_stay_open(tmp_path, text) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        say(world, room, "worker", text)
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "open"
        assert any(row["kind"] == "uncertain" for row in _evidence(world, expected_id))
        assert any(row["kind"] == "reconcile" for row in secretary.show(
            world.config, world.state_path, world.policy_path)["dispositions"])
        assert _expectation(world, expected_id)["state"] == "open"


def test_explicit_supersession_and_operator_and_terminal_cancellation(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, first = _awaited(world, room, REV1)
        say(world, room, "planner", f"Please test repo#7@{REV2}; supersedes repo#7@{REV1}",
            attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, first)["state"] == "superseded"
        newer = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)["expectations"]
                     if row["state"] == "open" and row["direction"] == "awaited")
        assert newer["artifact_rev"] == REV2
        assert _evidence(world, first)[-1]["kind"] == "superseded"
        secretary.cancel(world.config, world.state_path, world.policy_path, newer["id"], "withdrawn")
        assert _expectation(world, newer["id"])["state"] == "cancelled"
        assert _evidence(world, newer["id"])[-1]["rule_id"] == "operator-cancel"
        made = jobs.create(world.baton, world.config.jobs, request_id="terminal-job", actor="planner", title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        job_expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and row["state"] == "open")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "done", actor="planner")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, job_expected["id"])["state"] == "cancelled"
        assert any(row["rule_id"] == "terminal-job-state" for row in _evidence(world, job_expected["id"]))


def test_direct_message_answer_uses_unique_pair_and_existing_result_checks(tmp_path) -> None:
    with make_world(tmp_path) as world:
        first = messages.send(world.baton, sender="worker", recipient="planner", text="Please approve")
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "owed")
        assert expected["opening_source"] == f"message:{first['message_id']}"
        ack = messages.send(world.baton, sender="planner", recipient="worker", text="received, starting")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        assert any(row["kind"] == "ack" and row["source_key"] == f"message:{ack['message_id']}"
                   for row in _evidence(world, expected["id"]))
        unknown = messages.send(world.baton, sender="planner", recipient="worker", text="I'll look at it")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        assert any(row["kind"] == "uncertain" and row["source_key"] == f"message:{unknown['message_id']}"
                   for row in _evidence(world, expected["id"]))
        answer = messages.send(world.baton, sender="planner", recipient="worker", text="APPROVED, ship it")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "satisfied"
        assert any(row["kind"] == "satisfied" and row["source_key"] == f"message:{answer['message_id']}"
                   for row in _evidence(world, expected["id"]))


def test_two_direct_requests_from_same_pair_leave_both_open_on_later_answer(tmp_path) -> None:
    with make_world(tmp_path) as world:
        for text in ("Please approve the first", "Please approve the second"):
            messages.send(world.baton, sender="worker", recipient="planner", text=text)
        secretary.scan(world.config, world.state_path, world.policy_path)
        answer = messages.send(world.baton, sender="planner", recipient="worker", text="APPROVED")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        owed = [row for row in shown["expectations"] if row["direction"] == "owed"]
        assert len(owed) == 2 and all(row["state"] == "open" for row in owed)
        key = f"message:{answer['message_id']}"
        assert any(row["source_key"] == key and row["kind"] == "reconcile" for row in shown["dispositions"])
        assert {row["expectation_id"] for row in shown["evidence"] if row["source_key"] == key
                and row["kind"] == "uncertain"} == {row["id"] for row in owed}


def test_unique_job_linked_direct_pair_keeps_the_task_reference(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="direct-job", actor="outsider", title="Task")
        messages.send(world.baton, sender="worker", recipient="planner", job_id=made["job_id"],
                      text="Please approve")
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "owed")
        assert expected["task_ref"] == f"job:{made['job_id']}"
        unrelated = messages.send(world.baton, sender="planner", recipient="worker", text="APPROVED")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        assert not any(row["source_key"] == f"message:{unrelated['message_id']}" and row["kind"] == "satisfied"
                       for row in _evidence(world, expected["id"]))
        answer = messages.send(world.baton, sender="planner", recipient="worker", job_id=made["job_id"],
                               text="APPROVED")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "satisfied"
        assert any(row["source_key"] == f"message:{answer['message_id']}" and row["kind"] == "satisfied"
                   for row in _evidence(world, expected["id"]))


def test_incoming_direct_job_message_opens_owed_even_when_it_matches_awaited(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="direct-awaited", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        awaited = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and row["direction"] == "awaited")
        incoming = messages.send(world.baton, sender="worker", recipient="planner", job_id=made["job_id"],
                                 text="Please approve the plan")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert _expectation(world, awaited["id"])["state"] == "open"
        assert any(row["source_key"] == f"message:{incoming['message_id']}" and row["kind"] == "uncertain"
                   for row in _evidence(world, awaited["id"]))
        assert any(row["opening_source"] == f"message:{incoming['message_id']}" and row["direction"] == "owed"
                   and row["requester"] == "worker" and row["state"] == "open"
                   for row in shown["expectations"])


def test_unique_direct_pair_with_unrelated_artifact_records_uncertainty(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Please approve")
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "owed")
        answer = messages.send(world.baton, sender="planner", recipient="worker",
                               text=f"APPROVED repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        key = f"message:{answer['message_id']}"
        assert _expectation(world, expected["id"])["state"] == "open"
        assert any(row["source_key"] == key and row["kind"] == "uncertain"
                   for row in _evidence(world, expected["id"]))
        assert any(row["source_key"] == key and row["kind"] == "reconcile"
                   for row in shown["dispositions"])


def test_unique_direct_pair_still_requires_result_kind_artifact_and_recipient(tmp_path) -> None:
    with make_world(tmp_path) as world:
        messages.send(world.baton, sender="worker", recipient="planner",
                      text=f"Please review repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "owed")
        assert expected["expected_kind"] == "review" and expected["artifact_rev"] == REV2
        wrong_kind = messages.send(world.baton, sender="planner", recipient="worker",
                                   text=f"CI PASS repo#7@{REV2}")
        stale = messages.send(world.baton, sender="planner", recipient="worker",
                              text=f"review APPROVED repo#7@{REV1}")
        unrelated = messages.send(world.baton, sender="planner", recipient="worker",
                                  text=f"review APPROVED other#9@{REV2}")
        wrong_recipient = messages.send(world.baton, sender="planner", recipient="outsider",
                                        text=f"review APPROVED repo#7@{REV2}")
        missing_revision = messages.send(world.baton, sender="planner", recipient="worker",
                                         text="review APPROVED")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        observed = {row["source_key"]: row["kind"] for row in _evidence(world, expected["id"])}
        assert observed[f"message:{wrong_kind['message_id']}"] == "kind-mismatch"
        assert observed[f"message:{stale['message_id']}"] == "stale"
        assert observed.get(f"message:{unrelated['message_id']}") == "uncertain"
        assert f"message:{wrong_recipient['message_id']}" not in observed
        assert observed[f"message:{missing_revision['message_id']}"] == "uncertain"
        good = messages.send(world.baton, sender="planner", recipient="worker",
                             text=f"review APPROVED repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "satisfied"
        assert any(row["kind"] == "satisfied" and row["source_key"] == f"message:{good['message_id']}"
                   for row in _evidence(world, expected["id"]))


def test_evidence_bound_reserves_a_closure_row(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        ask, expected_id = _awaited(world, room)
        monkeypatch.setattr(expectations, "MAX_EVIDENCE", 2)
        say(world, room, "worker", "claimed", reply_to=ask)
        say(world, room, "worker", "starting", reply_to=ask)
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert len(_evidence(world, expected_id)) == 1
        result = secretary.cancel(world.config, world.state_path, world.policy_path, expected_id, "withdrawn")
        assert result["created"] is True
        assert _expectation(world, expected_id)["state"] == "cancelled"
        assert len(_evidence(world, expected_id)) == 2


def test_cancel_refuses_a_legacy_full_evidence_record(tmp_path, monkeypatch, capsys) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        monkeypatch.setattr(expectations, "MAX_EVIDENCE", 1)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            secretary_store.add_evidence(state, "planner", expected_id, "fixture:full", "ack", "fixture",
                                         (None, None))
        finally:
            state.close()
        with pytest.raises(InvalidRequest, match="evidence limit"):
            secretary.cancel(world.config, world.state_path, world.policy_path, expected_id, "withdrawn")
        args = ["--config", str(world.config_path), "secretary", "--state", str(world.state_path),
                "--policy", str(world.policy_path)]
        assert cli.main([*args, "cancel", expected_id, "--reason", "withdrawn"]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "invalid_request"
        assert _expectation(world, expected_id)["state"] == "open"


def test_replay_changed_policy_reports_differences_without_writing(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        say(world, room, "worker", f"READY repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        recorded = secretary.replay(world.config, world.state_path, world.policy_path)
        assert recorded["differences"] == []
        assert recorded["stored_sha256"] == recorded["replayed_sha256"]
        before = recorded["stored_sha256"]
        policy(world, answer_words=["READY"])
        changed = secretary.replay(world.config, world.state_path, world.policy_path)
        assert changed["differences"]
        assert changed["stored_sha256"] == before
        assert _expectation(world, expected_id)["state"] == "open"


def test_replay_reports_lost_visibility_as_unavailable_without_rewriting(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        sent = say(world, room, "worker", "Please decide", attention=["planner"])
        scanned = secretary.scan(world.config, world.state_path, world.policy_path)
        conversations.remove(world.baton, world.config, room, "planner", actor="planner")
        try:
            replayed = secretary.replay(world.config, world.state_path, world.policy_path)
        except InvalidRequest as error:
            raise AssertionError("replay must report unavailable sources") from error
        assert replayed["stored_sha256"] == scanned["export_sha256"]
        assert replayed["replayed_sha256"] is None
        assert any(row["source_key"] == f"message:{sent['message_id']}" and
                   row["reason"] == "source no longer visible" for row in replayed["differences"])
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            assert secretary_store.export_digest(state, "planner") == scanned["export_sha256"]
        finally:
            state.close()


def test_restarts_and_mid_batch_failures_have_identical_export(tmp_path) -> None:
    with make_world(tmp_path) as world:
        for index in range(5):
            messages.send(world.baton, sender="worker", recipient="planner", text=f"Request {index}")
        baseline = world.state_path
        for _ in range(3):
            complete = secretary.scan(world.config, baseline, world.policy_path, limit=2)
        expected_hash = complete["export_sha256"]
        for cut in range(3):
            for stage in ("mid_batch", "before_commit"):
                path = baseline.with_name(f"interrupted-{cut}-{stage}.db")
                for _ in range(cut):
                    secretary.scan(world.config, path, world.policy_path, limit=2)
                before_failure = secretary_store.open_state(path, world.config.data_dir)
                saved_hash = secretary_store.export_digest(before_failure, "planner")
                before_failure.close()

                def fail(point, boundary=stage):
                    if point == boundary:
                        raise RuntimeError(boundary)

                with pytest.raises(RuntimeError, match=stage):
                    secretary.scan(world.config, path, world.policy_path, limit=2, inject=fail)
                after_failure = secretary_store.open_state(path, world.config.data_dir)
                assert secretary_store.export_digest(after_failure, "planner") == saved_hash
                after_failure.close()
                for _ in range(3 - cut):
                    resumed = secretary.scan(world.config, path, world.policy_path, limit=2)
                assert resumed["export_sha256"] == expected_hash


def test_two_principals_are_isolated_and_hidden_sources_are_not_cited(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        messages.send(world.baton, sender="worker", recipient="planner", text="Planner work")
        messages.send(world.baton, sender="worker", recipient="reviewer", text="Reviewer work")
        messages.send(world.baton, sender="reviewer", recipient="worker", text="Private reviewer words")
        hidden_room = conversation(world, ["planner", "worker", "reviewer", "outsider"])
        conversations.remove(world.baton, world.config, hidden_room, "planner", actor="planner")
        hidden = say(world, hidden_room, "worker", "Hidden room evidence")
        p1 = world.policy_path
        p2 = p1.with_name("reviewer-policy.json")
        p2.write_text(json.dumps({"version": 1, "principal": "reviewer",
                                  "secretary_alias_id": "al_0000000000000001"}))
        shared = world.state_path
        p1_hash = secretary.scan(world.config, shared, p1)["export_sha256"]
        other = jobs.create(world.baton, world.config.jobs, request_id="other-only-job", actor="worker",
                            title="Other team's job")
        jobs.move(world.baton, world.config.jobs, other["job_id"], "handoff", actor="worker")
        after_other = secretary.scan(world.config, shared, p1)
        assert after_other["events_seen"] == after_other["items_added"] == 0
        assert after_other["export_sha256"] == p1_hash
        state = secretary_store.open_state(shared, world.config.data_dir)
        try:
            assert state.execute("SELECT COUNT(*) FROM items WHERE principal='planner' AND"
                                 " json_extract(envelope, '$.job_id')=?", (other["job_id"],)).fetchone()[0] == 0
            assert state.execute("SELECT COUNT(*) FROM expectations WHERE principal='planner' AND task_ref=?",
                                 (f"job:{other['job_id']}",)).fetchone()[0] == 0
        finally:
            state.close()
        p2_hash = secretary.scan(world.config, shared, p2)["export_sha256"]
        alone1 = secretary.scan(world.config, shared.with_name("alone1.db"), p1)["export_sha256"]
        alone2 = secretary.scan(world.config, shared.with_name("alone2.db"), p2)["export_sha256"]
        assert (p1_hash, p2_hash) == (alone1, alone2)
        interleaved = shared.with_name("interleaved.db")
        for _ in range(100):
            p1_page = secretary.scan(world.config, interleaved, p1, limit=1)
            p2_page = secretary.scan(world.config, interleaved, p2, limit=1)
            if p1_page["events_seen"] == p2_page["events_seen"] == 0:
                break
        else:
            pytest.fail("interleaved scans did not reach the same source position")
        assert (p1_page["export_sha256"], p2_page["export_sha256"]) == (p1_hash, p2_hash)
        p1_sources = secretary.show(world.config, shared, p1)["items"]
        assert all("Reviewer work" not in str(item) and "Private reviewer words" not in str(item)
                   for item in p1_sources)
        assert all(item["envelope"]["source_key"] != f"message:{hidden['message_id']}" for item in p1_sources)


def test_terminal_job_outside_policy_never_opens_a_creator_action(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        states = dict(world.config.jobs.states)
        states["working"] = replace(states["working"], next=states["working"].next | {"failed"})
        states["failed"] = JobState("failed", None, frozenset())
        world.config = replace(world.config, jobs=replace(world.config.jobs, states=states))
        policy(world, terminal_job_states=["failed"])
        made = jobs.create(world.baton, world.config.jobs, request_id="terminal-subset", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        jobs.move(world.baton, world.config.jobs, made["job_id"], "done", actor="worker")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        rows = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"]
        assert not any(row["direction"] == "owed" and row["state"] == "open" for row in rows)
        assert not any(row["direction"] == "awaited" and row["state"] == "open" for row in rows)
        assert all(item["envelope"]["job"]["event_waiting_on"] is None for item in shown["items"]
                   if item["envelope"]["job_id"] == made["job_id"] and
                   item["envelope"]["job"]["event_state"] == "done")


def test_unlisted_terminal_move_supersedes_prior_owed_action_without_opening_another(tmp_path) -> None:
    with make_world(tmp_path) as world:
        states = dict(world.config.jobs.states)
        states["working"] = replace(states["working"], next=states["working"].next | {"failed"})
        states["failed"] = JobState("failed", None, frozenset())
        world.config = replace(world.config, jobs=replace(world.config.jobs, states=states))
        policy(world, terminal_job_states=["failed"])
        made = jobs.create(world.baton, world.config.jobs, request_id="unlisted-owed", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        before = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and row["direction"] == "owed")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "done", actor="worker")
        moved = world.baton.execute("SELECT pos FROM events WHERE job_id=? ORDER BY pos DESC LIMIT 1",
                                    (made["job_id"],)).fetchone()[0]
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        job_rows = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"]
        assert len(job_rows) == 1 and job_rows[0]["id"] == before["id"]
        assert job_rows[0]["state"] == "superseded"
        assert any(row["source_key"] == f"event:{moved}" and row["kind"] == "superseded"
                   and row["rule_id"] == "job-next-actor-changed" for row in shown["evidence"])


def test_attention_names_every_responder_without_silent_truncation(tmp_path) -> None:
    with make_world(tmp_path) as world:
        responders = [f"agent{index}" for index in range(10)]
        world.config = replace(world.config,
                               roles={**world.config.roles, **{name: Role(name) for name in responders}},
                               conversations=replace(world.config.conversations, max_participants=20))
        room = conversation(world, ["planner", *responders])
        say(world, room, "planner", "Please check this task", attention=responders)
        secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            actual = {row["responder"] for row in state.execute(
                "SELECT responder FROM expectations WHERE principal='planner' AND direction='awaited'")}
        finally:
            state.close()
        assert actual == set(responders)


def test_brief_groups_work_and_separates_claimed_relays_and_notices(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, expected_id = _awaited(world, room)
        say(world, room, "worker", f"received repo#7@{REV2}",
            relayed=[{"source": "upstream", "at": "2026-11-02T00:00:00Z",
                      "text": f"PASS repo#7@{REV2}"}])
        say(world, room, "worker", f"BLOCK repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected_id)["state"] == "satisfied"
        versions = [row["content"] for row in shown["briefs"] if row["work_key"] == "artifact:repo#7"]
        items = [item for version in versions for item in version["items"]]
        assert len(items) >= 2
        assert all(version["policy"]["digest"] and version["policy"]["version"] == 1 for version in versions)
        assert all(item["envelope_digest"] and "verified" not in item["label"] for item in items)
        assert any(item["blockers"] == ["BLOCK"] for item in items)
        assert any({"key": "repo#7", "rev": REV2} in item["artifacts"] for item in items)
        assert any(item["relayed_sha256"] for item in items)
        assert all(len(secretary_store.canonical(version).encode()) <= secretary.MAX_BRIEF_BYTES
                   and len(version["items"]) <= sources.MAX_CITATIONS for version in versions)
        relayed = next(item for item in shown["items"] if item["envelope"]["relayed_sha256"])
        assert relayed["excerpt"]["relayed"][0]["excerpt"].startswith("PASS")
        assert relayed["excerpt"]["authored_excerpt"].startswith("received")


def test_brief_versions_cite_only_changes_since_previous_version(tmp_path) -> None:
    with make_world(tmp_path) as world:
        world.config = replace(world.config, conversations=replace(
            world.config.conversations, max_messages_per_hour=100))
        room = conversation(world)
        sent = [say(world, room, "worker", f"Progress update {index}") for index in range(70)]
        secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            rows = state.execute("SELECT version, content FROM briefs WHERE principal='planner' AND work_key=?"
                                 " ORDER BY version", (f"conversation:{room}",)).fetchall()
            kinds = [row[0] for row in state.execute("SELECT kind FROM dispositions WHERE principal='planner'")]
        finally:
            state.close()
        assert len(rows) == 70 and [row["version"] for row in rows] == list(range(1, 71))
        assert [json.loads(row["content"])["items"][0]["source_key"] for row in rows] == [
            f"message:{item['message_id']}" for item in sent]
        assert all(len(json.loads(row["content"])["items"]) == 1 and
                   len(row["content"].encode()) <= secretary.MAX_BRIEF_BYTES for row in rows)
        assert "reconcile" not in kinds


def test_dispositions_follow_the_five_way_policy_mapping(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        asked = say(world, room, "planner", f"Please test repo#7@{REV2}", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        ack = say(world, room, "worker", "received", reply_to=asked["message_id"])
        answer = say(world, room, "worker", f"PASS repo#7@{REV2}")
        owed = say(world, room, "worker", "Please approve", attention=["planner"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        replied = say(world, room, "planner", "APPROVED", reply_to=owed["message_id"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            dispositions = {row["source_key"]: row["kind"] for row in state.execute(
                "SELECT source_key, kind FROM dispositions WHERE principal='planner'")}
        finally:
            state.close()
        assert dispositions[f"message:{asked['message_id']}"] == "retain"
        assert dispositions[f"message:{ack['message_id']}"] == "digest"
        assert dispositions[f"message:{answer['message_id']}"] == "brief"
        assert dispositions[f"message:{owed['message_id']}"] == "brief"
        assert dispositions[f"message:{replied['message_id']}"] == "retain"
        cancel_request = say(world, room, "worker", "Please decide another thing", attention=["planner"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        opened = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["opening_source"] == f"message:{cancel_request['message_id']}")
        cancelled = secretary.cancel(world.config, world.state_path, world.policy_path, opened["id"], "withdrawn")
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            assert state.execute("SELECT kind FROM dispositions WHERE principal='planner' AND source_key=?",
                                 (cancelled["source_key"],)).fetchone()[0] == "retain"
        finally:
            state.close()

    (tmp_path / "jobs").mkdir()
    with make_world(tmp_path / "jobs", initial_state="working") as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="q5-job", actor="planner", title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        jobs.move(world.baton, world.config.jobs, made["job_id"], "done", actor="worker")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        job_kinds = [row["kind"] for row in shown["dispositions"]]
        assert "route" in job_kinds
        assert "retain" in job_kinds
        assert "digest" not in job_kinds


def test_limit_outcome_is_recorded_and_the_request_is_not_dropped(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        monkeypatch.setattr(expectations, "MAX_OPEN", 1)
        messages.send(world.baton, sender="worker", recipient="planner", text="First request")
        messages.send(world.baton, sender="worker", recipient="planner", text="Second request")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert len(shown["items"]) == 2
        assert len(shown["expectations"]) == 1
        assert sorted((row["kind"], row["reason"]) for row in shown["dispositions"]) == [
            ("brief", "principal owes a response"),
            ("brief", "principal owes a response; not tracked: open expectation limit reached")]


def test_sixty_five_artifact_tokens_record_a_bounded_reconcile(tmp_path) -> None:
    with make_world(tmp_path) as world:
        tokens = " ".join(f"repo#{index}@{REV2}" for index in range(65))
        sent = messages.send(world.baton, sender="worker", recipient="planner", text=tokens)
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        key = f"message:{sent['message_id']}"
        item = next(row["envelope"] for row in shown["items"] if row["envelope"]["source_key"] == key)
        assert item["artifact_over_limit"] is True and len(item["artifacts"]) == sources.MAX_CITATIONS
        assert any(row["source_key"] == key and row["kind"] == "reconcile" and "bounded" in row["reason"]
                   for row in shown["dispositions"])
        assert not any(row["opening_source"] == key for row in shown["expectations"])


def test_brief_byte_bound_records_reconcile_without_oversized_brief(tmp_path) -> None:
    with make_world(tmp_path) as world:
        first = messages.send(world.baton, sender="worker", recipient="planner", text="Please approve")
        long_artifact = f"{'x' * secretary.MAX_BRIEF_BYTES}#7@{REV2}"
        large = messages.send(world.baton, sender="worker", recipient="planner",
                              text=f"Please approve {long_artifact}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        by_source = {row["source_key"]: row for row in shown["dispositions"]}
        assert by_source[f"message:{first['message_id']}"]["brief_work"] is not None
        oversized = by_source[f"message:{large['message_id']}"]
        assert oversized["kind"] == "reconcile" and oversized["reason"].startswith("brief bound reached")
        assert oversized["brief_work"] is None and oversized["brief_version"] is None
        assert all(len(secretary_store.canonical(row["content"]).encode("utf-8")) <= secretary.MAX_BRIEF_BYTES
                   for row in shown["briefs"])
        assert not any(item["source_key"] == f"message:{large['message_id']}"
                       for row in shown["briefs"] for item in row["content"]["items"])


def test_send_retry_conversation_copies_and_rescan_deduplicate(tmp_path) -> None:
    with make_world(tmp_path) as world:
        direct = messages.send(world.baton, sender="worker", recipient="planner", text="Please respond")
        retry = messages.send(world.baton, sender="worker", recipient="planner", text="Please respond",
                              request_id=direct["request_id"])
        assert retry["message_id"] == direct["message_id"] and retry["created"] is False
        room = conversation(world)
        sent = say(world, room, "planner", f"Please check repo#7@{REV2}", attention=["worker"])
        count = world.baton.execute("SELECT COUNT(*) FROM messages WHERE id=?", (sent["message_id"],)).fetchone()[0]
        assert count > 1
        first = secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
        second = secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
        assert second["items_added"] == 0 and second["export_sha256"] == first["export_sha256"]
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            state.execute("UPDATE sources SET through_pos=0 WHERE principal='planner'")
        finally:
            state.close()
        repeated = secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)
        assert repeated["events_seen"] > 0 and repeated["items_added"] == 0
        assert repeated["export_sha256"] == first["export_sha256"]
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            key = f"message:{sent['message_id']}"
            assert state.execute("SELECT COUNT(*) FROM items WHERE source_key=?", (key,)).fetchone()[0] == 1
            assert state.execute("SELECT COUNT(*) FROM dispositions WHERE source_key=?", (key,)).fetchone()[0] == 1
        finally:
            state.close()


def test_job_move_rechecks_one_open_expectation(tmp_path) -> None:
    with make_world(tmp_path) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="job-recheck", actor="planner", title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="planner")
        jobs.move(world.baton, world.config.jobs, made["job_id"], "review", actor="worker")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        open_jobs = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"
                     and row["state"] == "open"]
        assert len(open_jobs) == 1
        assert open_jobs[0]["direction"] == "owed"
        assert any(row["rule_id"] == "job-next-actor-changed" and row["kind"] == "superseded"
                   for row in shown["evidence"])


@pytest.mark.parametrize("initial_state", ["working", "open"])
def test_outgoing_job_created_or_moved_opens_awaited_expectation(tmp_path, initial_state) -> None:
    with make_world(tmp_path, initial_state=initial_state) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="outgoing-job", actor="planner", title="Task")
        if initial_state == "open":
            jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="planner")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        awaited = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"
                   and row["direction"] == "awaited" and row["responder"] == "worker"
                   and row["state"] == "open"]
        assert len(awaited) == 1
        assert awaited[0]["opening_source"].startswith("event:")
        assert not any(row["direction"] == "owed" and row["state"] == "open" and
                       row["task_ref"] == f"job:{made['job_id']}" for row in shown["expectations"])
        assert any(row["kind"] == "route" for row in shown["dispositions"])
        replayed = secretary.replay(world.config, world.state_path, world.policy_path)
        assert replayed["differences"] == []
        assert replayed["stored_sha256"] == replayed["replayed_sha256"]


def test_tracked_job_later_handoff_replaces_awaited_actor_with_cited_event(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="later-handoff", actor="planner", title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff", actor="worker")
        handoff = world.baton.execute("SELECT pos FROM events WHERE job_id=? ORDER BY pos DESC LIMIT 1",
                                      (made["job_id"],)).fetchone()[0]
        readable = Tools(world.config).read_job_events("planner", made["job_id"])["events"]
        assert any(row["actor"] == "worker" and row["audience"] == "reviewer"
                   and row["kind"] == "job.moved" for row in readable)
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        job_rows = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"
                    and row["direction"] == "awaited"]
        assert sorted((row["responder"], row["state"]) for row in job_rows) == [
            ("reviewer", "open"), ("worker", "superseded")
        ]
        assert any(row["opening_source"] == f"event:{handoff}" for row in job_rows)
        assert any(row["source_key"] == f"event:{handoff}" for row in shown["evidence"])
        assert any(row["source_key"] == f"event:{handoff}" and row["kind"] == "superseded"
                   and row["rule_id"] == "job-next-actor-changed" for row in shown["evidence"])
        assert any(row["envelope"]["source_key"] == f"event:{handoff}" for row in shown["items"])
        private = messages.send(world.baton, sender="worker", recipient="reviewer", job_id=made["job_id"],
                                text="Private job detail")
        result = secretary.scan(world.config, world.state_path, world.policy_path)
        assert result["events_seen"] == result["invisible"] == 1
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert all(row["envelope"]["message_id"] != private["message_id"] for row in shown["items"])
        replayed = secretary.replay(world.config, world.state_path, world.policy_path)
        assert replayed["differences"] == []
        assert replayed["stored_sha256"] == replayed["replayed_sha256"]


@pytest.mark.parametrize("direction", ["owed", "awaited"])
def test_same_next_actor_move_keeps_each_message_origin_expectation_open(tmp_path, direction) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        _same_next_actor_state(world)
        made = jobs.create(world.baton, world.config.jobs, request_id=f"same-actor-{direction}",
                           actor="planner", title="Task")
        if direction == "owed":
            message = messages.send(world.baton, sender="reviewer", recipient="planner",
                                    job_id=made["job_id"], text="Please approve the plan")
        else:
            room = conversation(world)
            message = say(world, room, "planner", "Please review the plan", job_id=made["job_id"],
                          attention=["reviewer"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["opening_source"] == f"message:{message['message_id']}")
        assert expected["direction"] == direction
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working2", actor="worker")
        moved = world.baton.execute("SELECT MAX(pos) FROM events WHERE job_id=?", (made["job_id"],)).fetchone()[0]
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        assert not any(row["source_key"] == f"event:{moved}" and row["expectation_id"] == expected["id"]
                       and row["kind"] == "superseded" for row in _evidence(world, expected["id"]))


def test_job_move_preserves_message_requests_and_only_supersedes_job_origin_on_actor_change(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        _same_next_actor_state(world)
        made = jobs.create(world.baton, world.config.jobs, request_id="same-next-actor", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        job_expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and
            row["opening_source"].startswith("event:") and row["direction"] == "awaited")
        incoming = messages.send(world.baton, sender="reviewer", recipient="planner", job_id=made["job_id"],
                                 text="Please approve the plan")
        room = conversation(world)
        outgoing = say(world, room, "planner", "Please review the plan", job_id=made["job_id"],
                       attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        owed = next(row for row in shown["expectations"]
                    if row["opening_source"] == f"message:{incoming['message_id']}")
        awaited = next(row for row in shown["expectations"]
                       if row["opening_source"] == f"message:{outgoing['message_id']}")
        assert owed["direction"] == "owed" and awaited["direction"] == "awaited"
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working2", actor="worker")
        same_pos = world.baton.execute("SELECT MAX(pos) FROM events WHERE job_id=?", (made["job_id"],)).fetchone()[0]
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert all(_expectation(world, row["id"])["state"] == "open" for row in (owed, awaited, job_expected))
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert not any(row["source_key"] == f"event:{same_pos}" and row["kind"] == "superseded"
                       for row in shown["evidence"])
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff", actor="worker")
        change_pos = world.baton.execute("SELECT MAX(pos) FROM events WHERE job_id=?", (made["job_id"],)).fetchone()[0]
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert all(_expectation(world, row["id"])["state"] == "open" for row in (owed, awaited))
        assert _expectation(world, job_expected["id"])["state"] == "superseded"
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert any(row["source_key"] == f"event:{change_pos}" and row["expectation_id"] == job_expected["id"]
                   and row["rule_id"] == "job-next-actor-changed" for row in shown["evidence"])
        assert not any(row["source_key"] == f"event:{change_pos}" and
                       row["expectation_id"] in {owed["id"], awaited["id"]} and
                       row["kind"] == "superseded" for row in shown["evidence"])


def test_same_actor_guard_avoids_false_reconcile_for_legacy_full_evidence(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        states = dict(world.config.jobs.states)
        states["handoff"] = replace(states["handoff"], next=states["handoff"].next | {"handoff2"})
        states["handoff2"] = JobState("handoff2", "reviewer", frozenset({"done"}))
        world.config = replace(world.config, jobs=replace(world.config.jobs, states=states))
        made = jobs.create(world.baton, world.config.jobs, request_id="legacy-full-job", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        worker = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and row["responder"] == "worker")
        # A valid sidecar from a version without closure reserve can contain a fully used evidence budget.
        monkeypatch.setattr(expectations, "MAX_EVIDENCE", 2)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            for index in range(2):
                secretary_store.add_evidence(state, "planner", worker["id"], f"fixture:full:{index}",
                                             "ack", "fixture", (None, None))
        finally:
            state.close()
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff", actor="worker")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, worker["id"])["state"] == "open"
        reviewer = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["task_ref"] == f"job:{made['job_id']}" and row["responder"] == "reviewer")
        assert reviewer["state"] == "open"
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff2", actor="reviewer")
        moved = world.baton.execute("SELECT MAX(pos) FROM events WHERE job_id=?", (made["job_id"],)).fetchone()[0]
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        assert _expectation(world, worker["id"])["state"] == "open"
        assert next(row for row in shown["dispositions"] if row["source_key"] == f"event:{moved}")[
            "kind"] == "route"


def test_fresh_scan_through_same_position_is_identical_after_job_handoff(tmp_path) -> None:
    with make_world(tmp_path, initial_state="working") as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="scan-timing", actor="planner", title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            before = [tuple(row) for row in state.execute(
                "SELECT source_key, envelope_digest, envelope FROM items WHERE principal='planner' ORDER BY seq")]
        finally:
            state.close()
        jobs.move(world.baton, world.config.jobs, made["job_id"], "handoff", actor="worker")
        resumed = secretary.scan(world.config, world.state_path, world.policy_path)
        fresh = secretary.scan(world.config, world.state_path.with_name("fresh-secretary.db"), world.policy_path)
        assert resumed["through_pos"] == fresh["through_pos"]
        assert resumed["export_sha256"] == fresh["export_sha256"]
        state = secretary_store.open_state(world.state_path, world.config.data_dir)
        try:
            after = [tuple(row) for row in state.execute(
                "SELECT source_key, envelope_digest, envelope FROM items WHERE principal='planner' ORDER BY seq"
                " LIMIT ?", (len(before),))]
        finally:
            state.close()
        assert after == before


def test_tracked_job_move_replaces_stale_owed_actor_even_when_another_role_moves_it(tmp_path) -> None:
    with make_world(tmp_path) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="external-handoff", actor="planner",
                           title="Task")
        secretary.scan(world.config, world.state_path, world.policy_path)
        jobs.move(world.baton, world.config.jobs, made["job_id"], "working", actor="worker")
        secretary.scan(world.config, world.state_path, world.policy_path)
        shown = secretary.show(world.config, world.state_path, world.policy_path)
        job_rows = [row for row in shown["expectations"] if row["task_ref"] == f"job:{made['job_id']}"]
        assert sorted((row["direction"], row["responder"], row["state"]) for row in job_rows) == [
            ("awaited", "worker", "open"), ("owed", "planner", "superseded")]
        assert any(row["rule_id"] == "job-next-actor-changed" for row in shown["evidence"])


def test_sha_supersession_requires_explicit_predecessor_even_when_sha_decreases(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, first = _awaited(world, room, REV1)
        say(world, room, "planner", f"Please test repo#7@{REV2}", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        second = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["artifact_rev"] == REV2)
        assert _expectation(world, first)["state"] == "open"
        say(world, room, "planner", f"Please test repo#7@{REV1}", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, second["id"])["state"] == "open"
        say(world, room, "planner", f"Please test repo#7@{LOW_REV}; supersedes repo#7@{REV2}",
            attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, second["id"])["state"] == "superseded"
        assert _expectation(world, first)["state"] == "open"


def test_explicit_predecessor_prefix_supersedes_one_matching_revision(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        _, first = _awaited(world, room, REV1)
        say(world, room, "planner", f"Please test repo#7@{REV2}; supersedes repo#7@{REV1[:12]}",
            attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, first)["state"] == "superseded"
        assert any(row["rule_id"] == "explicit-predecessor" for row in _evidence(world, first))


@pytest.mark.parametrize("kind,positive", [("review", "review APPROVED"),
                                           ("release", "release APPROVED")])
def test_ci_pass_does_not_satisfy_review_or_release_at_same_sha(tmp_path, kind, positive) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        say(world, room, "planner", f"Please {kind} repo#7@{REV2}", attention=["worker"])
        secretary.scan(world.config, world.state_path, world.policy_path)
        expected = next(row for row in secretary.show(world.config, world.state_path, world.policy_path)[
            "expectations"] if row["direction"] == "awaited")
        assert expected["expected_kind"] == kind
        say(world, room, "worker", f"CI PASS repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "open"
        assert any(row["kind"] == "kind-mismatch" and row["observed_kind"] == "check"
                   for row in _evidence(world, expected["id"]))
        say(world, room, "worker", f"{positive} repo#7@{REV2}")
        secretary.scan(world.config, world.state_path, world.policy_path)
        assert _expectation(world, expected["id"])["state"] == "satisfied"
