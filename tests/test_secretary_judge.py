"""The judgment step: a judge command reads each new message for the principal, the word lists stay the fallback,
and the hard rules hold whatever the judge says. The judge here is a stand-in command that answers from a plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from secretary_support import conversation, make_world, policy, say

from baton import conversations, jobs, messages, secretary, secretary_decide, secretary_store
from baton.config import ConfigError

FAKE = Path(__file__).parent / "fixtures" / "secretary_fake_judge.py"
QUESTIONS = "0" * 64
SECRET = "the secret phrase"
NOTHING = {"relation": "none", "item": None, "opens_owed": 0.0, "needs_now": 0.0}


class Judge:
    """The policy's judge block, pointing at the stand-in command, and what that command was asked."""

    def __init__(self, world, tmp_path: Path, *steps: dict, timeout: float = 5.0) -> None:
        self.world, self.plan, self.log = world, tmp_path / "judge-plan.json", tmp_path / "judge-requests.jsonl"
        self.answer(*steps)
        self.block = {"command": [sys.executable, "-I", "-S", str(FAKE), str(self.plan), str(self.log)],
                      "timeout_seconds": timeout, "questions_sha256": QUESTIONS}
        self.configure()

    def answer(self, *steps: dict) -> None:
        self.plan.write_text(json.dumps(list(steps)))

    def configure(self, **changes: object) -> None:
        self.block.update(changes)
        policy(self.world, judge=self.block)

    @property
    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().split("\n") if line] if self.log.exists() else []


def _catch_up(world) -> None:
    secretary.scan(world.config, world.state_path, world.policy_path, limit=1000)


def _decide(world, **options) -> dict:
    return secretary_decide.decide(world.config, world.state_path, world.policy_path,
                                   budget_seconds=options.pop("budget_seconds", 30), **options)


def _after(result: dict, **options) -> dict:
    return {"since": result["since"], "floor": result["floor"], **options}


def _direct(world, text: str, sender: str = "worker", **kwargs) -> tuple[dict, str]:
    sent = messages.send(world.baton, sender=sender, recipient="planner", text=text, **kwargs)
    return sent, f"message:{sent['seq']}"


def _state(world, sql: str, *args: object) -> list[dict]:
    state = secretary_store.open_state(world.state_path, world.config.data_dir)
    try:
        return [dict(row) for row in state.execute(sql, args)]
    finally:
        state.close()


def _item(result: dict, sent: dict) -> dict:
    return next(item for item in result["new"] if item["message_id"] == sent["message_id"])


def _asked_of(world, source: dict) -> list[dict]:
    return _state(world, "SELECT direction, state FROM expectations WHERE opening_source=?",
                  f"message:{source['message_id']}")


def test_answers_closes_the_chosen_awaited_item_and_wakes(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path, {"when": "All done", "reply": {**NOTHING, "relation": "answers", "item": 0}})
        _catch_up(world)
        ask = say(world, room, "planner", "Please run the migration check", attention=["worker"])
        done = say(world, room, "worker", "All done on that", reply_to=ask["message_id"])
        result = _decide(world, trigger=f"message:{done['seqs']['planner']}")
        assert (result["decision"], result["rule"], result["evidence"]) == (
            "wake", "wake:brief", f"message:{done['message_id']}")
        # The word lists find no result word in the reply and would have held it as uncertain.
        assert _item(result, done)["judge"] == "judged"
        assert _item(result, done)["rules"] == {"decision": "hold", "rule": "hold:reconcile"}
        [request] = judge.requests
        expected = _state(world, "SELECT id FROM expectations WHERE direction='awaited'")[0]["id"]
        assert request["items"] == [{"id": expected, "direction": "awaited", "counterpart": "worker", "job_id": None,
                                     "artifact": None, "state": "open", "title": "Please run the migration check",
                                     "relayed": False}]
        message = request["message"]
        assert (message["id"], message["from"], message["channel"], message["reply_to"],
                message["replies_to_principal"], message["text"]) == (
            done["message_id"], "worker", "conversation", ask["message_id"], True, "All done on that")
        assert (request["principal"], request["questions_sha256"]) == ("planner", QUESTIONS)
        assert _asked_of(world, ask) == [{"direction": "awaited", "state": "satisfied"}]
        assert _state(world, "SELECT rule_id FROM expectation_evidence WHERE source_key=?",
                      f"message:{done['message_id']}") == [{"rule_id": "judge:answers"}]


