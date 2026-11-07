"""The retained secretary thread as Baton's protocol-4 command model, and the thread's three narrow commands.

Baton's secretary runs `secretary_bridge.py model` as its policy's command model, once per model turn. The turn's
request arrives on stdin, protocol version 4: the ordered prompt layers (three developer layers, then the event
window as one user layer), any tool calls and outputs from earlier turns of the same assessment, the tools on offer
and the response schema. The bridge offers that turn in a private spool, GATE_DIR/thread/protocol4, waits for the
thread's answer and prints it for Baton, which checks it again (`Assessment.parse`, the tool-call rules) before it
counts. The thread, a persistent runtime session, runs:

    receive [--wait S]          the live turn's header, or {"status": "none"} after S seconds (1-600; 30)
    read REQUEST_ID [--page N]  page N (1 by default) of that turn's trusted layers and events
    reply REQUEST_ID            its answer on stdin: an assessment or {"tool_calls": [...]}

Every output that hands the thread a live turn prints that turn's exact `reply` command, a quoted heredoc opener
included, and the line that closes it (`reply_form`): the thread submits the command, a newline, its serializer's
JSON, a newline and the closing line, so no answer is ever quoted by hand. The turn's first page also carries the
three trusted layers, so the contract is in front of the thread when it decides, on every turn. BRIDGE_VERSION names
this behavior for the thread's guidance.

Every printed line stays within what the thread's tools show whole (PAGE_TOKENS), because a page they cut loses
events or the contract without a sign; a turn whose header would not fit is never offered. A turn is a fixed
sequence of pages, counted in its header: the layers, then every event once, in window order, each page with an
index line per event. An event or layer too large for one page is split across consecutive pages in numbered parts,
never dropped or shortened. Every output carries a `status`, and each one for a turn that can no longer be answered
says to stop and go back to `receive`.

One deadline covers every turn of an assessment: the secretary host sets it in SECRETARY_DEADLINE_AT, which the
policy's model.env passes through, and Baton kills a turn's process at that deadline. The bridge never gives up
first, so a turn the thread doesn't answer in time ends as Baton's timeout. A turn lives as long as its bridge
process: `bridge.lock` is held for exactly that long, so a killed bridge (a timeout, a cancelled assessment) leaves
nothing a later `receive` hands out or a later `reply` can answer. One turn is live at a time; while one is, another
`model` exits 75 (busy) at once.

What the thread sees is no wider than the earlier read path gave it. A message event comes whole: the message's text,
relayed parts and metadata, as that read returned it. Any other event comes as metadata (its key, position, kind,
time, actor, audience, job and the job's state names), without its free-text summary or note, and a reply parent
outside the window comes as its source key alone. The developer layers are the policy's own trusted instructions.
The thread gives a request id, a position and its answer; nothing it sends names a path, config or command.

The spool keeps a live turn's request, privately, only while its bridge lives: the next `model` or `receive` removes
what a killed one left. timing.jsonl records request ids, steps, times and outcomes, never content.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import select
import shlex
import sys
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import NoReturn

BRIDGE_PATH = Path(__file__).resolve()
HERE = BRIDGE_PATH.parent
sys.path.insert(0, str(HERE))  # `python -I` leaves the script's own directory off the path
# Everything this file takes from the host's gate module, which sits beside it: the spool's root, the policy file's
# name and the private-file helpers. A gate lacking one is refused at import, by name.
GATE_NAMES = ("GATE_DIR", "POLICY_FILE", "_open_private", "_private_dir", "_read_private", "_write_private")
secretary_gate = importlib.import_module("secretary_gate")
if _missing := [name for name in GATE_NAMES if not hasattr(secretary_gate, name)]:
    raise ImportError(f"secretary_gate lacks {', '.join(_missing)}")

PROTOCOL = 4
BRIDGE_VERSION = "4.2"  # 4.1's reply form and contract, with pages bounded to what the thread's tools show; the
# guidance names it
DEADLINE_ENV = "SECRETARY_DEADLINE_AT"
TURN_SECONDS = 60  # Baton's longest turn: no deadline lies further out than this
MAX_REQUEST = 16 * 1024 * 1024
MAX_ANSWER = 64 * 1024  # Baton's bound on a command's output
POLL = 0.02  # seconds between looks at the spool
IDLE_SECONDS = 1.0  # how long a reply waits for its answer's first byte on a pipe left open
RETURN_SECONDS = 0.5  # kept before the deadline for the bridge to print an answer; a later one is refused as late
GRACE = 5  # a safety stop past the deadline, for a bridge whose caller is gone without killing it
WAIT = (1, 600)  # receive's bound, in seconds
# The most one printed line may cost, in tokens as `_tokens` counts them. It protects what the thread sees: its command
# tool and the cell that prints the command's output each cut a longer output, both at a default of 10,000 tokens,
# and a cut page silently loses events or the contract. A call may ask for more, but policy may cap that, so the bound
# never relies on it. 8,500 keeps 1,500 tokens (15%) for the tools' own wrapping of the output and for a tokenizer
# that counts somewhat above the count below, and every read page, layers, index, reply form and metadata included,
# stays within it, as the cell prints it at worst (`_printed`).
PAGE_TOKENS = 8500
# What printed text is taken to cost, by what it is made of, since no one rate is safe for all of it: prose spends
# about five bytes a token, while ids, digests and encodings spend under two, and punctuation little more than one.
# A word is a run of letters of one case shape, at most WORD_LETTERS long, never four consonants in a row, and with a
# vowel when three letters or longer; any other run of letters and digits is dense. Every other ASCII character is a
# symbol, and every byte of a non-ASCII character a token of its own, the most a byte-level tokenizer can spend on it.
# Set against a public byte-pair tokenizer on pages of prose, digests, ids, encodings, JSON, code and random text,
# this count came out above it on every page, by 10% at the least.
WORD_BYTES_PER_TOKEN = 3.0  # words and spaces
DENSE_BYTES_PER_TOKEN = 1.15  # any other run of letters and digits
SYMBOL_BYTES_PER_TOKEN = 1.25  # punctuation and escapes
WORD_LETTERS = 12
RUN = re.compile(r"[A-Za-z0-9]+")
WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+")
NOT_A_WORD = re.compile(r"[^aeiouy]{4}|^[^aeiouy]{3,}$")  # matched case-folded: how random letters look
# What a long text is cut between: a run of letters and digits, with the control character before it, whose escape
# ends in a letter or digit and so joins the run as printed, or any one other character.
ATOM = re.compile(r"[\x00-\x1f]?[A-Za-z0-9]+|[^A-Za-z0-9]")
PLACEHOLDER = 10**6  # wider than any page or part number, so a page's size is fixed before the pages are counted
TURN_OVER = "This turn is over. Stop work on it and go back to receive."
BUSY = 75  # EX_TEMPFAIL: another turn is live
LAYERS = ("developer", "developer", "developer", "user")
EVENT_KEYS = ("source_key", "pos", "kind", "at", "actor", "audience", "job_id", "seq")
EVENT_DATA = ("state", "from", "to", "revision", "conversation_id")  # names and numbers, never free text
ANSWER_KEYS = {"decision", "reason", "summary", "sources"}
DECISIONS = ("wake", "hold", "keep")
REQUEST_ID = re.compile(r"thr_[0-9a-f]{32}")
MARKERS = ("[secretary", "[end of secretary", "[automated baton")  # compared case-folded; the principal's own follows
COVERAGE = (  # beside required_sources, because the bridge checks citations and never the prose
    "Citing every source in required_sources is required and is not coverage: the summary's prose must account for "
    "every event on the page, as the trusted layers on the turn's first page require."
)
UNPRINTABLE = ("Cc", "Cf", "Cs", "Zl", "Zp")  # controls, format and bidi marks, surrogates, line separators
CITED = re.compile(r"\b[a-z]{2,8}_[0-9a-f]{8,}\b")  # anything shaped like a Baton id


class Invalid(ValueError):
    """A turn request that isn't protocol 4 as Baton's secretary sends it."""


def spool_dir() -> Path:
    return secretary_gate.GATE_DIR / "thread" / "protocol4"


def model(spool: Path, raw: bytes) -> int:
    """Baton's side: offer this turn unless another is live, then print the thread's answer for it."""
    turn = parse(raw)
    deadline = _deadline()
    with _locked(spool):
        lease = _lease(spool)
        if lease is None:
            _record(spool, None, "busy")
            return BUSY
        _discard(spool)  # anything here is a dead bridge's: this process now holds the lease
        offer = _offer(turn, deadline)
        if _cost(_line(offered(offer)) + "\n") > PAGE_TOKENS:
            # A header the tools would cut loses the contract's fields unseen, so the turn fails as a fault does
            # rather than reach the thread in part. Its `required_sources` names every event, so only a window of
            # some hundreds of events does this.
            _record(spool, offer["request_id"], "oversized", turn=offer["turn"])
            os.close(lease)
            return 1
        secretary_gate._write_private(spool / "request.json", offer)
        _record(spool, offer["request_id"], "offered", turn=offer["turn"], deadline=round(deadline, 3))
    try:
        while time.time() < deadline + GRACE:
            with _locked(spool):
                answer = _take(spool, offer["request_id"])
            if answer is not None:
                sys.stdout.write(answer)
                sys.stdout.flush()
                return 0
            time.sleep(POLL)
        _record(spool, offer["request_id"], "expired")
        return 1
    finally:
        with suppress(OSError, ValueError), _locked(spool):
            _discard(spool)
        os.close(lease)


def parse(raw: bytes) -> dict:
    """The turn request's parts, checked against the layout Baton's harness builds."""
    if len(raw) > MAX_REQUEST:
        raise Invalid("request exceeds its bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise Invalid("request is not JSON") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "input", "tools", "response_schema", "max_output_tokens"}
        or type(value["version"]) is not int
        or value["version"] != PROTOCOL
    ):
        raise Invalid("not a protocol 4 request")
    items, tools = value["input"], value["tools"]
    if not isinstance(items, list) or len(items) < len(LAYERS) or not isinstance(tools, list):
        raise Invalid("request input is incomplete")
    layers = []
    for item, role in zip(items, LAYERS, strict=False):
        content = item.get("content") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("role") != role
            or not isinstance(content, list)
            or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "input_text"
            or not isinstance(content[0].get("text"), str)
        ):
            raise Invalid("request layers are not in the harness's order")
        layers.append({"role": role, "text": content[0]["text"]})
    try:
        event = json.loads(layers[3]["text"])
    except ValueError as error:
        raise Invalid("the event layer is not JSON") from error
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("events"), list)
        or not isinstance(event.get("reply_context"), list)
        or event.get("mode") not in ("alert", "digest")
        or not all(
            isinstance(entry, dict) and isinstance(entry.get("source_key"), str)
            for entry in [*event["events"], *event["reply_context"]]
        )
    ):
        raise Invalid("the event layer is invalid")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(tools) or not all(isinstance(name, str) for name in names):
        raise Invalid("request tools are invalid")
    return {"layers": layers, "event": event, "history": _history(items[len(LAYERS) :]), "tools": names}


def _history(items: list) -> list[dict]:
    """Earlier turns of this assessment: each tool call with its output, in order, and the turn it was made in."""
    calls: dict[str, dict] = {}
    history, batch, previous = [], 0, None
    for item in items:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "function_call":
            if previous != "function_call":
                batch += 1
            try:
                arguments = json.loads(item["arguments"])
            except (KeyError, TypeError, ValueError) as error:
                raise Invalid("a tool call's arguments are not JSON") from error
            entry = {
                "id": item.get("call_id"),
                "name": item.get("name"),
                "arguments": arguments,
                "turn": batch,
                "output": None,
            }
            if not isinstance(entry["id"], str) or entry["id"] in calls or not isinstance(entry["name"], str):
                raise Invalid("a tool call's identity is invalid")
            calls[entry["id"]] = entry
            history.append(entry)
        elif kind == "function_call_output":
            entry = calls.get(item.get("call_id"))
            if entry is None or entry["output"] is not None or not isinstance(item.get("output"), str):
                raise Invalid("a tool output answers no call")
            try:
                entry["output"] = json.loads(item["output"])
            except ValueError as error:
                raise Invalid("a tool output is not JSON") from error
        else:
            raise Invalid("an unsupported history item")
        previous = kind
    if any(entry["output"] is None for entry in history):
        raise Invalid("a tool call has no output")
    return history


def receive(spool: Path, wait: float) -> dict:
    """The live turn's header, received before or not, so a thread that lost its place can still answer it; else
    "none" once `wait` seconds pass. Its first hand-out is its received time."""
    stop = time.monotonic() + wait
    while True:
        with _locked(spool):
            held = _live(spool)
            if held is not None:
                if held["received_at"] is None:
                    held["received_at"] = time.time()
                    secretary_gate._write_private(spool / "request.json", held)
                    _record(spool, held["request_id"], "received")
                return offered(held)
        if time.monotonic() >= stop:
            return {"status": "none"}
        time.sleep(POLL)


def offered(held: dict) -> dict:
    return {"status": "offered", "request": header(held)}


def header(held: dict) -> dict:
    """What the thread needs to answer the turn, without the events themselves: `read` pages those."""
    event = held["event"]
    events = event["events"]
    return {
        "request_id": held["request_id"],
        "turn": held["turn"],
        "deadline_at": _stamp(held["deadline"]),
        "seconds_left": _seconds_left(held),
        "mode": event["mode"],
        "trigger": event.get("trigger"),
        "since": event.get("since"),
        "floor": event.get("floor"),
        "through_pos": event.get("through_pos"),
        "events": len(events),
        "pages": len(pages(held)),
        "first_pos": events[0].get("pos") if events else None,
        "last_pos": events[-1].get("pos") if events else None,
        "required_sources": [entry["source_key"] for entry in events],
        "coverage": COVERAGE,
        "bridge_version": BRIDGE_VERSION,
        "reply_form": reply_form(held["request_id"]),
        "optional_sources": [entry["source_key"] for entry in event["reply_context"]],
        "summary_bytes": held["summary_bytes"],
        "wake_required": event.get("wake_required"),
        "engine_verdict": event.get("engine_verdict"),
        "tools": held["tools"],
        "tool_calls_left": held["calls_left"],
        "history": [
            {key: entry[key] for key in ("id", "name", "arguments", "output", "turn")} for entry in held["history"]
        ],
        "layers": [
            {
                "index": index,
                "role": layer["role"],
                "bytes": len(layer["text"].encode()),
                "sha256": hashlib.sha256(layer["text"].encode()).hexdigest(),
            }
            for index, layer in enumerate(held["layers"])
        ],
    }


def reply_form(request_id: str) -> dict:
    """The exact command that submits this turn's answer, and the line that closes its heredoc.

    The closing line comes from the turn's id, 128 random bits drawn when the turn is offered, after every event on
    the page was written, so no event's author can have known it. The opener is quoted, so the shell evaluates
    nothing in the body. A one-line serialized answer holds no physical line break to end it early.
    """
    if not REQUEST_ID.fullmatch(request_id):
        raise Invalid("not a request id")  # the id becomes shell text here: only this shape is safe in it
    closing = f"END_{request_id}"
    command = shlex.join([sys.executable, "-I", str(BRIDGE_PATH), "reply", request_id])
    return {"command": f"{command} <<'{closing}'", "closing_line": closing}


def read(spool: Path, request_id: str, number: int) -> tuple[dict, int]:
    """Page `number` of the live turn, as `pages` lays the turn out."""
    with _locked(spool):
        held = _live(spool)
    if held is None or held["request_id"] != request_id:
        return {"status": "not_outstanding", "next": TURN_OVER}, 1
    if _expired(held):
        return {"status": "late", "next": TURN_OVER}, 1  # an answer can no longer arrive in time: reading is wasted
    laid_out = pages(held)
    if not 1 <= number <= len(laid_out):
        return {"status": "invalid", "detail": "no_such_page", "pages": len(laid_out)}, 1
    page = laid_out[number - 1]
    _record(spool, request_id, "read", page=number, events=len(page["index"]))
    return {**page, "seconds_left": _seconds_left(held)}, 0


def pages(held: dict) -> list[dict]:
    """The turn's pages, in order, each a printed line within PAGE_TOKENS: the three trusted layers, the reply
    parents' keys, then every event once, in window order. A page closes when the next item doesn't fit; an item too
    large for a page of its own is split across consecutive pages, in parts. The same turn always pages the same way,
    so the header's count holds for every read."""
    form = reply_form(held["request_id"])
    room = PAGE_TOKENS - _cost(_line(_page(held["request_id"], form, PLACEHOLDER, PLACEHOLDER)) + "\n")
    laid_out: list[dict] = []
    current: dict[str, list] = _contents()
    used = 0.0
    for field, value, line in _items(held, room):
        cost = _item_cost(current, field, value, line)
        if used and used + cost > room:
            laid_out.append(current)
            current, used = _contents(), 0.0
            cost = _item_cost(current, field, value, line)
        current[field].append(value)
        if line is not None:
            current["index"].append(line)
        used += cost
    laid_out.append(current)
    count = len(laid_out)
    return [
        {**_page(held["request_id"], form, number, count), **contents} for number, contents in enumerate(laid_out, 1)
    ]


