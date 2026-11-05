"""Build message payloads and resolve reply parents according to the reader's access."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from baton import board_read
from baton.database import Connection, Row

from . import limits
from .post import Post

type Shape = Callable[[Row, dict[str, Any] | None], dict[str, Any]]


def attribution(row: Row) -> dict[str, str]:
    """Persisted server-bound attribution, optional so legacy/fixed-role results keep their shape."""
    return ({"alias_id": row["alias_id"], "alias": row["alias"], "badge": "guest", "code": row["alias_code"]}
            if row["alias_id"] else {})


def author_label(item: dict[str, Any]) -> str:
    if item.get("alias_id"):
        return f"guest · {item['alias']} · {item['code']}"
    return item.get("from_role") or "the host"


def reply_references(conn: Connection, reader: str, rows: list[Row], config: Any = None
                     ) -> dict[tuple[str | None, str], dict[str, Any]]:
    """What `reader` may be told about each parent the rows answer, keyed by (conversation, parent id).

    A parent is resolved only when it is retained and the shared board predicate allows the reader to see
    it. Otherwise the reference keeps the id carried by the reply but gives `seq` as None, without revealing
    its text or author.

    A parent counts only within the conversation of the reply that names it, so this answers correctly
    on its own rather than on the strength of the check `conversations.send` makes.
    """
    answered = {(row["conversation_id"], row["reply_to"]) for row in rows if row["reply_to"]}
    if not answered:
        return {}
    places = ", ".join(f":parent_{index}" for index in range(len(answered)))
    if config is None:
        condition, values = "(m.recipient = :reader OR m.sender = :reader)", {"reader": reader}
    else:
        condition, values = board_read.message_condition(config, reader)
    params = {**values, **{f"parent_{index}": parent for index, (_, parent) in enumerate(answered)}}
    visible = {(found["conversation_id"], found["id"]): found for found in conn.execute(
        f"SELECT m.id, m.conversation_id, m.conversation_seq, m.alias_id, m.alias, m.alias_code"
        f" FROM messages m WHERE m.id IN ({places}) AND {condition}", params)}
    return {key: {"message_id": key[1], "seq": visible[key]["conversation_seq"] if key in visible else None,
                  **(attribution(visible[key]) if key in visible else {})} for key in answered}


def as_dict(row: Row, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """A message as readers see it. `from_role` is the sender's credential role, or None for a host notice.

    `author_kind` is `human` or `agent`, recorded when the message was sent; None for a host notice.

    `reply_to` is the reader's view of the message this one answers, from `reply_references`; None means
    this message answers nothing, or that it is being shaped for no particular reader.
    """
    return Post.from_row(row).to_dict(reply_to)


def as_page_for(conn: Connection, reader: str, rows: list[Row],
                shape: Shape = as_dict, config: Any = None) -> list[dict[str, Any]]:
    """A page of messages as `reader` sees them, with each reply's parent resolved in one query."""
    references = reply_references(conn, reader, rows, config)
    return as_page(rows, lambda row: shape(row, references.get((row["conversation_id"], row["reply_to"]))))


def as_page(rows: list[Row],
            shape: Callable[[Row], dict[str, Any]] = as_dict) -> list[dict[str, Any]]:
    page: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        message = shape(row)
        size += 2 * len(json.dumps(message))
        # Always return at least one message: admission keeps any single message far below the page budget.
        if page and size > limits.MAX_PAGE_BYTES:
            break
        page.append(message)
    return page