def test_acknowledges_holds_where_the_word_lists_would_close_and_wake(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        Judge(world, tmp_path, {"when": "fail-first", "reply": {**NOTHING, "relation": "acknowledges", "item": 0}})
        _catch_up(world)
        ask = say(world, room, "planner", "Please fix the parser", attention=["worker"])
        note = say(world, room, "worker", "The fail-first case is written; the fix comes next",
                   reply_to=ask["message_id"])
        result = _decide(world, trigger=f"message:{note['seqs']['planner']}")
        assert (result["decision"], result["rule"]) == ("hold", "hold:digest")
        assert _item(result, note)["rules"] == {"decision": "wake", "rule": "wake:brief"}  # "fail" read as a verdict
        assert _asked_of(world, ask) == [{"direction": "awaited", "state": "open"}]


def test_changes_or_cancels_records_evidence_and_never_closes(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        Judge(world, tmp_path, {"when": "only the first", "reply": {**NOTHING, "relation": "changes_or_cancels",
                                                                     "item": 0}})
        _catch_up(world)
        ask = say(world, room, "planner", "Please look over the notes", attention=["worker"])
        change = say(world, room, "worker", "APPROVED in principle, but only the first section now",
                     reply_to=ask["message_id"])
        result = _decide(world, trigger=f"message:{change['seqs']['planner']}")
        assert (result["decision"], result["rule"]) == ("hold", "hold:digest")
        assert _item(result, change)["rules"] == {"decision": "wake", "rule": "wake:brief"}
        assert _asked_of(world, ask) == [{"direction": "awaited", "state": "open"}]
        assert _state(world, "SELECT kind, rule_id FROM expectation_evidence WHERE source_key=?",
                      f"message:{change['message_id']}") == [{"kind": "changed", "rule_id": "judge:changes-or-cancels"}]


def test_an_addressed_message_opens_an_owed_item_only_when_it_asks(tmp_path) -> None:
    with make_world(tmp_path) as world:
        Judge(world, tmp_path, {"when": "FYI", "reply": {**NOTHING, "opens_owed": 0.1, "needs_now": 0.3}},
              {"when": "approve", "reply": {**NOTHING, "opens_owed": 0.9, "needs_now": 0.6}})
        _catch_up(world)
        fyi, trigger = _direct(world, "FYI the nightly finished")
        held = _decide(world, trigger=trigger)
        assert (held["decision"], held["rule"]) == ("hold", "hold:digest")  # held, never kept
        assert _item(held, fyi)["rules"] == {"decision": "wake", "rule": "wake:brief"}
        assert _asked_of(world, fyi) == []
        ask, trigger = _direct(world, "Please approve the deploy")
        woken = _decide(world, **_after(held, trigger=trigger))
        assert (woken["decision"], woken["evidence"]) == ("wake", f"message:{ask['message_id']}")
        assert _asked_of(world, ask) == [{"direction": "owed", "state": "open"}]


def test_needs_now_moves_only_the_margins_set_in_the_policy(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path, {"when": "whenever", "reply": {**NOTHING, "opens_owed": 0.9, "needs_now": 0.2}},
                      {"when": "outage", "reply": {**NOTHING, "needs_now": 0.9}},
                      {"when": "routine", "reply": {**NOTHING, "needs_now": 0.7}})
        _catch_up(world)
        later, trigger = _direct(world, "Please look whenever you can")
        result = _decide(world, trigger=trigger)
        assert (result["decision"], result["rule"]) == ("hold", "hold:digest")  # owed, and it can wait
        assert _asked_of(world, later) == [{"direction": "owed", "state": "open"}]
        outage = say(world, room, "worker", "The outage is spreading")  # addressed to nobody
        result = _decide(world, since=result["through_pos"], floor=result["floor"],
                         trigger=f"message:{outage['seqs']['planner']}")
        assert (result["decision"], result["evidence"]) == ("wake", f"message:{outage['message_id']}")
        assert _item(result, outage)["rules"] == {"decision": "keep", "rule": "keep:retain"}
        routine = say(world, room, "worker", "A routine note")
        result = _decide(world, since=result["through_pos"], floor=result["floor"],
                         trigger=f"message:{routine['seqs']['planner']}")
        assert (result["decision"], result["rule"]) == ("keep", "keep:retain")
        judge.configure(wake_at=0.6)
        again = say(world, room, "worker", "Another routine note")
        result = _decide(world, since=result["through_pos"], floor=result["floor"],
                         trigger=f"message:{again['seqs']['planner']}")
        assert result["decision"] == "wake"


FALLBACKS = [
    ({"exit": 3}, "error"),                                                  # a verdict printed, then a failure
    ({"sleep": 3}, "timeout"),
    ({"raw": "not json"}, "invalid_json"),
    ({"reply": {**NOTHING, "needs_now": 1.5}}, "wrong_shape"),
    ({"reply": {**NOTHING, "item": "f" * 24}}, "wrong_shape"),               # an item it was never offered
    ({"reply": {"relation": "none", "item": None, "opens_owed": 0.0}}, "wrong_shape"),
    ({"reply": {"declined": "operator_relay"}}, "declined"),
]


@pytest.mark.parametrize(("step", "reason"), FALLBACKS,
                         ids=["error", "timeout", "invalid_json", "out_of_range", "unknown_item", "missing_call",
                              "declined"])
def test_each_judge_failure_leaves_the_message_to_the_word_lists(tmp_path, step, reason) -> None:
    with make_world(tmp_path) as world:
        Judge(world, tmp_path, step, timeout=1.0 if reason == "timeout" else 5.0)
        _catch_up(world)
        sent, trigger = _direct(world, "FYI the nightly finished")
        result = _decide(world, trigger=trigger)
        # The word lists: a direct message opens an owed item and wakes.
        assert (result["decision"], result["rule"]) == ("wake", "wake:brief")
        assert _item(result, sent)["judge"] == f"judge_fallback:{reason}"
        assert _item(result, sent)["rules"] == {"decision": "wake", "rule": "wake:brief"}
        assert result["judge"] == {"calls": 1, "fallbacks": 1}
        assert _asked_of(world, sent) == [{"direction": "owed", "state": "open"}]


def test_the_judge_sees_only_what_the_principal_can_see(tmp_path) -> None:
    with make_world(tmp_path) as world:
        made = jobs.create(world.baton, world.config.jobs, request_id="job-seen", actor="planner", title="Task")
        room = conversation(world)
        judge = Judge(world, tmp_path)
        say(world, room, "planner", f"Please check {SECRET} first", attention=["worker"], job_id=made["job_id"])
        _catch_up(world)  # the request opens an awaited item while its conversation is the principal's to read
        conversations.remove(world.baton, world.config, room, "planner", actor="planner")
        messages.send(world.baton, sender="worker", recipient="reviewer", text=f"Between us: {SECRET}",
                      job_id=made["job_id"])
        sent, trigger = _direct(world, "Where do we stand?", job_id=made["job_id"])
        result = _decide(world, trigger=trigger)
        # One request: the direct message. The other two aren't the principal's to read now, nor is the item the
        # conversation post opened, so neither its title nor the item itself reaches the judge.
        assert [(request["message"]["id"], request["items"]) for request in judge.requests] == [
            (sent["message_id"], [])]
        assert SECRET not in judge.log.read_text()
        # That item is still a candidate the judge never saw, so its "none" can't rule it out: the word lists decide.
        assert _item(result, sent)["judge"] == "judge_fallback:unoffered"


def _requests_to_worker(world, room: str, count: int) -> list[dict]:
    """The principal's open requests to one worker in one conversation, oldest first."""
    return [say(world, room, "planner", f"Please run check {index}", attention=["worker"]) for index in range(count)]


@pytest.mark.parametrize("reply", [NOTHING, {**NOTHING, "relation": "answers"}], ids=["none", "answers_no_item"])
@pytest.mark.parametrize("count", [5, 6, 9])
def test_an_untagged_result_for_a_request_past_the_four_offered_is_held_for_reconciliation(
        tmp_path, count, reply) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path, {"reply": reply})  # its verdict names none of the items it is offered
        _catch_up(world)
        asks = _requests_to_worker(world, room, count)
        result = say(world, room, "worker", "Check 0 passed")  # for the oldest request, with no reply_to
        decided = _decide(world, trigger=f"message:{result['seqs']['planner']}")
        [request] = judge.requests
        assert [item["title"] for item in request["items"]] == [f"Please run check {index}"
                                                                for index in range(count - 4, count)]
        # The judge never saw the oldest request, so a verdict naming none of the offered items can't rule that one
        # out: the word lists decide, and they hold the result for reconciliation as they would without a judge.
        item = _item(decided, result)
        assert (item["decision"], item["rule"], item["reason"]) == (
            "hold", "hold:reconcile", "multiple open expectations match this message")
        assert (decided["decision"], decided["rule"], decided["judge"]) == (
            "hold", "hold:reconcile", {"calls": 1, "fallbacks": 1})
        assert _asked_of(world, asks[0]) == [{"direction": "awaited", "state": "open"}]
        assert _state(world, "SELECT kind FROM expectation_evidence WHERE source_key=?",
                      f"message:{result['message_id']}") == [{"kind": "uncertain"}] * count
        # The next hourly digest cites the result as uncertain, beside the open requests' count.
        digest = _decide(world, **_after(decided, hourly=True))
        cited = [line for line in digest["brief"].split("\n") if result["message_id"] in line]
        assert len(cited) == 1 and cited[0].startswith(f"- hold, uncertain, evidence message:{result['message_id']}")
        assert item["judge"] == "judge_fallback:unoffered"


def test_past_four_a_result_the_judge_ties_to_an_offered_item_still_closes_it(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        Judge(world, tmp_path, {"when": "Check 5", "reply": {**NOTHING, "relation": "answers", "item": 3}})
        _catch_up(world)
        asks = _requests_to_worker(world, room, 6)
        result = say(world, room, "worker", "Check 5 passed")  # for the newest request, with no reply_to
        decided = _decide(world, trigger=f"message:{result['seqs']['planner']}")
        assert _item(decided, result)["judge"] == "judged"
        assert (decided["decision"], decided["rule"], decided["evidence"]) == (
            "wake", "wake:brief", f"message:{result['message_id']}")
        assert [_asked_of(world, ask)[0]["state"] for ask in asks] == ["open"] * 5 + ["satisfied"]


@pytest.mark.parametrize("count", [5, 6])
def test_an_explicit_reply_to_the_oldest_of_many_requests_is_judged_against_that_request(tmp_path, count) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path, {"when": "Check 0", "reply": {**NOTHING, "relation": "answers", "item": 0}})
        _catch_up(world)
        asks = _requests_to_worker(world, room, count)
        reply = say(world, room, "worker", "Check 0 passed", reply_to=asks[0]["message_id"])
        decided = _decide(world, trigger=f"message:{reply['seqs']['planner']}")
        # The reply names its request, so that request is the only candidate, and the judge is offered it.
        assert [item["title"] for item in judge.requests[0]["items"]] == ["Please run check 0"]
        assert (_item(decided, reply)["judge"], decided["decision"]) == ("judged", "wake")
        assert [_asked_of(world, ask)[0]["state"] for ask in asks] == ["satisfied"] + ["open"] * (count - 1)


def test_with_every_candidate_offered_the_judges_none_stands(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path)
        _catch_up(world)
        _requests_to_worker(world, room, 4)
        note = say(world, room, "worker", "Still working through the list")
        decided = _decide(world, trigger=f"message:{note['seqs']['planner']}")
        assert len(judge.requests[0]["items"]) == 4
        item = _item(decided, note)
        assert (item["judge"], item["decision"], item["rule"]) == ("judged", "keep", "keep:retain")
        assert item["rules"] == {"decision": "hold", "rule": "hold:reconcile"}


def test_both_verdicts_are_kept_for_every_judged_event_without_text(tmp_path) -> None:
    with make_world(tmp_path) as world:
        Judge(world, tmp_path, {"when": "asks", "reply": {**NOTHING, "opens_owed": 0.9, "needs_now": 0.5}},
              {"when": "fails", "exit": 1})
        _catch_up(world)
        asked, trigger = _direct(world, f"This asks about {SECRET}")
        failed, _ = _direct(world, f"This fails on {SECRET}")
        result = _decide(world, trigger=trigger)
        assert [(item["judge"], item["rules"]) for item in result["new"]] == [
            ("judged", {"decision": "wake", "rule": "wake:brief"}),
            ("judge_fallback:error", {"decision": "wake", "rule": "wake:brief"})]
        rows = _state(world, "SELECT * FROM judgments ORDER BY source_key")
        assert {(row["source_key"], row["status"], row["relation"], row["opens_owed"], row["rules_kind"])
                for row in rows} == {(f"message:{asked['message_id']}", "judged", "none", 0.9, "brief"),
                                     (f"message:{failed['message_id']}", "judge_fallback:error", None, None, "brief")}
        everything = json.dumps(rows) + json.dumps(result["new"]) + json.dumps(_state(world, "SELECT * FROM decisions"))
        assert SECRET not in everything


def test_the_catch_up_scan_never_asks_the_judge_and_nothing_before_activation_wakes(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"reply": {**NOTHING, "opens_owed": 1.0, "needs_now": 1.0}})
        _direct(world, "Before activation")
        _catch_up(world)
        first = _decide(world)
        assert (first["decision"], first["rule"]) == ("keep", "keep:nothing-new") and judge.requests == []


