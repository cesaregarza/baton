"""Validate incremental semantics before changing any persisted graph."""

import copy
import re

from baton.graph.contract import require, validate_shape


def indexed(items, label):
    result = {item["id"]: item for item in items}
    require(len(result) == len(items), f"duplicate {label} ID")
    return result


def allocate(existing, additions, prefix):
    new = [key for key in additions if key not in existing]
    require(all(re.fullmatch(prefix + r"[1-9][0-9]*", key) for key in additions), "invalid stable ID")
    start = max((int(key[1:]) for key in existing), default=0) + 1
    require(set(new) == {f"{prefix}{i}" for i in range(start, start + len(new))}, "new IDs must be consecutive")


def check_evidence(items, statements, end):
    for item in items:
        for ref in item["evidence"]:
            if ref.startswith("post:"):
                require(
                    bool(re.fullmatch(r"post:[1-9][0-9]*", ref)) and int(ref[5:]) <= end,
                    "evidence references unavailable source post",
                )
            else:
                require(ref in statements, "evidence references unknown statement")


def check_relevance(item):
    partial = item["relevance"] == "partially_superseded"
    require(
        (isinstance(item["surviving_text"], str) and bool(item["surviving_text"].strip()))
        if partial
        else item["surviving_text"] is None,
        "only partial relevance requires surviving_text",
    )


def check_dag(edges, statements):
    children = {key: set() for key in statements}
    indegree = dict.fromkeys(statements, 0)
    seen = set()
    for edge in edges:
        a, b = edge["from"], edge["to"]
        require(a in statements and b in statements and a != b, "edge needs distinct known statements")
        key = (a, b, edge["kind"])
        require(key not in seen, "duplicate edge")
        seen.add(key)
        if b not in children[a]:
            children[a].add(b)
            indegree[b] += 1
    ready = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for child in children[node]:
            indegree[child] -= 1
            if not indegree[child]:
                ready.append(child)
    require(visited == len(statements), "semantic edge cycle")


def add_statements(state, proposal, source, start, end):
    additions = indexed(proposal["statements"], "statement")
    require(not additions.keys() & state["statements"].keys(), "statement IDs are immutable")
    by_seq = {}
    for item in additions.values():
        seq = item["seq"]
        require(start <= seq <= end, "statement outside batch")
        by_seq.setdefault(seq, []).append(item["id"])
        check_relevance(item)
        post = source["messages"][seq - 1]
        require(all(quote in post["text"] for quote in item["quotes"]), "quote is not exact source text")
        item["source"] = {key: value for key, value in post.items() if key != "text"}
        item["relevance_evidence"] = item["evidence"][:]
        item["relevance_history"] = []
    for seq, ids in by_seq.items():
        require(set(ids) == {f"{seq}-{i}" for i in range(1, len(ids) + 1)}, "statement ordinals must start at 1")
    dispositions = {item["seq"]: item for item in proposal["dispositions"]}
    require(len(dispositions) == len(proposal["dispositions"]), "duplicate disposition")
    require(not dispositions.keys() & by_seq.keys(), "post has both statements and disposition")
    require(set(dispositions) | set(by_seq) == set(range(start, end + 1)), "incomplete or extra batch coverage")
    state["statements"].update(additions)
    state["dispositions"].extend(proposal["dispositions"])
    return set(additions)


def update_relevance(state, proposal, added, shown):
    updates = indexed(proposal["relevance_updates"], "relevance update")
    require(not updates.keys() & added, "new statements cannot also have relevance updates")
    for key, update in updates.items():
        require(key in state["statements"], "relevance update references unknown statement")
        require(key in shown, "relevance update targets a statement the request did not show")
        check_relevance(update)
        current = state["statements"][key]
        current["relevance_history"].append(
            {
                "relevance": current["relevance"],
                "surviving_text": current["surviving_text"],
                "evidence": current["relevance_evidence"],
            }
        )
        current.update({field: update[field] for field in ("relevance", "surviving_text")})
        current["relevance_evidence"] = update["evidence"][:]
    return set(updates)


