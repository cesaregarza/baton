"""Wiki pages: one set of pages shared across every project, each kept one revision at a time.

A page is a path of 1 to 4 segments, such as `ops/deploys`, each one a topic segment. A page and pages under it may
all exist, and nothing ties them together but the path. The wiki is its own part of Baton: nothing here reads or
changes a topic, a topic grant or a reference document.

Every edit appends the page's next revision and nothing is edited or deleted, whoever asks, the host owner included.
There is no rename, move or delete. A restore writes the next revision with an earlier one's exact text, read here
from the stored row, so the text never passes back through a caller or a model that could alter it.

Who may call is decided by the guard alone: a core role with the tool's scope, or a guest alias with a live binding
token. No grant and no role flag is consulted, and there is no acceptance step: every revision names who wrote it.
An edit or a restore is a compare-and-swap whose retry check, compare and page limit are decided in one write
transaction, by the rules reference documents share (`baton.revisions`). A refusal for the shape of the arguments
comes first and needs no database.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from baton import events, revisions
from baton.database import Connection
from baton.errors import InvalidRequest, LimitExceeded, UnknownWikiPage
from baton.topics import paths as topic_paths

MAX_SEGMENTS = 4
MAX_PAGES = 10000
MAX_LISTED = 100
REVISIONS = revisions.Revisions("wiki_revisions", "page_id", ("restored_from",))
# Joins a page `p` to its current revision `r`, its highest.
CURRENT = (
    "JOIN wiki_revisions r ON r.page_id = p.id"
    " AND r.revision = (SELECT MAX(o.revision) FROM wiki_revisions o WHERE o.page_id = p.id)"
)
# The tools whose audit line names the page, by the path the call gave.
AUDITED = frozenset({"edit_wiki", "restore_wiki"})


def is_path(value: Any) -> bool:
    """Whether `value` is a page path. No binding token or grant key is one: a token has no slash and is longer than a
    segment, and a key holds an underscore."""
    if not isinstance(value, str):
        return False
    segments = value.split("/")
    return len(segments) <= MAX_SEGMENTS and all(topic_paths.is_segment(segment) for segment in segments)


def check_path(value: Any, field: str = "path") -> str:
    if not is_path(value):
        raise InvalidRequest(
            f"{field} must be 1-{MAX_SEGMENTS} segments joined by /, each 1-{topic_paths.MAX_TOPIC_SEGMENT} "
            "characters of a-z, 0-9 and inner hyphens, such as ops/deploys"
        )
    return value


def under(prefix: str | None) -> tuple[str, dict[str, Any]]:
    """The condition, on a page `p`, that it is `prefix` or below it; and its parameters. None is every page.

    A checked path holds neither of LIKE's metacharacters, so it is safe to match literally.
    """
    if prefix is None:
        return "", {}
    return " AND (p.path = :prefix OR p.path LIKE :prefix || '/%')", {"prefix": prefix}


def audit_fields(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """An edit's or a restore's audit line names its page. A refused call's path is whatever the caller sent, so it is
    written only if it is a well-formed path."""
    path = arguments.get("path")
    return {"page": path} if tool in AUDITED and is_path(path) else {}


def _named(path: str) -> str:
    return f"wiki page {path!r}"


def _locate(conn: Connection, path: str) -> tuple[int | None, int]:
    """A page's id and its current revision. A page that does not exist is at revision 0."""
    row = conn.execute(
        "SELECT p.id, r.revision FROM wiki_pages p JOIN wiki_revisions r ON r.page_id = p.id"
        " WHERE p.path = ? ORDER BY r.revision DESC LIMIT 1",
        (path,),
    ).fetchone()
    return (None, 0) if row is None else (row["id"], row["revision"])


def _existing(conn: Connection, path: str) -> tuple[int, int]:
    page_id, current = _locate(conn, path)
    if page_id is None:
        raise UnknownWikiPage(f"no {_named(path)}")
    return page_id, current


def _page(conn: Connection, path: str, expected: int) -> int:
    """The id of the page an edit appends to, made if it is new, or the refusal of a stale edit or a full wiki.

    The limit counts every page and applies only when one is made, so a full wiki can still have each page edited.
    """
    page_id, current = _locate(conn, path)
    revisions.require_current(_named(path), current, expected)
    if page_id is not None:
        return page_id
    held = conn.execute("SELECT COUNT(*) FROM wiki_pages").one()[0]
    if held >= MAX_PAGES:
        raise LimitExceeded(f"the wiki already holds {MAX_PAGES} pages; edit one of them", "wiki_pages")
    conn.execute("INSERT INTO wiki_pages (path, created_at) VALUES (?, ?)", (path, events.now()))
    return conn.execute("SELECT id FROM wiki_pages WHERE path = ?", (path,)).one()[0]


# What an edit or a restore writes, decided under the write lock: the page, the revision's number, its text and the
# revision it restores, if any.
Plan = Callable[[], tuple[int, int, str, int | None]]


