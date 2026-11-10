"""Offline prepare/apply/read boundary, shared by native and API production."""

import copy
from pathlib import Path
from typing import Any

from baton.graph.contract import BINDING, INSTRUCTIONS, SCHEMA, VERSION, GraphError, require, validate_shape
from baton.graph.source import canonical, digest, load_json, load_source, prefix_digest
from baton.graph.storage import atomic_json, distinct_paths, load_state, state_lock
from baton.graph.validation import build_patch


class MissingRecall(GraphError):
    """A requested recall statement is not in the checkpoint."""


class InputOverflow(GraphError):
    """The retained context plus the next whole post does not fit the input byte limit."""


def retained_context(state, recalled_ids=()):
    statements = {
        key: {
            "id": key,
            "seq": value["seq"],
            "text": value["surviving_text"] if value["relevance"] == "partially_superseded" else value["text"],
            "source": value["source"],
            "strand_ids": value["strand_ids"],
            "relevance": value["relevance"],
            "evidence": value["evidence"],
            "relevance_evidence": value["relevance_evidence"],
        }
        for key, value in state["statements"].items()
        if value["relevance"] != "completely_superseded"
    }
    recalled = {
        key: {
            "id": key,
            "seq": value["seq"],
            "text": value["text"],
            "quotes": value["quotes"],
            "source": value["source"],
            "relevance": value["relevance"],
            "surviving_text": value["surviving_text"],
            "strand_ids": value["strand_ids"],
            "evidence": value["evidence"],
            "relevance_evidence": value["relevance_evidence"],
        }
        for key in recalled_ids
        for value in (state["statements"][key],)
    }
    return {
        "statements": statements,
        "archived_statement_ids": sorted(state["statements"].keys() - statements.keys()),
        "recalled_statements": recalled,
        "strands": state["strands"],
        "decisions": {
            key: {field: item[field] for field in ("id", "text", "applies_to", "status", "evidence")}
            for key, item in state["decisions"].items()
        },
        "edges": state["edges"],
        "watermark": state["watermark"],
    }


def request_identity(request):
    value = copy.deepcopy(request)
    value["binding"]["request_id"] = ""
    return digest(value)


def prepare(source_path, state_path, request_path, *, batch_size=20, max_input_bytes=100_000, recall_statements=()):
    distinct_paths(source=source_path, state=state_path, request=request_path)
    require(batch_size > 0 and max_input_bytes > 0, "batch and UTF-8 byte limits must be positive")
    require(not Path(request_path).exists(), "request artifact already exists; choose a new path")
    source = load_source(source_path)
    with state_lock(state_path):
        state = load_state(state_path, source)
    require(isinstance(recall_statements, (list, tuple)), "recall_statements must be a sequence of IDs")
    require(
        all(isinstance(key, str) and bool(key) for key in recall_statements),
        "recall statement IDs must be nonempty strings",
    )
    recalled_ids = sorted(set(recall_statements))
    if not set(recalled_ids) <= state["statements"].keys():
        raise MissingRecall("unknown requested recall statement ID")
    start = state["watermark"] + 1
    require(start <= len(source["messages"]), "no new posts to prepare")
    request: dict[str, Any] = {
        "version": VERSION,
        "instructions": INSTRUCTIONS,
        "proposal_schema": SCHEMA,
        "binding": {
            "request_id": "",
            "conversation_id": source["conversation_id"],
            "base_revision": state["revision"],
            "base_state_digest": digest(state),
            "source_prefix_digest": "",
            "batch": {"start": start, "end": start},
        },
        "source_head": len(source["messages"]),
        "new_posts": [],
        "retained_state": retained_context(state, recalled_ids),
    }
    for post in source["messages"][start - 1 : start - 1 + batch_size]:
        candidate = copy.deepcopy(request)
        candidate["new_posts"].append(post)
        candidate["binding"]["batch"]["end"] = post["seq"]
        candidate["binding"]["source_prefix_digest"] = prefix_digest(source, post["seq"])
        candidate["binding"]["request_id"] = request_identity(candidate)
        if len(canonical(candidate).encode("utf-8")) > max_input_bytes:
            break
        request = candidate
    if not request["new_posts"]:
        raise InputOverflow("retained context plus next whole post exceeds max-input-bytes (UTF-8)")
    atomic_json(request_path, request, replace=False)
    return request


