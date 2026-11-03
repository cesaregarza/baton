"""MCP result decoding preserves the remote CLI's values and JSON refusals."""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, TextContent

from baton import cli, remote


def result(text: str = "", *, structured=None, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=text)] if text else [],
                          structured_content=structured, is_error=is_error)


def test_sdk_validation_errors_become_remote_refusals_and_cli_json(start_host, monkeypatch, capsys):
    host = start_host()
    with pytest.raises(remote.RemoteRefusal) as raised:
        remote.call(host.base, host.tokens["worker"], "read_messages", {"limit": 0})
    assert raised.value.code == "tool_error"
    detail = str(raised.value)
    assert detail.startswith("Error executing tool read_messages:") and "limit" in detail

    monkeypatch.setenv(remote.TOKEN_ENV, host.tokens["worker"])
    code = cli.main(["--host", host.base, "read", "--limit", "0"])
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "tool_error", "detail": detail}


@pytest.mark.parametrize(("response", "expected"), [
    (result("not JSON", structured={"result": {"messages": []}}), {"messages": []}),
    (result(structured={"messages": []}), {"messages": []}),
    (result(structured={"result": [1, 2]}), [1, 2]),
    (result('{"messages": []}'), {"messages": []}),
    (result('{"result": [1, 2]}'), {"result": [1, 2]}),
    (result("[1, 2]"), [1, 2]),
    (result("0"), 0),
    (result("null"), None),
    (result(), None),
])
def test_successful_responses_keep_their_existing_shapes(response, expected):
    assert remote._decode_result(response) == expected


@pytest.mark.parametrize("representation", ["structured", "wrapped", "text"])
@pytest.mark.parametrize("is_error", [False, True])
def test_baton_refusals_keep_the_hosts_exact_code_and_detail(representation, is_error):
    refusal = {"error": "unknown_job", "detail": "No job named job_missing.\nRead the list again."}
    if representation == "structured":
        response = result(structured=refusal, is_error=is_error)
    elif representation == "wrapped":
        response = result(structured={"result": refusal}, is_error=is_error)
    else:
        response = result(json.dumps(refusal), is_error=is_error)
    with pytest.raises(remote.RemoteRefusal) as raised:
        remote._decode_result(response)
    assert raised.value.code == refusal["error"]
    assert str(raised.value) == refusal["detail"]


@pytest.mark.parametrize("text", ["Validation failed: limit must be at least 1.\nTry again.", "[]", "42", "null", ""])
def test_tool_errors_without_a_baton_refusal_keep_the_original_text(text):
    with pytest.raises(remote.RemoteRefusal) as raised:
        remote._decode_result(result(text, is_error=True))
    assert raised.value.code == "tool_error" and str(raised.value) == text


def test_a_successful_malformed_json_response_is_not_reclassified_as_a_tool_error():
    with pytest.raises(json.JSONDecodeError):
        remote._decode_result(result("not JSON"))


def test_an_error_flagged_dictionary_without_a_refusal_keeps_its_existing_shape():
    assert remote._decode_result(result('{"note": "unchanged"}', is_error=True)) == {"note": "unchanged"}