def _page(request_id: str, form: dict, number: int, count: int) -> dict:
    """A page's frame. `seconds_left` is measured at its widest, TURN_SECONDS, and replaced when the page is read."""
    return {
        "status": "page",
        "request_id": request_id,
        "page": number,
        "pages": count,
        "more": number < count,
        "seconds_left": float(TURN_SECONDS),
        **_contents(),
        "reply_form": form,
    }


def _contents() -> dict[str, list]:
    return {"layers": [], "reply_context": [], "events": [], "index": []}


def _items(held: dict, room: float):
    """Everything a turn's pages carry, in order, as (field, value, index line), each small enough for a page of its
    own: a larger layer or event comes as consecutive parts."""
    for number, layer in enumerate(held["layers"]):
        whole = {"index": number, "role": layer["role"], "text": layer["text"]}
        for part in _parts(whole, {"index": number, "role": layer["role"]}, "text", layer["text"], None, room):
            yield "layers", *part
    for entry in held["event"]["reply_context"]:
        yield "reply_context", {"source_key": entry["source_key"], "text": "withheld"}, None
    events = held["event"]["events"]
    for number, entry in enumerate(events, 1):
        shown = view(entry)
        head = {"source_key": shown.get("source_key"), "pos": shown.get("pos")}
        line = _index_line(number, len(events), entry)
        yield from (("events", *part) for part in _parts(shown, head, "json", _line(shown), line, room))


