"""What one extraction request shows the model of the graph so far.

A graph grows larger than the thread it describes, so a request carries the part its new posts are likely to
touch instead of the whole of it:

    outline   every strand on one line, so a new statement can be filed under any of them
    entries   the standing statements and active decisions chosen for these posts, one whole line each
    cards     the summary and open-work lines, evidence and next line ids of every strand a shown entry
              belongs to
    refresh   strands holding statements newer than their summary: each is owed a refresh, so its card and
              those statements are shown whatever else is left out
    next_ids  the strand and decision ids a proposal allocates from

The lines are the ones `baton.graph.entries` serves to readers. A proposal may change only what its request
showed (`shown_ids`), so an entry left out can be missed but never altered unread.
"""

import re
from typing import Any

from baton.database import memory, store
from baton.graph import lines
from baton.graph.contract import GraphError
from baton.graph.entries import QUERY_WORDS, Entries
from baton.graph.snapshot import ordinal_key, statement_key
from baton.graph.validation import behind

RECENT_POSTS = 20  # every standing statement and changed active decision from this many posts back is shown
HITS = 20  # each new post adds this many of its best search hits


class Context:
    """Chooses and renders what one graph state shows the model, for any run of new posts after it."""

    def __init__(self, state: dict[str, Any]) -> None:
        require_search()
        self._state = state
        self._entries = Entries({**state, "covered_through_seq": state["watermark"]})
        owed = behind(state)
        self._behind = sorted(owed, key=ordinal_key)
        self._unsummarized = [sid for late in owed.values() for sid in late]
        self._found: dict[int, list[str]] = {}

    def ranked(self, posts: list[dict[str, Any]]) -> list[str]:
        """The entry ids worth showing for these new posts, the ones to keep longest first.

        New posts mostly answer what was just said, so the standing statements of the last RECENT_POSTS posts
        lead, newest post first, then the active decisions changed in those posts. Each new post's best search
        hits follow, reaching the older entries it is about.
        """
        state = self._state
        oldest = max(state["watermark"] - RECENT_POSTS, 0)
        recent = [sid for seq in range(state["watermark"], oldest, -1) for sid, _ in self._entries.find(f"post:{seq}")]
        changed = [
            key
            for key in sorted(state["decisions"], key=ordinal_key)
            if state["decisions"][key]["status"] == "active" and state["decisions"][key]["last_changed"] > oldest
        ]
        hits = [uid for post in posts for uid in self._hits(post)]
        return list(dict.fromkeys(recent + changed + hits))

    def render(self, ids: list[str]) -> dict[str, Any]:
        """The request's `context` with these entries shown whole and a card for every strand they belong to.

        The statements a strand owed a refresh holds past its summary are shown whatever `ids` names. That
        refresh moves the summary's frontier over them, so it must not be written without them.
        """
        state = self._state
        shown = {*ids, *self._unsummarized}
        statements = sorted((uid for uid in shown if uid in state["statements"]), key=statement_key)
        decisions = sorted((uid for uid in shown if uid in state["decisions"]), key=ordinal_key)
        carded = {sid for uid in statements for sid in state["statements"][uid]["strand_ids"]}
        carded |= {sid for uid in decisions for sid in state["decisions"][uid]["applies_to"]}  # "all" is no strand
        carded = (carded & state["strands"].keys()) | set(self._behind)
        return {
            "outline": self._entries.outline(),
            "cards": [self._card(sid) for sid in sorted(carded, key=ordinal_key)],
            "entries": [line for uid in statements + decisions for _, line in self._entries.find(uid)],
            "refresh": self._behind,
            "next_ids": {"strand": _next(state["strands"], "S"), "decision": _next(state["decisions"], "D")},
        }

    def _card(self, strand_id: str) -> str:
        """A strand's card as readers see it, plus what an edit of it needs.

        That is the evidence its summary cites, since an edit cites its own, and the ids its next lines take: the
        current lines cannot say which ids retired lines have used.
        """
        [(_, card)] = self._entries.find(strand_id)
        strand = self._state["strands"][strand_id]
        return f"{card}\nEvidence: {' '.join(strand['evidence'])}\nNext line IDs: {' '.join(lines.next_ids(strand))}"

    def _hits(self, post: dict[str, Any]) -> list[str]:
        if post["seq"] not in self._found:
            lines = self._entries.search(queries(post["text"]), HITS)
            self._found[post["seq"]] = [line.split(" | ", 1)[0] for line in lines]
        return self._found[post["seq"]]


def require_search() -> None:
    """Refuse a host whose SQLite has no FTS5: without the search there is no choosing what to show.

    Showing only the recent entries instead would look like it worked while older statements went unread.
    """
    with memory() as index:
        if not store.fts5_available(index):
            raise GraphError("graph extraction needs SQLite FTS5, which this host does not have")


def queries(text: str) -> list[str]:
    """One post as search queries: each line's distinct words, in runs the search takes whole."""
    runs = []
    for line in text.split("\n"):
        distinct = list(dict.fromkeys(re.findall(r"\w+", line.lower())))
        runs += [" ".join(distinct[i : i + QUERY_WORDS]) for i in range(0, len(distinct), QUERY_WORDS)]
    return runs


def shown_ids(context: dict[str, Any]) -> set[str]:
    """The ids a request showed whole: every entry line and strand card starts with its id."""
    return {text.split(" | ", 1)[0] for text in context["entries"] + context["cards"]}


def _next(existing: dict[str, Any], prefix: str) -> str:
    return f"{prefix}{max((ordinal_key(key) for key in existing), default=0) + 1}"