def _write(
    conn: Connection,
    *,
    path: str,
    actor: str,
    identity: dict[str, str] | None,
    request_id: str,
    digest: str,
    plan: Plan,
) -> dict[str, Any]:
    """Append what `plan` decides, or answer the retry of a revision already appended, in one write transaction.

    A retry is found before the plan runs, so one made from a revision that has since moved on still answers.
    """
    with conn.transaction():
        stored = events.replayed(conn, REVISIONS.table, request_id, digest)
        if stored is None:
            page_id, number, body, restored_from = plan()
            REVISIONS.append(
                conn,
                page_id,
                number,
                body,
                actor=actor,
                identity=identity,
                request_id=request_id,
                digest=digest,
                restored_from=restored_from,
            )
        else:
            page_id, number = stored["page_id"], stored["revision"]
        return {
            "path": path,
            **REVISIONS.summary(REVISIONS.read(conn, page_id, number)),
            "request_id": request_id,
            "created": stored is None,
        }


def edit(
    conn: Connection,
    *,
    actor: str,
    path: Any,
    body: Any,
    expected_revision: Any,
    request_id: Any = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Append a page's next revision, creating the page when `expected_revision` is 0."""
    path = check_path(path)
    revisions.check_body(body, "a wiki page body", "wiki_body_bytes")
    revisions.check_expected(expected_revision, "the page")
    request_id = revisions.request_id(request_id)
    digest = revisions.digest("edit_wiki", actor, identity, path=path, body=body, expected_revision=expected_revision)

    def plan() -> tuple[int, int, str, int | None]:
        return _page(conn, path, expected_revision), expected_revision + 1, body, None

    return _write(conn, path=path, actor=actor, identity=identity, request_id=request_id, digest=digest, plan=plan)


def restore(
    conn: Connection,
    *,
    actor: str,
    path: Any,
    revision: Any,
    expected_revision: Any,
    request_id: Any = None,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Append a page's next revision with the text of its `revision`, which may be the current one or a restore.

    The retry identity holds the revision in place of a body, so a request id an edit used is a conflict here.
    """
    path = check_path(path)
    revisions.check_revision(revision)
    if not revisions.is_count(expected_revision, 1):
        raise InvalidRequest("expected_revision is required: the revision of the page you read")
    request_id = revisions.request_id(request_id)
    digest = revisions.digest(
        "restore_wiki", actor, identity, path=path, revision=revision, expected_revision=expected_revision
    )

    def plan() -> tuple[int, int, str, int | None]:
        # The stale compare comes before the revision's, so a caller behind the page always learns where it is.
        page_id, current = _existing(conn, path)
        revisions.require_current(_named(path), current, expected_revision)
        revisions.require_revision(UnknownWikiPage, _named(path), revision, current)
        return page_id, current + 1, REVISIONS.read(conn, page_id, revision)["body"], revision

    return _write(conn, path=path, actor=actor, identity=identity, request_id=request_id, digest=digest, plan=plan)


def read(conn: Connection, *, path: Any, revision: Any = None) -> dict[str, Any]:
    """A page's current text, or the text of one earlier revision, exactly as it was written."""
    path = check_path(path)
    if revision is not None:
        revisions.check_revision(revision)
    with conn.snapshot():  # one view, so a write in between cannot mix two revisions into one answer
        page_id, current = _existing(conn, path)
        if revision is not None:
            revisions.require_revision(UnknownWikiPage, _named(path), revision, current)
        found = REVISIONS.read(conn, page_id, current if revision is None else revision)
        return {"path": path, "current_revision": current, "body": found["body"], **REVISIONS.summary(found)}


def history(conn: Connection, *, path: Any, after_revision: Any = 0, limit: Any = 50) -> dict[str, Any]:
    """A page's revisions after `after_revision`, oldest first and without their text."""
    path = check_path(path)
    revisions.check_page(after_revision, limit)
    with conn.snapshot():
        page_id, current = _existing(conn, path)
        return {
            "path": path,
            "current_revision": current,
            **REVISIONS.page(conn, page_id, current, after_revision, limit),
        }


def listing(conn: Connection, *, prefix: Any = None, after: Any = None, limit: Any = 50) -> dict[str, Any]:
    """The pages at and under `prefix` whose paths sort after `after`, in path order, without their text.

    `next_after` is the last path returned while more remain, and null once the list is through.
    """
    scope, values = under(None if prefix is None else check_path(prefix, "prefix"))
    if after is not None:
        check_path(after, "after")
    if not revisions.is_count(limit, 1) or limit > MAX_LISTED:
        raise InvalidRequest(f"limit is 1-{MAX_LISTED}")
    # One past the limit, to tell a last full page from one with more after it.
    rows = conn.execute(
        f"SELECT p.path, {REVISIONS.described} FROM wiki_pages p {CURRENT}"
        f" WHERE p.path > :after{scope} ORDER BY p.path LIMIT :limit",
        {**values, "after": after or "", "limit": limit + 1},
    ).fetchall()
    shown = rows[:limit]
    return {
        "pages": [
            {
                "path": row["path"],
                "current_revision": row["revision"],
                "at": row["at"],
                "size": row["size"],
                "author": revisions.author(row),
            }
            for row in shown
        ],
        "next_after": shown[-1]["path"] if len(rows) > limit else None,
    }