def _parts(whole: dict, head: dict, field: str, text: str, line: str | None, room: float):
    """`whole` with its index line when it fits a page of its own; else `text` cut at whole characters into parts
    that each fit one, each part `head` with its number, the count and its share of `text` under `field`. Joined in
    order, the shares are `text` exactly: a layer's text, or an event as a whole page prints it."""
    if _item_cost(_contents(), "index", whole, line) <= room:  # alone on a page: no separators
        yield whole, line
        return
    frame = {**head, "part": PLACEHOLDER, "parts": PLACEHOLDER, field: ""}
    budget = room - _item_cost(_contents(), "index", frame, _part_line(line, PLACEHOLDER, PLACEHOLDER))
    shares = _shares(text, budget)
    for number, share in enumerate(shares, 1):
        yield {**head, "part": number, "parts": len(shares), field: share}, _part_line(line, number, len(shares))


def _part_line(line: str | None, number: int, count: int) -> str | None:
    return None if line is None else f"{line} (part {number} of {count})"


def _index_line(number: int, total: int, entry: dict) -> str:
    """One event in a line the thread can enumerate: its place in the turn, position, kind, author, audience and,
    for a message, whom it asks for attention."""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    attention = message.get("attention") if isinstance(message.get("attention"), list) else []
    line = f"{number}/{total} pos {entry.get('pos')} {entry.get('kind')} from {entry.get('actor')}"
    line += f" to {entry.get('audience')}" if entry.get("audience") is not None else ""
    return line + (f" attention {', '.join(str(name) for name in attention)}" if attention else "")


