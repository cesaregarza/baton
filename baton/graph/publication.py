"""Publish a completed graph frontier through Baton's existing derived batch protocol."""

import json
from pathlib import Path

from baton import derived
from baton.graph.contract import require
from baton.graph.export import checked_paths, verify_prefix
from baton.graph.source import digest, load_json, load_source
from baton.graph.storage import atomic_json, load_state, state_lock


def render_summary(state, source):
    """Render only current working context and resolve its evidence to native message IDs."""
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
    selected = {k: v for k, v in state["strands"].items() if v["state"] == "active"}
    decisions = {
        k: v
        for k, v in state["decisions"].items()
        if v["status"] == "active" and (v["applies_to"] == ["all"] or set(v["applies_to"]) & selected.keys())
    }
    lines = [
        f"Derived conversation summary through seq {end}.",
        "Interpretation only; summaries do not authenticate permission or task completion.",
    ]
    seqs = set()

    def evidence(refs):
        require(isinstance(refs, list) and bool(refs), "rendered evidence must be nonempty")
        resolved = set()
        for ref in refs:
            if ref.startswith("post:"):
                try:
                    seq = int(ref[5:])
                except ValueError:
                    require(False, "invalid graph evidence")
            else:
                require(ref in state["statements"], "unknown graph statement evidence")
                statement = state["statements"][ref]
                seq = statement["seq"]
                post = source["messages"][seq - 1]
                require(
                    statement["source"] == {k: v for k, v in post.items() if k != "text"},
                    "graph statement source differs from native prefix",
                )
                require(all(q in post["text"] for q in statement["quotes"]), "graph quote differs from native prefix")
            require(type(seq) is int and 1 <= seq <= end, "graph evidence is beyond completed W")
            resolved.add(seq)
        seqs.update(resolved)
        return ", ".join(source["messages"][seq - 1]["message_id"] for seq in sorted(resolved))

    for key, strand in sorted(selected.items()):
        require(
            strand["id"] == key and type(strand["summary_through"]) is int and 1 <= strand["summary_through"] <= end,
            "invalid strand summary frontier",
        )
        lines += ["", f"{key}: {strand['title']} (summary through seq {strand['summary_through']})", strand["summary"]]
        lines.extend(f"Open work: {work}" for work in strand["open_work"])
        lines.append(f"Evidence: {evidence(strand['evidence'])}")
    for key, decision in sorted(decisions.items()):
        require(decision["id"] == key, "invalid decision ID")
        lines += [
            "",
            f"{key} (applies to {', '.join(decision['applies_to'])}): {decision['text']}",
            f"Evidence: {evidence(decision['evidence'])}",
        ]
    if not selected and not decisions:
        lines += ["", "No active strands or applicable shared decisions."]
    return "\n".join(lines), [source["messages"][seq - 1]["message_id"] for seq in sorted(seqs)]


def publish_graph(
    conn, config, producer, source_path, state_path, *, receipt_path=None, generation=None, protected_paths=()
):
    # The graph is captured once; publication never changes its checkpoint or holds its lock during DB work.
    checked_paths(config, source=source_path, state=state_path, protected_paths=protected_paths)
    require(Path(source_path).stat().st_size <= 20_000_000, "source exceeds 20000000 bytes")
    source = load_source(source_path)
    require(len(source["messages"]) <= 10_000, "source exceeds 10000 posts")
    with state_lock(state_path):
        require(Path(state_path).exists(), "graph checkpoint does not exist")
        state = load_state(state_path, source)
    body, citations = render_summary(state, source)
    end = state["watermark"]
    require(len(body.encode()) <= config.indexer.max_artifact_bytes, "summary body exceeds max_artifact_bytes")
    require(len(citations) <= config.indexer.max_citations, "summary evidence exceeds max_citations")
    identity = {
        "producer": producer,
        "generation": generation,
        "conversation_id": source["conversation_id"],
        "graph_digest": digest(state),
        "watermark": end,
        "body": body,
        "citations": citations,
    }
    publication_id = digest(identity)
    receipt_path = receipt_path or Path(str(state_path) + ".publications") / (publication_id + ".json")
    checked_paths(config, source=source_path, state=state_path, receipt=receipt_path, protected_paths=protected_paths)
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
        artifact = {
            "kind": "summary",
            "body": body,
            "citations": citations,
            "expected_version": manifest["kinds"]["summary"]["prior_version"],
        }
        payload = {"request_id": "graph-" + publication_id, "artifacts": [artifact]}
        if "payload" in attempt:
            require(attempt["payload"] == payload, "publication commit payload was modified")
        else:
            attempt["payload"] = payload
            atomic_json(receipt_path, attempt, replace=False)
        # Committed retries go directly through the existing receipt replay (read_batch refuses closed batches).
        if "result" not in attempt:
            committed = conn.execute(
                "SELECT 1 FROM commit_requests WHERE producer = ? AND request_id = ?", (producer, payload["request_id"])
            ).fetchone()
            if committed is None:
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