def test_the_hourly_digest_never_asks_the_judge_and_never_stops_for_it(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"reply": {**NOTHING, "opens_owed": 1.0, "needs_now": 1.0}})
        _catch_up(world)
        for index in range(3):
            _direct(world, f"Note {index}")
        digest = _decide(world, hourly=True)
        assert judge.requests == [] and (digest["coverage"], digest["judge"]) == ("current", None)
        assert [item["judge"] for item in digest["new"]] == [None, None, None]


def test_a_job_wake_still_holds_a_message_the_judge_would_wake_for(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"reply": {**NOTHING, "needs_now": 1.0}})
        _catch_up(world)
        room = conversation(world)
        say(world, room, "worker", "Nothing addressed to anyone")
        made = jobs.create(world.baton, world.config.jobs, request_id="job-wake", actor="worker", title="Task")
        result = _decide(world, trigger=f"job:{made['job_id']}:1")
        assert len(judge.requests) == 1 and result["counts"]["wake"] == 1
        assert (result["decision"], result["rule"]) == ("hold", "hold:non-message-trigger")


def test_a_message_over_a_bound_is_reconciled_and_never_judged(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"reply": {**NOTHING, "opens_owed": 1.0, "needs_now": 1.0}})
        _catch_up(world)
        tokens = " ".join(f"repo#{index}@abcdef0{index:09x}" for index in range(65))
        sent, trigger = _direct(world, f"Please review {tokens}")
        result = _decide(world, trigger=trigger)
        assert (result["decision"], result["rule"]) == ("hold", "hold:reconcile")
        assert _item(result, sent)["judge"] is None and judge.requests == []


