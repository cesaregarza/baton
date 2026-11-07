"""Synthetic event windows for the retained-thread bridge, shaped like two live turns that went wrong.

`forty_one_events` reproduces a turn whose two read pages, at the earlier 48 KiB event bound, each printed more than
the thread's tools display: sixteen long messages fill the first page, twenty-five shorter ones the second.
`nine_events` holds the two shapes a summary dropped: a change held after an approval refusal, before a later
approval, and one message asking two different actors for two different things. The DENSE_WINDOWS carry the text
that costs the most per byte: digests, ids, an encoding, quotes and backslashes, and line breaks. Names, ids,
positions and text are invented.
"""

from __future__ import annotations

import base64
import hashlib
import json

OLD_PAGE_BYTES = 48 * 1024  # the earlier bridge's bound on one page's events
FIRST_POS = 1000
CONVERSATION = "conv_0000000000000a11"
ATTRIBUTION_POLICY = (
    "Synthetic attribution policy. Name the author of every event. Say who asked whom for what. Report a claim as "
    "the sender's report, never as verified. Keep a refusal and a later approval as separate steps. "
) * 10  # about the size of a real policy's instructions
OPENING = "Note {n} \u2014 the caf\u00e9 check, \u65e5\u672c: "  # non-ASCII, which the page bound counts at its worst
FILLER = (
    "the synthetic deployment check ran, the rollout stayed paused, the hourly fallback stayed in place and no reset "
    "was made. Timing was recorded for the review and no action is asked. "
)


def message(pos: int, sender: str, text: str, *, attention: list[str] | None = None, reply_to: str | None = None):
    """One message event as Baton's secretary read path shapes it."""
    key = f"msg_{pos:016x}"
    return {
        "source_key": f"message:{key}",
        "pos": pos,
        "kind": "message",
        "at": f"2026-01-01T00:{(pos // 60) % 60:02d}:{pos % 60:02d}+00:00",
        "actor": sender,
        "audience": None,
        "job_id": None,
        "seq": pos,
        "summary": text[:200],
        "data": {"message_id": key, "seq": pos, "conversation_id": CONVERSATION},
        "message": {
            "id": key,
            "pos": pos,
            "conversation_id": CONVERSATION,
            "to": None,
            "sender": sender,
            "from_kind": "agent",
            "alias": None,
            "at": f"2026-01-01T00:{(pos // 60) % 60:02d}:{pos % 60:02d}+00:00",
            "reply_to": reply_to,
            "job_id": None,
            "attention": attention or [],
            "needs_decision": False,
            "text": text,
            "relayed": [],
            "notice": None,
        },
    }


def printed_size(event: dict) -> int:
    """An event's size as the earlier bridge counted it against OLD_PAGE_BYTES."""
    return len(json.dumps(event, ensure_ascii=False).encode())


