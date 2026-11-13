"""Read saved, complete conversation snapshots without touching live services."""

import hashlib
import json
from pathlib import Path

from baton.graph.contract import GraphError, require


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphError("cannot read valid JSON artifact") from exc


def load_source(path):
    data = load_json(path)
    require(isinstance(data, dict), "source must be an object")
    conversation = data.get("conversation", {})
    cid = data.get("conversation_id") or (
        conversation.get("conversation_id") if isinstance(conversation, dict) else None
    )
    require(isinstance(cid, str) and bool(cid), "source needs native conversation_id")
    require(not data.get("retention_gaps"), "gapped source snapshots are unsupported")
    posts = data.get("messages")
    require(isinstance(posts, list), "source messages must be a list")
    normalized, ids = [], set()
    for seq, post in enumerate(posts, 1):
        require(
            isinstance(post, dict) and type(post.get("seq")) is int and post["seq"] == seq,
            "source seq must be contiguous starting at 1",
        )
        mid = post.get("message_id")
        require(isinstance(mid, str) and bool(mid) and mid not in ids, "source message IDs must be unique strings")
        ids.add(mid)
        require(post.get("conversation_id", cid) == cid, "incompatible message conversation_id")
        author = post.get("author") or post.get("sender") or post.get("from_role")
        at = post.get("at", post.get("created_at", post.get("timestamp")))
        require(isinstance(author, str) and bool(author), "source post needs author/sender/from_role")
        require(isinstance(at, str) and bool(at), "source post needs at/created_at/timestamp")
        require(isinstance(post.get("text"), str), "source text must be a string")
        normalized.append(
            {
                "seq": seq,
                "message_id": mid,
                "author": author,
                "at": at,
                "text": post["text"],
                "reply_to": post.get("reply_to"),
                "conversation_id": cid,
            }
        )
    through = data.get("through_seq", len(posts))
    require(through == len(posts), "source through_seq disagrees with complete message prefix")
    if isinstance(conversation, dict) and "last_seq" in conversation:
        require(conversation["last_seq"] == len(posts), "source conversation last_seq disagrees with messages")
    return {"conversation_id": cid, "messages": normalized}


def prefix_digest(source, through):
    return digest({"conversation_id": source["conversation_id"], "messages": source["messages"][:through]})