def test_judging_starts_at_the_wakes_own_message_and_stops_after_two(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path)
        _catch_up(world)
        sent = [_direct(world, f"Note {index}") for index in range(4)]
        result = _decide(world, trigger=sent[1][1])
        # The first note came before this wake's own message: its own wake was decided when it came, so the word
        # lists read it. Then two judged notes, and the scan stops before the fourth.
        assert [request["message"]["text"] for request in judge.requests] == ["Note 1", "Note 2"]
        assert [item["judge"] for item in result["new"]] == [None, "judged", "judged"]
        assert result["through_pos"] == _item(result, sent[2][0])["pos"]
        assert (result["coverage"], result["covers_trigger"]) == ("behind", True)
        following = _decide(world, **_after(result, trigger=sent[3][1]))
        assert judge.requests[-1]["message"]["text"] == "Note 3" and following["coverage"] == "current"


def test_without_time_for_a_call_the_word_lists_read_the_wakes_own_message(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path)
        _catch_up(world)
        _, trigger = _direct(world, "Note")
        result = _decide(world, trigger=trigger, budget_seconds=0.5)
        # No call starts without the time for it, and the decision still covers its own wake.
        assert (result["decision"], result["rule"], result["covers_trigger"]) == ("wake", "wake:brief", True)
        assert judge.requests == [] and result["judge"] == {"calls": 0, "fallbacks": 1}
        assert [item["judge"] for item in result["new"]] == ["judge_fallback:budget"]


