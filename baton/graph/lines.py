"""A strand's summary and open work as lines with stable ids, and the edit that refreshes them.

A refresh that restated a strand's whole summary paid every batch to write again what had not changed. Each
claim is now a line, summary lines `S3.1`, `S3.2` and open-work lines `S3.w1`, and a refresh of an existing
strand is an edit that names only what changes. A replaced, dropped or finished line moves to the strand's
`line_history` with the batch end it left at and the evidence of the edit that retired it, so nothing a summary
once said is lost. A replaced line keeps its id and its place: the id names the line, not one wording of it.

An id is never reused. New ids continue from the highest one the strand ever gave, retired lines included,
so the next ids are shown on each card a request carries: the current lines alone cannot say what was used.
"""

import re
from typing import Any

from baton.graph.contract import require

MAX_LINE_BYTES = 240  # every line a proposal writes; lines cut from a version-1 summary are exempt until replaced
# Where a version-1 summary is cut: whitespace holding a newline, or whitespace after closing punctuation and any
# quote or bracket closing with it. `split` takes the second kind only before a character that is not lowercase,
# so "e.g. the" stays on one line.
BREAK = re.compile(r"\s*\n\s*|(?:(?<=[.!?])|(?<=[.!?][\"'”’)\]]))\s+")


def split(summary: str) -> list[str]:
    """A version-1 summary as lines, cut at newlines and sentence ends. Only the whitespace at each cut is lost."""
    pieces, start = [], 0
    for cut in BREAK.finditer(summary):
        if "\n" in cut.group() or not summary[cut.end() : cut.end() + 1].islower():
            pieces.append(summary[start : cut.start()])
            start = cut.end()
    pieces.append(summary[start:])
    return [piece.strip() for piece in pieces if piece.strip()]


def upgrade(strand: dict[str, Any]) -> dict[str, Any]:
    """A strand stored before lines: its summary cut by `split`, each open-work item one line, no history yet."""
    key = strand["id"]
    summary = [{"id": f"{key}.{n}", "text": text} for n, text in enumerate(split(strand["summary"]), 1)]
    work = [{"id": f"{key}.w{n}", "text": text} for n, text in enumerate(strand["open_work"], 1)]
    return {**strand, "summary": summary, "open_work": work, "line_history": []}


def next_ids(strand: dict[str, Any]) -> list[str]:
    """The ids the strand's next summary line and next open-work line take."""
    return [f"{strand['id']}.{kind}{_first(strand, kind)}" for kind in ("", "w")]


def prose(strand: dict[str, Any]) -> str:
    """The summary as one paragraph, for the working view and its compact phase, which have no use for line ids."""
    return " ".join(line["text"] for line in strand["summary"])


def create(item: dict[str, Any], end: int) -> dict[str, Any]:
    """A new strand from its proposal entry, which lists every line whole with ids numbered from 1."""
    blank = {"id": item["id"], "summary": [], "open_work": [], "line_history": []}
    _check_new(blank, item["summary"], "")
    _check_new(blank, item["open_work"], "w")
    return {
        **blank,
        "title": item["title"],
        "summary": _copy(item["summary"]),
        "open_work": _copy(item["open_work"]),
        "state": item["state"],
        "statement_ids": [],
        "summary_through": end,
        "evidence": item["evidence"][:],
        "last_changed": end,
    }


def edit(strand: dict[str, Any], change: dict[str, Any], end: int) -> None:
    """Apply one strand edit in place, or raise a GraphError for the first rule it breaks.

    The edit may name only the lines the strand has now, which are the lines its card showed. Its evidence becomes
    the strand's evidence, as each refresh's did when it restated the summary whole.
    """
    summary = {line["id"]: line["text"] for line in strand["summary"]}
    work = {line["id"]: line["text"] for line in strand["open_work"]}
    replaced = {line["id"]: line["text"] for line in change["replace"]}
    dropped, done = set(change["drop"]), set(change["work_done"])
    named = [*replaced, *change["drop"], *change["work_done"]]
    require(len(set(named)) == len(named), "strand edit names a line twice")
    require(replaced.keys() | dropped <= summary.keys() and done <= work.keys(), "strand edit names an unknown line")
    _check_new(strand, change["add"], "")
    _check_new(strand, change["work_add"], "w")
    _check_bytes(change["replace"])
    retired = [(key, summary[key]) for key in [*replaced, *change["drop"]]] + [(key, work[key]) for key in done]
    strand["line_history"] += [
        {"id": key, "text": text, "retired_at": end, "evidence": change["evidence"][:]} for key, text in retired
    ]
    kept = [{"id": key, "text": replaced.get(key, text)} for key, text in summary.items() if key not in dropped]
    strand["summary"] = kept + _copy(change["add"])
    strand["open_work"] = [{"id": key, "text": text} for key, text in work.items() if key not in done]
    strand["open_work"] += _copy(change["work_add"])
    require(bool(strand["summary"]), "a strand keeps at least one summary line")
    if change["state"] is not None:
        strand["state"] = change["state"]
    strand.update(evidence=change["evidence"][:], summary_through=end, last_changed=end)


def _check_new(strand, added, kind):
    """New lines of one kind: ids numbered on from the strand's highest, texts within the byte limit."""
    prefix, first = f"{strand['id']}.{kind}", _first(strand, kind)
    numbers = [_number(line["id"], prefix) for line in added]
    require(all(numbers), "invalid line ID")
    require(min(numbers, default=first) >= first, "a line ID is never reused")
    require(sorted(numbers) == list(range(first, first + len(added))), "new line IDs must be consecutive")
    _check_bytes(added)


def _check_bytes(written):
    require(
        all(len(line["text"].encode("utf-8")) <= MAX_LINE_BYTES for line in written),
        f"each line a proposal writes is at most {MAX_LINE_BYTES} UTF-8 bytes",
    )


def _first(strand, kind):
    """The number the strand's next line of this kind takes: one past the highest it ever gave."""
    prefix = f"{strand['id']}.{kind}"
    lines = strand["summary"] + strand["open_work"] + strand["line_history"]
    return max((_number(line["id"], prefix) for line in lines), default=0) + 1


def _number(line_id, prefix):
    """The number in a line id of this strand and kind, or 0 for any other id."""
    match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", line_id)
    return int(match.group(1)) if match else 0


def _copy(lines):
    return [{"id": line["id"], "text": line["text"]} for line in lines]