def _item_cost(contents: dict[str, list], field: str, value: dict, line: str | None) -> float:
    """What adding `value`, and its index line, adds to a page's printed line: each item, plus a separator after
    the first in its list."""
    cost = _cost(_line(value)) + (_cost(", ") if contents[field] else 0.0)
    if line is not None:
        cost += _cost(_line(line)) + (_cost(", ") if contents["index"] else 0.0)
    return cost


def _shares(text: str, budget: float) -> list[str]:
    """`text` cut at whole characters into consecutive shares that each cost at most `budget` as a page prints them.
    A share is whole atoms (`ATOM`), which print with a symbol on either side, so a share costs exactly what its
    atoms cost; an atom too costly for a share of its own fills the share with its longest prefix that fits, measured
    whole, since a cut changes the run it falls in."""
    shares: list[str] = []
    start, spent = 0, 0.0
    for atom in ATOM.finditer(text):
        begin, end = atom.span()
        cost = _atom_cost(atom.group())
        if spent + cost > budget and cost <= budget:  # the next share starts with it
            shares.append(text[start:begin])
            start, spent = begin, 0.0
        while spent + cost > budget:
            cut = _longest(text, begin, end, budget - spent)
            if cut == begin and spent == 0:
                raise Invalid("a page cannot hold one character")  # only a frame wider than the bound does this
            shares.append(text[start:cut])
            start, begin, spent = cut, cut, 0.0
            cost = _share_cost(text[begin:end])
        spent += cost
    shares.append(text[start:])
    return shares