def test_a_conversation_wake_judges_two_and_leaves_the_rest_to_the_word_lists(tmp_path) -> None:
    with make_world(tmp_path) as world:
        room = conversation(world)
        judge = Judge(world, tmp_path)
        _catch_up(world)
        posts = [say(world, room, "worker", f"Note {index}", attention=["planner"]) for index in range(3)]
        result = _decide(world, trigger="conversation")
        # A conversation wake names no event, so only a current scan covers it: the scan never stops for the judge.
        assert (result["coverage"], result["covers_trigger"]) == ("current", True)
        assert [request["message"]["text"] for request in judge.requests] == ["Note 0", "Note 1"]
        assert [(item["message_id"], item["judge"]) for item in result["new"]] == [
            (posts[0]["message_id"], "judged"), (posts[1]["message_id"], "judged"),
            (posts[2]["message_id"], "judge_fallback:budget")]


def test_replay_reuses_the_stored_verdicts_and_never_asks_again(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"when": "approve", "reply": {**NOTHING, "opens_owed": 0.1}})
        _catch_up(world)
        _, trigger = _direct(world, "Please approve the deploy")
        _decide(world, trigger=trigger)
        judge.answer({"reply": {**NOTHING, "opens_owed": 1.0, "needs_now": 1.0}})  # it would answer otherwise now
        replayed = secretary.replay(world.config, world.state_path, world.policy_path)
        assert replayed["differences"] == [] and replayed["stored_sha256"] == replayed["replayed_sha256"]
        assert len(judge.requests) == 1


