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


def update_relevance(state, proposal, added):
    updates = indexed(proposal["relevance_updates"], "relevance update")
    require(not updates.keys() & added, "new statements cannot also have relevance updates")
    for key, update in updates.items():
        require(key in state["statements"], "relevance update references unknown statement")
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


def update_strands(state, proposal, changed, end):
    strands = indexed(proposal["strands"], "strand")
    allocate(state["strands"], strands, "S")
    touched = {sid for key in changed for sid in state["statements"][key]["strand_ids"]}
    for edge in proposal["edges"]:
        for key in (edge["from"], edge["to"]):
            require(key in state["statements"], "edge references unknown statement")
            touched.update(state["statements"][key]["strand_ids"])
    require(touched <= strands.keys(), "every affected strand needs a refreshed summary")
    for key, strand in strands.items():
        require(strand["summary_through"] == end, "strand summary frontier must cover batch end")
        if key in state["strands"]:
            require(strand["title"] == state["strands"][key]["title"], "existing strand title is immutable")
        strand["last_changed"] = end
    state["strands"].update(strands)
    for key, strand in state["strands"].items():
        actual = {sid for sid, statement in state["statements"].items() if key in statement["strand_ids"]}
        require(set(strand["statement_ids"]) == actual, "strand membership must exactly match statements")
    require(
        all(sid in state["strands"] for item in state["statements"].values() for sid in item["strand_ids"]),
        "statement references unknown strand",
    )


def update_decisions(state, proposal, end):
    decisions = indexed(proposal["decisions"], "decision")
    allocate(state["decisions"], decisions, "D")
    refreshed = {item["id"] for item in proposal["strands"]}
    for key, decision in decisions.items():
        applies = decision["applies_to"]
        require(applies == ["all"] or all(sid in state["strands"] for sid in applies), "unknown decision applicability")
        affected = set(state["strands"]) if applies == ["all"] else set(applies)
        old = state["decisions"].get(key)
        if old:
            affected.update(state["strands"] if old["applies_to"] == ["all"] else old["applies_to"])
        require(affected <= refreshed, "decision changes require affected strand summaries")
        require(
            old is None or old["text"] == decision["text"], "decision text is immutable; supersede and create a new ID"
        )
        decision["history"] = (
            []
            if old is None
            else old.get("history", []) + [{field: old[field] for field in ("status", "applies_to", "evidence")}]
        )
        decision["last_changed"] = end
    state["decisions"].update(decisions)


def build_patch(current, proposal, source):
    validate_shape(proposal)
    state, patch = copy.deepcopy(current), copy.deepcopy(proposal)
    start, end = (patch["binding"]["batch"][key] for key in ("start", "end"))
    added = add_statements(state, patch, source, start, end)
    changed = added | update_relevance(state, patch, added)
    check_evidence(
        patch["statements"] + patch["relevance_updates"] + patch["strands"] + patch["decisions"] + patch["edges"],
        state["statements"],
        end,
    )
    update_strands(state, patch, changed, end)
    update_decisions(state, patch, end)
    state["edges"].extend(patch["edges"])
    check_dag(state["edges"], state["statements"])
    return state
