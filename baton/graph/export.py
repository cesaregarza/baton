"""Canonical host-local native prefixes, shared by export and publication verification."""

from pathlib import Path

from baton.errors import InvalidRequest
from baton.graph.contract import require
from baton.graph.source import canonical, load_source
from baton.graph.storage import atomic_json, distinct_paths, state_lock
from baton.messages.rendering import attribution, author_label

MAX_POSTS = 10_000
MAX_BYTES = 20_000_000


def checked_paths(config, *, protected_paths=(), **paths):
    """Artifacts cannot alias host state or any explicitly protected configuration file."""
    host = config.db_path
    protected = [host, Path(str(host) + "-wal"), Path(str(host) + "-shm"), *protected_paths]
    if paths.get("receipt") is not None:
        paths["receipt lock"] = Path(str(Path(paths["receipt"]).resolve()) + ".lock")
    distinct_paths(**paths, **{f"protected {i}": path for i, path in enumerate(protected)})
    for value in paths.values():
        if value is not None:
            target = Path(value).resolve()
            require(
                not target.is_relative_to(config.data_dir.resolve()), "graph artifacts must be outside host data_dir"
            )


def native_prefix(conn, conversation_id, through_seq=None, *, max_posts=MAX_POSTS, max_bytes=MAX_BYTES):
    """Read all copies in one snapshot, requiring one complete and unambiguous prefix."""
    require(type(max_posts) is int and max_posts > 0, "max-posts must be positive")
    require(type(max_bytes) is int and max_bytes > 0, "max-bytes must be positive")
    with conn.snapshot():
        head = conn.execute("SELECT last_seq FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        require(head is not None, "unknown native conversation")
        end = head[0] if through_seq is None else through_seq
        require(type(end) is int and 1 <= end <= head[0], "through-seq must be within the nonempty native head")
        require(end <= max_posts, "native prefix exceeds max-posts; choose a smaller explicit through-seq")
        posts, identities, size = {}, {}, 0
        for row in conn.execute(
            "SELECT id, conversation_id, conversation_seq, sender, at, text, reply_to, alias_id, alias, alias_code"
            " FROM messages WHERE conversation_id = ? AND conversation_seq BETWEEN 1 AND ?"
            " ORDER BY conversation_seq, at, recipient",
            (conversation_id, end),
        ):
            seq = row["conversation_seq"]
            post = {
                "seq": seq,
                "message_id": row["id"],
                "conversation_id": conversation_id,
                "author": author_label({"from_role": row["sender"], **attribution(row)}),
                "at": row["at"],
                "text": row["text"],
                "reply_to": row["reply_to"],
            }
            # Compare persisted attribution too: two guest identities must not collapse into one label.
            identity = tuple(row[key] for key in ("sender", "alias_id", "alias", "alias_code"))
            require(
                seq not in posts
                or (
                    {k: v for k, v in posts[seq].items() if k != "at"} == {k: v for k, v in post.items() if k != "at"}
                    and identities[seq] == identity
                ),
                f"inconsistent native copies at seq {seq}",
            )
            if seq not in posts:
                posts[seq], identities[seq] = post, identity
                size += len(canonical(post).encode())
                require(size <= max_bytes, "native prefix exceeds max-bytes")
        require(len(posts) == end, "native prefix has missing messages; gapped exports are unsupported")
        source = {"conversation_id": conversation_id, "messages": list(posts.values())}
        require(len({p["message_id"] for p in posts.values()}) == end, "native message ID repeats across seqs")
        require(len(canonical(source).encode()) + 1 <= max_bytes, "native prefix exceeds max-bytes")
        return source


def verify_prefix(conn, source, through):
    authoritative = native_prefix(conn, source["conversation_id"], through)
    require(
        source["messages"][:through] == authoritative["messages"],
        "source prefix differs from authoritative native posts",
    )


def export_source(
    conn,
    config,
    conversation_id,
    path,
    *,
    through_seq=None,
    max_posts=MAX_POSTS,
    max_bytes=MAX_BYTES,
    protected_paths=(),
):
    checked_paths(config, state=path, protected_paths=protected_paths)
    with state_lock(path):
        source = native_prefix(conn, conversation_id, through_seq, max_posts=max_posts, max_bytes=max_bytes)
        if Path(path).exists():
            previous = load_source(path)
            require(previous["conversation_id"] == conversation_id, "existing source conversation differs")
            require(len(previous["messages"]) <= len(source["messages"]), "cannot shorten an existing source")
            require(
                source["messages"][: len(previous["messages"])] == previous["messages"],
                "existing source prefix was edited",
            )
        atomic_json(path, source)
    return {"conversation_id": conversation_id, "through_seq": len(source["messages"]), "file": str(path)}


def invalid_graph(operation):
    """Translate graph validation errors at the host CLI boundary."""
    from baton.graph.contract import GraphError

    try:
        return operation()
    except (GraphError, OSError) as error:
        raise InvalidRequest(str(error)) from None
