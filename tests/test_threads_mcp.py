"""Threads and attention over the MCP surface: two OAuth clients and a CLI agent in one conversation.

The model tests cover the rules; this covers the wiring -- that the tool takes the three new arguments,
hands them to the conversation, resolves each reader's view of a reply's parent, and keeps its refusals
and its audit line. Synthetic and offline: a real host on a loopback port, generic roles, a throwaway
passphrase.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
from test_conversations_mcp import cli_as, client, grant, group_host, texts  # noqa: F401  (group_host: a fixture)
from test_integration import payload

from baton.store import connect

CONVERSATION_ARGUMENTS = {"reply_to", "attention", "needs_decision"}


def one(page: dict[str, Any], text: str) -> dict[str, Any]:
    [found] = [message for message in page["messages"] if message["text"] == text]
    return found


# A fixture is always registered under its own name, so the parameter has to shadow the import it came from.
def test_a_reply_over_mcp_carries_its_attention_and_resolves_its_parent_per_reader(
        group_host,  # noqa: F811
        capsys) -> None:
    host = group_host
    reviewer_token, assistant_token = grant(host, "reviewer"), grant(host, "assistant")
    code, made = cli_as(host, capsys, "conversation", "new", "--title", "Release plan",
                        "--with", "orchestrator,reviewer,assistant")
    assert code == 0
    conversation_id = made["conversation_id"]

    async def run() -> dict[str, Any]:
        async with client(host, reviewer_token) as reviewer, client(host, assistant_token) as assistant:
            schema = {t.name: t for t in (await reviewer.list_tools()).tools}["send_message"].input_schema
            assert CONVERSATION_ARGUMENTS <= set(schema["properties"])
            assert not {"role", "from_role", "sender"} & set(schema["properties"])

            question = payload(await reviewer.call_tool("send_message", {
                "conversation_id": conversation_id, "text": "which release?"}))
            answered = payload(await assistant.call_tool("send_message", {
                "conversation_id": conversation_id, "text": "the next one",
                "reply_to": question["message_id"], "attention": ["reviewer"], "needs_decision": True}))
            assert set(answered["seqs"]) == {"orchestrator", "reviewer"}

            # The three arguments belong to a conversation; a direct send has no thread to reply into.
            refused = payload(await assistant.call_tool("send_message", {
                "to": "reviewer", "text": "x", "reply_to": question["message_id"]}))
            outside = payload(await assistant.call_tool("send_message", {
                "conversation_id": conversation_id, "text": "x", "attention": ["outsider"]}))
            unseen = payload(await assistant.call_tool("send_message", {
                "conversation_id": conversation_id, "text": "x", "reply_to": "msg_nonexistent"}))
            assert refused["error"] == unseen["error"] == "invalid_request"
            assert outside["error"] == "not_a_participant"

            return {"question": question, "answered": answered,
                    "read": payload(await reviewer.call_tool("read_messages", {})),
                    "history": payload(await reviewer.call_tool("read_history",
                                                                {"conversation_id": conversation_id}))}

    seen = anyio.run(run)
    reference = {"message_id": seen["question"]["message_id"], "seq": 1}
    delivered = one(seen["read"], "the next one")
    assert (delivered["reply_to"], delivered["attention"], delivered["needs_decision"]) == \
        (reference, ["reviewer"], True)
    assert texts(seen["history"]) == [("reviewer", "which release?"), ("assistant", "the next one")]
    assert one(seen["history"], "the next one")["reply_to"] == reference
    assert one(seen["history"], "which release?")["reply_to"] is None

    # The CLI carries them too, and the audit names the parent without any message text.
    code, from_cli = cli_as(host, capsys, "message", "--conversation", conversation_id, "--text", "from the cli",
                            "--reply-to", seen["answered"]["message_id"], "--attention", "reviewer,assistant",
                            "--needs-decision")
    assert code == 0 and set(from_cli["seqs"]) == {"reviewer", "assistant"}
    conn = connect(host.config.db_path)
    try:
        [row] = conn.execute("SELECT reply_to, attention, needs_decision FROM messages WHERE text = 'from the cli'"
                             " AND recipient = 'reviewer'").fetchall()
        assert (row["reply_to"], json.loads(row["attention"]), row["needs_decision"]) == \
            (seen["answered"]["message_id"], ["reviewer", "assistant"], 1)
    finally:
        conn.close()
    audit = [json.loads(line) for line in host.config.audit_path.read_text().splitlines()]
    replies = [line["reply_to"] for line in audit if line.get("reply_to")]
    question = seen["question"]["message_id"]
    assert replies == [question, question, "msg_nonexistent"]  # the reply, the refused direct send, the unseen parent
    assert all("text" not in line and "attention" not in line for line in audit)