def behind(state):
    """Strands holding a statement newer than their summary: each is owed a refresh."""
    return {
        key
        for key, strand in state["strands"].items()
        if any(state["statements"][sid]["seq"] > strand["summary_through"] for sid in strand["statement_ids"])
    }


def update_strands(state, proposal, updated, shown, end):
    """Apply the proposal's strands and file each new statement under the strands it names.

    A proposal restates a strand only to refresh its summary; the host keeps the membership and the frontier.
    A strand is due a refresh when this batch changed relevance in it, linked it by an edge, or left it
    holding a statement newer than its summary. Only a strand whose card the request showed is held to that.
    Any other stays behind, and the next request shows its card and asks for the refresh.
    """
    strands = indexed(proposal["strands"], "strand")
    allocate(state["strands"], strands, "S")
    added = [item["id"] for item in proposal["statements"]]
    readable = shown | set(added)
    due = {sid for key in updated for sid in state["statements"][key]["strand_ids"]}
    for edge in proposal["edges"]:
        for key in (edge["from"], edge["to"]):
            require(key in state["statements"], "edge references unknown statement")
            require(key in readable, "edge names a statement the request did not show")
            due.update(state["statements"][key]["strand_ids"])
    for key, strand in strands.items():
        old = state["strands"].get(key)
        if old:
            require(key in shown, "strand refresh needs a card the request did not show")
            require(strand["title"] == old["title"], "existing strand title is immutable")
        strand.update(statement_ids=old["statement_ids"] if old else [], summary_through=end, last_changed=end)
    state["strands"].update(strands)
    for key in added:
        for sid in state["statements"][key]["strand_ids"]:
            require(sid in state["strands"], "statement references unknown strand")
            state["strands"][sid]["statement_ids"].append(key)
            state["strands"][sid]["last_changed"] = end
    require(all(strand["statement_ids"] for strand in strands.values()), "new strand needs a statement")
    due |= behind(state)
    require(due & shown <= strands.keys(), "every affected strand with a card needs a refreshed summary")


def update_decisions(state, proposal, shown, end):
    decisions = indexed(proposal["decisions"], "decision")
    allocate(state["decisions"], decisions, "D")
    for key, decision in decisions.items():
        applies = decision["applies_to"]
        require(applies == ["all"] or all(sid in state["strands"] for sid in applies), "unknown decision applicability")
        old = state["decisions"].get(key)
        if old:
            require(key in shown, "decision change targets a decision the request did not show")
            require(old["text"] == decision["text"], "decision text is immutable; supersede and create a new ID")
        decision["history"] = (
            []
            if old is None
            else old.get("history", []) + [{field: old[field] for field in ("status", "applies_to", "evidence")}]
        )
        decision["last_changed"] = end
    state["decisions"].update(decisions)


def build_patch(current, proposal, source, shown):
    """The state after this proposal, or a GraphError that leaves the current state as it was.

    `shown` holds every id the request showed whole. A proposal may change only those and what it adds itself:
    the model reads part of the graph, and a change to something it could not read would be a guess.
    """
    validate_shape(proposal)
    state, patch = copy.deepcopy(current), copy.deepcopy(proposal)
    start, end = (patch["binding"]["batch"][key] for key in ("start", "end"))
    added = add_statements(state, patch, source, start, end)
    updated = update_relevance(state, patch, added, shown)
    check_evidence(
        patch["statements"] + patch["relevance_updates"] + patch["strands"] + patch["decisions"] + patch["edges"],
        state["statements"],
        end,
    )
    update_strands(state, patch, updated, shown, end)
    update_decisions(state, patch, shown, end)
    state["edges"].extend(patch["edges"])
    check_dag(state["edges"], state["statements"])
    return state