def sized(pos: int, sender: str, size: int, **options) -> dict:
    """A message whose event is exactly `size` bytes as OLD_PAGE_BYTES counts them: ASCII filler is cut to fit."""
    text = OPENING.format(n=pos) + FILLER * (2 + size // len(FILLER))
    excess = printed_size(message(pos, sender, text, **options)) - size
    event = message(pos, sender, text[: len(text) - excess], **options)
    assert printed_size(event) == size, (pos, printed_size(event))
    return event


def forty_one_events() -> list[dict]:
    """41 messages: 16 of 3,000 bytes, then 25 of 1,900, so the earlier bound pages them 16 and 25."""
    senders = ("operator", "reviewer", "coordinator")
    events = []
    for number in range(41):
        pos = FIRST_POS + 3 * number
        size = 3000 if number < 16 else 1900
        attention = ["reviewer"] if number % 3 == 0 else ["operator", "coordinator"] if number % 3 == 1 else None
        events.append(sized(pos, senders[number % 3], size, attention=attention))
    return events


def nine_events() -> list[dict]:
    """Nine messages of one review thread, with a refusal before an approval and a message asking two actors."""
    pos = FIRST_POS - 1
    rows = [
        (
            "operator",
            ["reviewer"],
            "The hourly run timed out with no summaries delivered, and the next assessment "
            "replied too late. I propose a same-thread low-effort trial.",
        ),
        ("reviewer", ["operator"], "A reasonable controlled attempt, not a proven fix."),
        (
            "coordinator",
            ["operator", "reviewer"],
            "Safeguards for the trial: pass criteria, latency evidence, a content review and a rollback path.",
        ),
        ("reviewer", ["coordinator"], "Agreed. Keep the fallback in place and verify a real callback."),
        (
            "operator",
            ["reviewer"],
            "The automatic approval review refused the low-effort change: it needs the "
            "owner's separate explicit approval. No change was made. I restored normal receive operation on the same "
            "thread, and the owner's approval is pending.",
        ),
        (
            "operator",
            ["reviewer"],
            "The owner approved the low-effort trial. Low effort and readiness are verified. "
            "Reviewer, please run one factual callback check.",
        ),
        (
            "reviewer",
            ["operator"],
            "Runtime, migration and cutover review are complete; a real assessment and its delivery are still open.",
        ),
        (
            "operator",
            ["reviewer", "coordinator"],
            "First low-effort result: kept on time on an older page; the "
            "current trigger was not reached and the fallback ran. Reviewer: assess whether ordinary catch-up may "
            "continue under this trial. Coordinator: explain the not-reached path and a narrow completion route.",
        ),
        (
            "reviewer",
            ["operator"],
            "This fits older-page catch-up: ordinary catch-up and the hourly run may "
            "continue, with no reset or special retry.",
        ),
    ]
    events = []
    for sender, attention, text in rows:
        pos += 3
        events.append(message(pos, sender, text, attention=attention))
    return events


REFUSAL, APPROVAL, TWO_ACTORS = 4, 5, 7  # indexes in nine_events of the shapes a summary dropped


def digest_listing() -> list[dict]:
    """One message listing 600 file digests, the densest text a review relays: hex runs cost far more per byte."""
    lines = [f"{hashlib.sha256(str(number).encode()).hexdigest()}  file{number}.py" for number in range(600)]
    return [message(FIRST_POS, "reviewer", "\n".join(lines))]


def id_listing() -> list[dict]:
    """40 messages, each naming 60 Baton-shaped ids."""
    names = ("msg", "job", "conv")
    return [
        message(
            FIRST_POS + 3 * number,
            "coordinator",
            " ".join(f"{names[k % 3]}_{_hex(number, k)}" for k in range(60)),
        )
        for number in range(40)
    ]


def encoded_blob() -> list[dict]:
    """One message carrying 30,000 bytes as base64."""
    raw = b"".join(hashlib.sha256(str(number).encode()).digest() for number in range(938))[:30000]
    return [message(FIRST_POS, "operator", base64.b64encode(raw).decode())]


def escapes() -> list[dict]:
    """One message of quotes and backslashes, which each print escaped twice: once in the page's line and once more
    if the cell prints the command's result as an object."""
    return [message(FIRST_POS, "operator", '"\\' * 30000)]


def line_breaks() -> list[dict]:
    """One message of 15,000 short lines: a line break prints as an escape whose last letter joins the next word."""
    return [message(FIRST_POS, "operator", "\nOk" * 15000)]


DENSE_WINDOWS = ("digest_listing", "id_listing", "encoded_blob", "escapes", "line_breaks")


def _hex(number: int, k: int) -> str:
    return hashlib.sha256(f"{number}:{k}".encode()).hexdigest()[:16]


def old_pages(events: list[dict]) -> list[list[dict]]:
    """How the earlier bridge paged these events: as many as fit OLD_PAGE_BYTES, at least one."""
    laid_out, page, size = [], [], 0
    for event in events:
        cost = printed_size(event)
        if page and size + cost > OLD_PAGE_BYTES:
            laid_out.append(page)
            page, size = [], 0
        page.append(event)
        size += cost
    return [*laid_out, page] if page else laid_out
