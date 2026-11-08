"""Publish a completed graph frontier through Baton's existing derived batch protocol."""

import json
from pathlib import Path

from baton import derived
from baton.graph import compact
from baton.graph import snapshot as snapshot_module
from baton.graph.contract import require
from baton.graph.export import checked_paths, verify_prefix
from baton.graph.source import digest, load_json, load_source
from baton.graph.storage import atomic_json, load_state, state_lock


def check_receipts(state):
    """A publishable checkpoint has a contiguous apply receipt chain ending at its W."""
    end = state["watermark"]
    require(end > 0, "graph has no completed prefix")
    receipts = list(state["receipts"].values())
    require(type(state["revision"]) is int and state["revision"] == len(receipts), "invalid graph receipt revisions")
    require(
        sorted(r["revision"] for r in receipts) == list(range(1, state["revision"] + 1)), "invalid graph receipt chain"
    )
    ordered = sorted(receipts, key=lambda r: r["revision"])
    require(ordered[-1]["watermark"] == end, "graph W disagrees with apply receipt")
    require(
        all(a["watermark"] < b["watermark"] for a, b in zip(ordered, ordered[1:], strict=False)),
        "graph receipt frontier regressed",
    )


def render_summary(state, source):
    """Render only current working context and resolve its evidence to native message IDs."""
    check_receipts(state)
    projected = snapshot_module.project(state)
    view = compact.render_view(projected, source)
    return view["body"], snapshot_module.resolve_evidence(view["evidence"], projected, source)


def publish_graph(
    conn,
    config,
    producer,
    source_path,
    state_path,
    *,
    receipt_path=None,
    generation=None,
    protected_paths=(),
    view=None,
    attach_graph=False,
):
    """Publish graph W as a cumulative summary; with `view`, publish that validated compact view instead of prose.

    `attach_graph` adds the canonical snapshot to the same commit, so summary, graph, version, coverage and
    receipt commit together. The graph is captured once; publication never changes its checkpoint or holds its
    lock during database work, and it never runs a model.
    """
    checked_paths(config, source=source_path, state=state_path, protected_paths=protected_paths)
    require(Path(source_path).stat().st_size <= 20_000_000, "source exceeds 20000000 bytes")
    source = load_source(source_path)
    require(len(source["messages"]) <= 10_000, "source exceeds 10000 posts")
    with state_lock(state_path):
        require(Path(state_path).exists(), "graph checkpoint does not exist")
        state = load_state(state_path, source)
    check_receipts(state)
    projected = snapshot_module.project(state)
    if view is None:
        body, citations = render_summary(state, source)
    else:
        require(view["graph_digest"] == projected["graph_digest"], "working view digest differs from captured graph")
        accepted = compact.validate_compact(
            view,
            projected,
            source,
            max_body_bytes=compact.body_limit(config.indexer.max_artifact_bytes),
            max_citations=config.indexer.max_citations,
        )
        body, citations = accepted["body"], accepted["citations"]
    end = state["watermark"]
    require(len(body.encode()) <= config.indexer.max_artifact_bytes, "summary body exceeds max_artifact_bytes")
    require(len(citations) <= config.indexer.max_citations, "summary evidence exceeds max_citations")
    identity = {
        "producer": producer,
        "generation": generation,
        "conversation_id": source["conversation_id"],
        "graph_digest": digest(state),
        "snapshot_digest": projected["graph_digest"] if attach_graph else None,
        "watermark": end,
        "body": body,
        "citations": citations,
    }
    artifact = {"kind": "summary", "body": body, "citations": citations}
    if attach_graph:
        artifact["graph_snapshot"] = projected
    return _commit_publication(
        conn, config, producer, source, end, identity, artifact, receipt_path, state_path, generation, protected_paths
    )


def _commit_publication(
    conn, config, producer, source, end, identity, artifact, receipt_path, state_path, generation, protected_paths
):
    publication_id = digest(identity)
    receipt_path = receipt_path or Path(str(state_path) + ".publications") / (publication_id + ".json")
    checked_paths(config, state=state_path, receipt=receipt_path, protected_paths=protected_paths)
    # Serializes identical local retries, including delivery/commit interruption; no inference occurs here.
    with state_lock(receipt_path):
        verify_prefix(conn, source, end)
        if Path(receipt_path).exists():
            attempt = load_json(receipt_path)
            require(
                attempt.get("identity") == identity,
                "publication receipt identity conflict; choose another receipt path",
            )
            manifest = attempt["manifest"]
        else:
            manifest = derived.begin_batch(
                conn, config, producer, source["conversation_id"], ["summary"], generation=generation, through_seq=end
            )
            require(
                manifest["window"]["to_seq"] == end,
                "manifest limits stop before graph W; adjust limits or use smaller checkpoint",
            )
            attempt = {"identity": identity, "manifest": manifest}
        require(
            manifest["window"]["to_seq"] == end
            and manifest["conversation_id"] == source["conversation_id"]
            and manifest["producer"] == producer
            and set(manifest["kinds"]) == {"summary"},
            "publication manifest does not cover graph W",
        )
        stored = conn.execute("SELECT manifest FROM batches WHERE id = ?", (manifest["batch_id"],)).fetchone()
        require(stored is not None, "publication batch is no longer retained")
        require(json.loads(stored[0]) == manifest, "publication manifest was modified")
        artifact = {**artifact, "expected_version": manifest["kinds"]["summary"]["prior_version"]}
        payload = {"request_id": "graph-" + publication_id, "artifacts": [artifact]}
        if "payload" in attempt:
            require(attempt["payload"] == payload, "publication commit payload was modified")
        else:
            attempt["payload"] = payload
            atomic_json(receipt_path, attempt, replace=False)
        # Committed retries go directly through the existing receipt replay (read_batch refuses closed batches).
        if "result" not in attempt:
            _deliver_pages(conn, config, producer, source, manifest, payload)
        # Native posts are immutable; the derived commit also revalidates retained input identity.
        verify_prefix(conn, source, end)
        result = derived.commit_batch(conn, config, producer, manifest["batch_id"], payload)
        attempt["result"] = result
        atomic_json(receipt_path, attempt)
    return {
        **result,
        "conversation_id": source["conversation_id"],
        "covered_through_seq": end,
        "publication_receipt": str(receipt_path),
    }


def _deliver_pages(conn, config, producer, source, manifest, payload):
    committed = conn.execute(
        "SELECT 1 FROM commit_requests WHERE producer = ? AND request_id = ?", (producer, payload["request_id"])
    ).fetchone()
    if committed is not None:
        return
    for page in range(1, manifest["pages"] + 1):
        delivered = derived.read_batch(conn, config, producer, manifest["batch_id"], page)
        expected = [item for segment in manifest["segments"] for item in segment["items"]]
        expected = expected[(page - 1) * manifest["page_items"] : page * manifest["page_items"]]
        require(len(delivered["items"]) == len(expected), "manifest page differs from source")
        for item, named in zip(delivered["items"], expected, strict=True):
            post = source["messages"][named["seq"] - 1]
            require(
                item["seq"] == post["seq"]
                and item["message_id"] == post["message_id"]
                and item["text"] == post["text"],
                "manifest page differs from source",
            )