def _longest(text: str, begin: int, end: int, room: float) -> int:
    """The end of the longest prefix of text[begin:end] the search finds within `room`, or `begin` for none."""
    low, high = begin, end - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _share_cost(text[begin:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return low


@lru_cache(maxsize=4096)
def _atom_cost(atom: str) -> float:
    return _share_cost(atom)


def _share_cost(share: str) -> float:
    """A share's cost as its page prints it: a JSON string inside the page's line."""
    return _cost(json.dumps(share, ensure_ascii=False)[1:-1])


def _cost(text: str) -> float:
    """What a piece of a printed line costs the thread's tools at worst, as the cell prints it."""
    return _tokens(_printed(text))


def _printed(text: str) -> str:
    """Text as the cell prints it at worst: escaped once more, inside the JSON of an object the command tool returns
    in place of a string. Escaping goes character by character, so the pieces of a line print as the line does."""
    return json.dumps(text, ensure_ascii=False)[1:-1]


def _tokens(text: str) -> float:
    """What text costs the thread's tools, in tokens: each run of letters and digits at its rate, each space as a
    word's, each other ASCII character as a symbol, and every byte of any other character a token."""
    runs = RUN.findall(text)
    words = sum(len(run) for run in runs if _word(run))
    dense = sum(map(len, runs)) - words
    spaces = text.count(" ")
    ascii_chars = len(text.encode("ascii", "ignore"))
    other_bytes = len(text.encode("utf-8", "surrogatepass")) - ascii_chars  # half of a pair counts: it fails later
    symbols = ascii_chars - words - dense - spaces
    return (
        (words + spaces) / WORD_BYTES_PER_TOKEN
        + dense / DENSE_BYTES_PER_TOKEN
        + symbols / SYMBOL_BYTES_PER_TOKEN
        + other_bytes
    )


@lru_cache(maxsize=65536)
def _word(run: str) -> bool:
    return len(run) <= WORD_LETTERS and WORD.fullmatch(run) is not None and not NOT_A_WORD.search(run.casefold())


def _line(value: object) -> str:
    """A value as the bridge prints it."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def view(entry: dict) -> dict:
    """An event as the thread sees it: a message whole, as the earlier read gave it; anything else as metadata."""
    if entry.get("kind") == "message" and isinstance(entry.get("message"), dict):
        return entry
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    shown = {key: entry.get(key) for key in EVENT_KEYS}
    shown["data"] = {key: data[key] for key in EVENT_DATA if key in data}
    withheld = (["summary"] if entry.get("summary") else []) + [f"data.{key}" for key in data if key not in EVENT_DATA]
    if withheld:
        shown["withheld"] = withheld
    return shown


def reply(spool: Path, request_id: str, raw: bytes, refusal: str | None) -> tuple[dict, int]:
    """The thread's answer for the live turn, and only for it. A refused answer leaves the turn open for a fix.
    `refusal` is why stdin could not be an answer, judged before the lock and weighed after the turn is known."""
    known = request_id if REQUEST_ID.fullmatch(request_id) else None  # only an id goes on record
    with _locked(spool):
        held = _live(spool)
        if held is None or held["request_id"] != request_id:
            _record(spool, known, "replied", outcome="not_outstanding")
            return {"status": "not_outstanding", "next": TURN_OVER}, 1
        answer, detail = (None, refusal) if refusal else check(held, raw)
        if answer is None:
            _record(spool, known, "replied", outcome="invalid", detail=detail)
            refused = {"status": "invalid", "detail": detail, "reply_form": reply_form(request_id)}
            return {**refused, **_summary_size(held, raw, detail), "seconds_left": _seconds_left(held)}, 1
        if _expired(held):
            _record(spool, known, "replied", outcome="late")
            return {"status": "late", "next": TURN_OVER}, 1
        secretary_gate._write_private(spool / "reply.json", {"request_id": request_id, "answer": answer})
        _record(
            spool,
            known,
            "replied",
            outcome="accepted",
            kind="tool_calls" if "tool_calls" in answer else answer["decision"],
        )
        return {"status": "accepted"}, 0


def check(held: dict, raw: bytes) -> tuple[dict | None, str | None]:  # noqa: C901, PLR0912  one flat rule ladder
    """The answer, or why Baton would refuse it, by the same rules: `Assessment.parse` and the harness's tool-call
    checks. The summary's text is held to the thread's own rules as well: no controls, no gate markers, and no id the
    turn didn't offer."""
    if len(raw) > MAX_ANSWER:
        return None, "too_long"
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "not_json"
    if not isinstance(answer, dict):
        return None, "wrong_shape"
    if _unpaired_surrogate(answer):
        return None, "unpaired_surrogate"
    if "tool_calls" in answer:
        return _calls(held, answer)
    if set(answer) != ANSWER_KEYS:
        return None, "wrong_shape"
    decision, reason, summary, sources = (answer[key] for key in ("decision", "reason", "summary", "sources"))
    event = held["event"]
    if decision not in DECISIONS:
        return None, "wrong_decision"
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 4096:
        return None, "reason"
    if not isinstance(summary, str) or len(summary.encode()) > held["summary_bytes"]:
        return None, "summary_bytes"
    offered = [entry["source_key"] for entry in [*event["events"], *event["reply_context"]]]
    required = {entry["source_key"] for entry in event["events"]}
    if (
        not isinstance(sources, list)
        or not all(isinstance(key, str) and key in offered for key in sources)
        or len(set(sources)) != len(sources)
    ):
        return None, "unoffered_source"
    if event.get("wake_required") is True and decision != "wake":
        return None, "wake_required"
    needs_summary = decision == "wake" or event["mode"] == "digest"
    if needs_summary and (not summary.strip() or not sources):
        return None, "summary_missing"
    if needs_summary and not required <= set(sources):
        return None, "source_missing"
    if not needs_summary:
        return (None, "summary_without_wake") if summary or sources else (answer, None)
    if any(unicodedata.category(char) in UNPRINTABLE and char != "\n" for char in summary):
        return None, "summary_control"
    if any(marker in summary.casefold() for marker in _markers(held)):
        return None, "summary_marker"
    if not set(CITED.findall(summary)) <= _ids(event):
        return None, "uncited_id"
    return answer, None


def _summary_size(held: dict, raw: bytes, detail: str | None) -> dict:
    """For a summary over its bound, its size and the bound, so the thread shortens it without measuring bytes."""
    if detail != "summary_bytes":
        return {}
    summary = json.loads(raw).get("summary")
    size = len(summary.encode()) if isinstance(summary, str) else None
    return {"summary_bytes": held["summary_bytes"], "summary_size": size}


def _seconds_left(held: dict) -> float:
    """The time left to answer, the bridge's return margin kept."""
    return round(max(0.0, held["deadline"] - RETURN_SECONDS - time.time()), 1)


def _expired(held: dict) -> bool:
    """Whether an answer could still be printed for Baton in time: after this, a reply is late."""
    return time.time() > held["deadline"] - RETURN_SECONDS


def _unpaired_surrogate(value: object) -> bool:
    """Whether the answer holds half of a UTF-16 pair: JSON's escapes allow one, and no UTF-8 output can carry it."""
    try:
        json.dumps(value, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return True
    return False


def _markers(held: dict) -> tuple[str, ...]:
    """The gate's bracketed markers, and the principal's hourly one, which the turn's own principal layer names. Read
    from the stored layer, as either bridge version spools it, never from a field only this version writes."""
    principal = _principal(held["layers"][2]["text"])["principal"]
    return (*MARKERS, f"[{principal.casefold()} hourly")


def _calls(held: dict, answer: dict) -> tuple[dict | None, str | None]:
    calls = answer["tool_calls"]
    if set(answer) != {"tool_calls"} or not isinstance(calls, list) or not calls:
        return None, "wrong_shape"
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
            return None, "tool_call_shape"
        if not all(isinstance(call[key], str) and 0 < len(call[key]) <= 200 for key in ("id", "name")):
            return None, "tool_call_identity"
        if call["name"] not in held["tools"]:
            return None, "unoffered_tool"
        if call["arguments"] != {}:
            return None, "tool_arguments"  # the harness supplies the tool's whole context itself
    ids = [call["id"] for call in calls]
    used = {entry["id"] for entry in held["history"]}
    if len(set(ids)) != len(ids) or used.intersection(ids):
        return None, "tool_call_id_reused"
    if held["calls_left"] is not None and len(ids) > held["calls_left"]:
        return None, "tool_call_limit"
    return answer, None


def _ids(event: dict) -> set[str]:
    """Every id the turn offered: messages, conversations, jobs, and any id in a sender."""
    found: set[str] = set()
    for entry in [*event["events"], *event["reply_context"]]:
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        for value in (
            entry.get("job_id"),
            message.get("id"),
            message.get("conversation_id"),
            message.get("job_id"),
            data.get("conversation_id"),
        ):
            if isinstance(value, str):
                found.add(value)
        found.update(CITED.findall(str(message.get("sender") or "")))
        key = entry["source_key"]
        if key.startswith("message:"):
            found.add(key.split(":", 1)[1])
    return found


def status(spool: Path) -> dict:
    """The spool's state: whether a turn is live and how far it got. Ids and times only."""
    with _locked(spool):
        held = _read(spool / "request.json")
        live = held is not None and _alive(spool)
        known = isinstance(held, dict)
        return {
            "spool": "live" if live else "abandoned" if held is not None else "idle",
            "request_id": held.get("request_id") if known else None,
            "turn": held.get("turn") if known else None,
            "received": bool(held.get("received_at")) if known else None,
            "deadline_at": _stamp(held["deadline"]) if known and "deadline" in held else None,
        }


def timing(spool: Path) -> dict:
    """Per request: the time the thread had, took to receive and to answer, and how the turn ended. Ids and times
    only."""
    path = spool / "timing.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line] if path.exists() else []
    steps: dict[str, dict] = {}
    busy = oversized = 0
    for row in rows:
        if row["step"] in ("busy", "oversized"):
            busy += row["step"] == "busy"
            oversized += row["step"] == "oversized"
            continue
        if row.get("request_id") is None:
            continue
        entry = steps.setdefault(row["request_id"], {"reads": 0, "refused": 0})
        if row["step"] == "read":
            entry["reads"] += 1
        elif row["step"] == "replied" and row.get("outcome") != "accepted":
            entry["refused"] += 1
        else:
            entry[row["step"]] = row
    requests = []
    for request_id, entry in steps.items():
        if "offered" not in entry:
            continue
        offered, received, replied = (entry.get(step, {}).get("at") for step in ("offered", "received", "replied"))
        deadline = entry["offered"].get("deadline")
        outcome = (
            (entry.get("replied", {}).get("kind") or "answered")
            if "replied" in entry
            else ("expired" if "expired" in entry else "unanswered" if received else "unreceived")
        )
        requests.append(
            {
                "request_id": request_id,
                "turn": entry["offered"].get("turn"),
                "outcome": outcome,
                "budget_ms": _ms(offered, deadline),
                "receive_ms": _ms(offered, received),
                "answer_ms": _ms(received, replied),
                "spare_ms": _ms(replied, deadline),
                "reads": entry["reads"],
                "refused": entry["refused"],
            }
        )
    outcomes = dict(Counter(row["outcome"] for row in requests))
    return {"requests": requests, "outcomes": outcomes, "busy": busy, "oversized": oversized}


def _offer(turn: dict, deadline: float) -> dict:
    principal = _principal(turn["layers"][2]["text"])
    limit = _max_tool_calls()
    return {
        "request_id": f"thr_{secrets.token_hex(16)}",
        "turn": 1 + (turn["history"][-1]["turn"] if turn["history"] else 0),
        "deadline": deadline,
        "offered_at": time.time(),
        "received_at": None,
        "summary_bytes": principal["summary_bytes"],
        "layers": turn["layers"][:3],
        "event": turn["event"],
        "history": turn["history"],
        "tools": turn["tools"],
        "calls_left": None if limit is None else max(0, limit - len(turn["history"])),
    }


def _principal(text: str) -> dict:
    """The principal layer's JSON: the principal, its instructions and the summary's byte bound."""
    prefix = "Principal-specific guidance:\n"
    if not text.startswith(prefix):
        raise Invalid("the principal layer is invalid")
    try:
        value = json.loads(text[len(prefix) :])
    except ValueError as error:
        raise Invalid("the principal layer is not JSON") from error
    if (
        not isinstance(value, dict)
        or type(value.get("summary_bytes")) is not int
        or not isinstance(value.get("principal"), str)
        or not value["principal"]
    ):
        raise Invalid("the principal layer is invalid")
    return value


def _max_tool_calls() -> int | None:
    """The policy's tool-call limit, so an answer over it is refused while the thread can still fix it. None where
    the policy can't be read: Baton still enforces it."""
    try:
        value = secretary_gate._read_private(secretary_gate.GATE_DIR / secretary_gate.POLICY_FILE)
    except (OSError, ValueError):
        return None
    limit = value.get("max_tool_calls") if isinstance(value, dict) else None
    return limit if type(limit) is int else None


def _deadline() -> float:
    """The assessment's one deadline, as the secretary host set it: within TURN_SECONDS from now."""
    try:
        deadline = float(os.environ[DEADLINE_ENV])
    except (KeyError, ValueError) as error:
        raise Invalid("the assessment deadline is missing") from error
    now = time.time()
    if not math.isfinite(deadline) or deadline <= now:
        raise Invalid("the assessment deadline has passed")
    return min(deadline, now + TURN_SECONDS)


def _lease(spool: Path) -> int | None:
    """bridge.lock, held for this bridge's life, or None while another bridge holds it. Called under the spool lock,
    as every look at the lease is, so a look never takes it from a bridge starting up."""
    fd = secretary_gate._open_private(spool / "bridge.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _alive(spool: Path) -> bool:
    """Whether a bridge holds the lease. Under the spool lock."""
    fd = _lease(spool)
    if fd is None:
        return True
    os.close(fd)
    return False


def _live(spool: Path) -> dict | None:
    """The live turn, under the spool lock: offered by a bridge that still holds the lease and not yet answered.
    What a dead bridge left is removed here."""
    held = _read(spool / "request.json")
    if held is None:
        return None
    if not _alive(spool):
        _discard(spool)
        _record(spool, held.get("request_id") if isinstance(held, dict) else None, "abandoned")
        return None
    answered = _read(spool / "reply.json")
    if isinstance(answered, dict) and answered.get("request_id") == held.get("request_id"):
        return None
    return held


def _discard(spool: Path) -> None:
    for name in ("request.json", "reply.json"):
        (spool / name).unlink(missing_ok=True)


def _take(spool: Path, request_id: str) -> str | None:
    """The answer `reply` left for this turn, taken out of the spool with the turn itself."""
    value = _read(spool / "reply.json")
    if not isinstance(value, dict) or value.get("request_id") != request_id:
        return None
    _discard(spool)
    _record(spool, request_id, "returned")
    return json.dumps(value["answer"], ensure_ascii=False, separators=(",", ":"))


def _read(path: Path):
    try:
        fd = secretary_gate._open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        raw = source.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError(f"{path.name} exceeds its bound")
    return json.loads(raw)


@contextmanager
def _locked(spool: Path):
    """The spool's lock, around each look and change, never across a wait."""
    secretary_gate._private_dir(spool.parent)
    secretary_gate._private_dir(spool)
    fd = secretary_gate._open_private(spool / "lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _record(spool: Path, request_id: str | None, step: str, **fields) -> None:
    """One timing row: a request id, the step, its time and how it went. Best effort: it never costs an answer."""
    row = json.dumps({"at": round(time.time(), 3), "request_id": request_id, "step": step, **fields}, sort_keys=True)
    with suppress(OSError, ValueError):
        fd = secretary_gate._open_private(spool / "timing.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, (row + "\n").encode())
        finally:
            os.close(fd)


def _stamp(when: float) -> str:
    return datetime.fromtimestamp(when, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(start: float | None, end: float | None) -> int | None:
    return None if start is None or end is None else round((end - start) * 1000)


class _Parser(argparse.ArgumentParser):
    """A usage error prints a status on stdout as every other failure does, so a mistyped command still answers."""

    def error(self, message: str) -> NoReturn:
        print(_line({"status": "invalid", "detail": "usage"}), flush=True)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    """The commands' only arguments: no option names a config, policy, path or token."""
    parser = _Parser(prog="secretary_bridge.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("model", allow_abbrev=False)  # Baton's: the policy's command model
    commands.add_parser("receive", allow_abbrev=False).add_argument("--wait", type=float, default=30)
    reading = commands.add_parser("read", allow_abbrev=False)
    reading.add_argument("request_id")
    reading.add_argument("--page", type=int, default=1)
    commands.add_parser("reply", allow_abbrev=False).add_argument("request_id")
    commands.add_parser("status", allow_abbrev=False)
    commands.add_parser("timing", allow_abbrev=False)  # the operator's: per-turn times
    return parser


def stdin_answer() -> tuple[bytes, str | None]:
    """The reply's stdin, or why it can't be an answer. A terminal, or a pipe that stays open and silent, is refused
    unread: a read would wait on input that never comes, and the turn's time would run out. A heredoc or a finished
    writer is ready at once."""
    stream = sys.stdin
    if stream is not None and stream.isatty():
        return b"", "stdin_terminal"
    if stream is not None and not select.select([stream], [], [], IDLE_SECONDS)[0]:
        return b"", "stdin_idle"  # a pipe held open with nothing in it: a read would wait out the turn
    raw = b"" if stream is None else stream.buffer.read(MAX_ANSWER + 1)
    return raw, None if raw.strip() else "stdin_empty"


def main(argv: list[str]) -> int:
    os.umask(0o077)
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "receive" and not WAIT[0] <= args.wait <= WAIT[1]:
        parser.error(f"--wait must be {WAIT[0]} to {WAIT[1]} seconds")
    spool = spool_dir()
    if args.command == "model":
        try:
            return model(spool, sys.stdin.buffer.read(MAX_REQUEST + 1))
        except Exception:  # any fault is a failed turn: Baton retries the page with backoff
            return 1
    try:
        if args.command == "receive":
            result, code = receive(spool, args.wait), 0
        elif args.command == "read":
            result, code = read(spool, args.request_id, args.page)
        elif args.command == "reply":
            result, code = reply(spool, args.request_id, *stdin_answer())
        elif args.command == "status":
            result, code = status(spool), 0
        else:
            result, code = timing(spool), 0
        printed = _line(result)
        printed.encode()  # a line stdout can't carry, such as half of a pair, is a fault and said as one
    except Exception as error:  # the kind of fault alone: its message could hold a path
        printed, code = _line({"status": "fault", "type": type(error).__name__}), 1
    print(printed, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
