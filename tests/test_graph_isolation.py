"""The event-stream client's request and session-record checks on synthetic requests and records."""

from __future__ import annotations

import json

from baton.graph.isolation import record_problems, request_problems

DEVELOPER = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}
USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "probe"}]}
REASONING = {"type": "configuration_update", "reasoning": {"effort": "high"}}
PLAIN = {"type": "response.create", "model": "m", "tool_choice": "auto", "input": [REASONING, DEVELOPER, USER]}


def test_only_a_plain_turn_holding_baton_text_once_passes_the_request_check() -> None:
    # The client reads the instructions file trimmed; a prewarm holds the instructions and no content.
    assert request_problems(PLAIN, "rules\n", "probe", prewarm=False) == []
    assert request_problems({**PLAIN, "input": [DEVELOPER]}, "rules", "probe", prewarm=True) == []
    other_text = {**DEVELOPER, "content": [{"type": "input_text", "text": "environment context"}]}
    for changed, problem in (
        ({"tools": []}, "a tools field"),
        ({"input": [DEVELOPER, USER, {"type": "additional_tools", "items": [{"tools": []}]}]}, "of type additional"),
        ({"input": [{**REASONING, "tools": []}, DEVELOPER, USER]}, "a tools field"),
        ({"instructions": "more"}, "the request field instructions"),
        ({"input": [DEVELOPER, USER, USER]}, "the content 2 times"),
        ({"input": [other_text, DEVELOPER, USER]}, "a developer message with other text"),
        ({"input": [USER]}, "no developer message"),
    ):
        problems = request_problems({**PLAIN, **changed}, "rules", "probe", prewarm=False)
        assert any(problem in found for found in problems), (changed, problems)
    assert "no developer message holding the instructions" in request_problems(
        {**PLAIN, "input": []}, "rules", "probe", prewarm=True
    )


def test_a_session_record_passes_only_with_messages_and_reasoning() -> None:
    def line(kind, **payload):
        return json.dumps({"type": kind, "payload": payload}, ensure_ascii=False)

    plain = [
        line("session_meta", id="t"),
        line("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": "a\u2028b"}]),
        line("response_item", type="reasoning"),
        line("event_msg", type="item_completed", item={"type": "AgentMessage"}),
        line("event_msg", type="token_count"),
    ]
    assert record_problems("\n".join(plain)) == []
    assert record_problems(line("response_item", type="function_call", name="exec")) == [
        "a function_call item at line 1"
    ]
    assert record_problems(line("event_msg", type="item_completed", item={"type": "CommandExecution"})) == [
        "a CommandExecution item event at line 1"
    ]
    assert record_problems(line("compacted") + "\nnot json") == [
        "an entry of type compacted at line 1",
        "an unreadable entry at line 2",
    ]