def test_each_decision_names_its_wakes_own_item_and_counts_what_the_judge_decided(tmp_path) -> None:
    with make_world(tmp_path) as world:
        Judge(world, tmp_path, {"when": "fails", "exit": 1},
              {"when": "asks", "reply": {**NOTHING, "opens_owed": 0.9, "needs_now": 0.9}})
        _catch_up(world)
        failed, trigger = _direct(world, "This fails")
        _direct(world, "This asks for a review")
        first = _decide(world, trigger=trigger)
        assert first.get("trigger_item") == _item(first, failed)
        assert first["trigger_item"]["judge"] == "judge_fallback:error"
        assert first["counts"] == {"wake": 2, "hold": 0, "keep": 0}
        assert first.get("judged_counts") == {"wake": 1, "hold": 0, "keep": 0}
        # A reminder decides the same wake again: its message is no longer new, and the result still names it.
        again = _decide(world, **_after(first, trigger=trigger))
        assert again["new"] == [] and again.get("trigger_item") == first["trigger_item"]
        assert again.get("judged_counts") == first["judged_counts"]
        # Past the window, or for a wake that names no message, there is no such item.
        past = _decide(world, since=first["through_pos"], floor=first["floor"], trigger=trigger)
        assert past.get("trigger_item", "absent") is None
        assert past.get("judged_counts") == {"wake": 0, "hold": 0, "keep": 0}
        assert _decide(world, **_after(first, trigger="conversation")).get("trigger_item", "absent") is None
        assert _decide(world, **_after(first, hourly=True)).get("trigger_item", "absent") is None


def test_a_principal_direct_message_awaits_its_recipient_unless_it_answers_them(tmp_path) -> None:
    with make_world(tmp_path) as world:
        judge = Judge(world, tmp_path, {"when": "All green", "reply": {**NOTHING, "relation": "answers", "item": 0}})
        _catch_up(world)
        asked, _ = _direct(world, "Please approve the rollout")
        answer = messages.send(world.baton, sender="planner", recipient="worker", text="APPROVED")
        request = messages.send(world.baton, sender="planner", recipient="worker", text="Please run the release check")
        reply, trigger = _direct(world, "All green")
        result = _decide(world, trigger=trigger)
        assert _asked_of(world, asked) == [{"direction": "owed", "state": "satisfied"}]
        assert _asked_of(world, answer) == []
        assert _asked_of(world, request) == [{"direction": "awaited", "state": "satisfied"}]
        assert [item["title"] for item in judge.requests[-1]["items"]] == ["Please run the release check"]
        assert (result["decision"], _item(result, reply)["decision"]) == ("wake", "wake")


def test_without_a_judge_a_principal_direct_message_still_opens_nothing(tmp_path) -> None:
    with make_world(tmp_path) as world:
        _catch_up(world)
        request = messages.send(world.baton, sender="planner", recipient="worker", text="Please run the check")
        _catch_up(world)
        assert _asked_of(world, request) == []


def test_policy_judge_block_is_checked_and_frozen_in_the_digest(tmp_path) -> None:
    with make_world(tmp_path) as world:
        plain = secretary.load_policy(world.config, world.baton, world.policy_path)
        assert plain.judge is None
        block = {"command": ["/usr/bin/true"], "timeout_seconds": 1.5, "questions_sha256": "a" * 64}
        policy(world, judge=block)
        loaded = secretary.load_policy(world.config, world.baton, world.policy_path)
        assert loaded.judge == {**block, "owed_digest_below": 0.4, "wake_at": 0.8} and loaded.digest != plain.digest
        policy(world, judge={**block, "wake_at": 0.9})
        assert secretary.load_policy(world.config, world.baton, world.policy_path).digest != loaded.digest
        for bad in ({"command": []}, {"command": ["true"]}, {"command": "/usr/bin/true"}, {"timeout_seconds": 0},
                    {"timeout_seconds": 11}, {"timeout_seconds": True}, {"questions_sha256": "A" * 64},
                    {"wake_at": 1.5}, {"owed_digest_below": 0.9}, {"extra": 1}):
            policy(world, judge={**block, **bad})
            with pytest.raises(ConfigError):
                secretary.load_policy(world.config, world.baton, world.policy_path)
