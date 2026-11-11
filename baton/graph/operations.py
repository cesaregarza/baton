"""Offline prepare/apply/read boundary, shared by native and API production."""

import copy
from pathlib import Path

from baton.graph.context import Context, shown_ids
from baton.graph.contract import BINDING, CONTEXT, INSTRUCTIONS, SCHEMA, VERSION, current, require, validate_shape
from baton.graph.source import canonical, digest, load_json, load_source, prefix_digest
from baton.graph.storage import STATE_VERSION, atomic_json, distinct_paths, load_state, state_lock
from baton.graph.validation import build_patch


def request_identity(request):
    value = copy.deepcopy(request)
    value["binding"]["request_id"] = ""
    return digest(value)


def prepare(source_path, state_path, request_path, *, batch_size=20, max_input_bytes=100_000):
    """Write the request for the next posts: as many whole posts as fit, with the graph context chosen for them.

    A post is never split. When the next post alone does not fit with its full context, the context is cut
    from its least valuable end until the request fits. The strand outline stays, and so does what a refresh
    owed to a strand has to read: its card and its statements newer than its summary. A post that does not
    fit with that much is refused.
    """
    distinct_paths(source=source_path, state=state_path, request=request_path)
    require(batch_size > 0 and max_input_bytes > 0, "batch and UTF-8 byte limits must be positive")
    require(not Path(request_path).exists(), "request artifact already exists; choose a new path")
    source = load_source(source_path)
    with state_lock(state_path):
        state = load_state(state_path, source)
    start = state["watermark"] + 1
    require(start <= len(source["messages"]), "no new posts to prepare")
    posts = source["messages"][start - 1 : start - 1 + batch_size]
    context, base = Context(state), digest(state)

    def fitted(count, entries=None):
        """The request for the first `count` posts, or None when it is over the limit."""
        batch = posts[:count]
        shown = context.ranked(batch) if entries is None else entries
        candidate = _request(source, state, base, batch, context.render(shown))
        return candidate if len(canonical(candidate).encode("utf-8")) <= max_input_bytes else None

    request = None
    for count in range(1, len(posts) + 1):
        larger = fitted(count)
        if larger is None:
            break
        request = larger
    if request is None:
        ranked = context.ranked(posts[:1])
        request = fitted(1, [])
        require(
            request is not None,
            "next whole post exceeds max-input-bytes (UTF-8) with only the graph context it must carry",
        )
        kept, over = 0, len(ranked)  # the longest prefix known to fit, the shortest known not to
        while over - kept > 1:
            middle = (kept + over) // 2
            trimmed = fitted(1, ranked[:middle])
            if trimmed is None:
                over = middle
            else:
                kept, request = middle, trimmed
    atomic_json(request_path, request, replace=False)
    return request


def _request(source, state, base, posts, context):
    """The request artifact for one batch of new posts with this graph context."""
    end = posts[-1]["seq"]
    request = {
        "version": VERSION,
        "instructions": INSTRUCTIONS,
        "proposal_schema": SCHEMA,
        "binding": {
            "request_id": "",
            "conversation_id": source["conversation_id"],
            "base_revision": state["revision"],
            "base_state_digest": base,
            "source_prefix_digest": prefix_digest(source, end),
            "batch": {"start": posts[0]["seq"], "end": end},
        },
        "source_head": len(source["messages"]),
        "new_posts": posts,
        "context": context,
    }
    request["binding"]["request_id"] = request_identity(request)
    return request


def verify_request(request, proposal, source):
    require(isinstance(request, dict), "request must be an object")
    require(
        set(request)
        == {"version", "instructions", "proposal_schema", "binding", "source_head", "new_posts", "context"},
        "request has missing or unknown keys",
    )
    validate_shape(request["binding"], BINDING, "request.binding")
    require(current(request), "unsupported or modified producer contract")
    validate_shape(request["context"], CONTEXT, "request.context")
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
        updated = build_patch(state, proposal, source, shown_ids(request["context"]))
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
        "version": STATE_VERSION,
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