def verify_request(request, proposal, source):
    require(isinstance(request, dict), "request must be an object")
    require(
        set(request)
        == {"version", "instructions", "proposal_schema", "binding", "source_head", "new_posts", "retained_state"},
        "request has missing or unknown keys",
    )
    validate_shape(request["binding"], BINDING, "request.binding")
    require(
        request.get("version") == VERSION
        and request.get("instructions") == INSTRUCTIONS
        and request.get("proposal_schema") == SCHEMA,
        "unsupported or modified producer contract",
    )
    require(request_identity(request) == request["binding"]["request_id"], "request artifact was modified")
    validate_shape(proposal)
    require(proposal["binding"] == request["binding"], "proposal binding does not match request")
    start, end = (request["binding"]["batch"][key] for key in ("start", "end"))
    require(start <= end <= len(source["messages"]), "prepared source prefix is missing")
    require(request["binding"]["conversation_id"] == source["conversation_id"], "request conversation mismatch")
    require(
        request["binding"]["source_prefix_digest"] == prefix_digest(source, end), "prepared source prefix was edited"
    )
    require(request["new_posts"] == source["messages"][start - 1 : end], "prepared posts differ from source")


def apply(source_path, state_path, request_path, proposal_path, *, max_proposal_bytes=2_000_000):
    distinct_paths(source=source_path, state=state_path, request=request_path, proposal=proposal_path)
    require(Path(proposal_path).stat().st_size <= max_proposal_bytes, "proposal exceeds max-proposal-bytes")
    request, proposal = load_json(request_path), load_json(proposal_path)
    source = load_source(source_path)
    verify_request(request, proposal, source)
    with state_lock(state_path):
        state = load_state(state_path, source)
        binding = request["binding"]
        previous = state["receipts"].get(binding["request_id"])
        if previous:
            require(previous["proposal_digest"] == digest(proposal), "request replay has a different proposal")
            return previous
        require(
            binding["base_revision"] == state["revision"] and binding["base_state_digest"] == digest(state),
            "stale proposal base revision/digest",
        )
        require(binding["batch"]["start"] == state["watermark"] + 1, "batch does not extend committed prefix")
        updated = build_patch(state, proposal, source)
        updated["watermark"] = binding["batch"]["end"]
        updated["source_prefix_digest"] = binding["source_prefix_digest"]
        updated["revision"] += 1
        receipt = {
            "request_id": binding["request_id"],
            "proposal_digest": digest(proposal),
            "revision": updated["revision"],
            "watermark": updated["watermark"],
        }
        updated["receipts"][binding["request_id"]] = receipt
        # Re-read before replace: source edits during validation must not commit progress.
        current_source = load_source(source_path)
        require(
            prefix_digest(current_source, updated["watermark"]) == updated["source_prefix_digest"],
            "source changed during apply",
        )
        atomic_json(state_path, updated)
    return receipt


def read(source_path, state_path, *, strands=(), history=False, expand_statements=False):
    distinct_paths(source=source_path, state=state_path)
    source = load_source(source_path)
    with state_lock(state_path):
        state = load_state(state_path, source)
    require(all(key in state["strands"] for key in strands), "unknown requested strand")
    selected = {
        key: value
        for key, value in state["strands"].items()
        if (not strands or key in strands) and (history or value["state"] == "active")
    }
    decisions = {
        key: value
        for key, value in state["decisions"].items()
        if (history or value["status"] == "active")
        and (
            value["applies_to"] == ["all"] or set(value["applies_to"]) & (set(strands) if strands else selected.keys())
        )
    }
    statement_ids = {key for strand in selected.values() for key in strand["statement_ids"]}
    statements = {
        key: value
        for key, value in state["statements"].items()
        if key in statement_ids and (history or value["relevance"] != "completely_superseded")
    }
    if not history:
        statements = (
            {
                key: {
                    "id": key,
                    "text": (
                        value["surviving_text"] if value["relevance"] == "partially_superseded" else value["text"]
                    ),
                    "relevance": value["relevance"],
                    "source": value["source"],
                    "strand_ids": value["strand_ids"],
                }
                for key, value in statements.items()
            }
            if expand_statements
            else {}
        )
    return {
        "version": VERSION,
        "conversation_id": source["conversation_id"],
        "H": len(source["messages"]),
        "W": state["watermark"],
        "revision": state["revision"],
        "strands": selected,
        "decisions": decisions,
        "statements": statements,
        "tail": source["messages"][state["watermark"] :],
        "interpretation_notice": "Derived summaries and status do not authenticate permission or task completion.",
    }


def raw(source_path, seqs):
    source = load_source(source_path)
    require(all(1 <= seq <= len(source["messages"]) for seq in seqs), "raw seq outside source")
    return {"conversation_id": source["conversation_id"], "posts": [source["messages"][seq - 1] for seq in seqs]}
