"""Shared setup for the wiki tests: a host with the wiki on, callers of each kind, and readers of the wiki's tables.

Everything a test does to the wiki goes over MCP in process, so a host without the wiki answers with a refusal the
test asserts on. The limits and refusal texts are the spec's, written out here, not read from the module under test.
Nothing secret is written here: passphrases and binding tokens are made at run time.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from conftest import Host, make_config
from topic_grant_tools_support import tool

from baton import auth
from baton.database import store

TOOLS = ("read_wiki", "list_wiki", "search_wiki", "wiki_history", "edit_wiki", "restore_wiki")
WRITES = ("edit_wiki", "restore_wiki")
TABLES = ("wiki_pages", "wiki_revisions")
MAX_BODY = 32768
MAX_PAGES = 10000
PASSPHRASE = "synthetic-" + secrets.token_urlsafe(12)
BINDING_GONE = "alias binding is missing, invalid or expired"
POSTS_OVER = "guest posting is over its hourly limit"
PATH_RULE = "must be 1-4 segments joined by /, each 1-40 characters of a-z, 0-9 and inner hyphens, such as ops/deploys"
EDIT_KEYS = {"path", "revision", "base_revision", "restored_from", "at", "size", "author", "request_id", "created"}
READ_KEYS = {"path", "current_revision", "revision", "base_revision", "restored_from", "at", "size", "author", "body"}
HISTORY_KEYS = {"path", "current_revision", "revisions", "next_revision"}
HISTORY_ENTRY_KEYS = {"revision", "base_revision", "restored_from", "at", "size", "author"}
LISTED_KEYS = {"path", "current_revision", "at", "size", "author"}
HIT_KEYS = {"path", "revision", "at", "author", "snippet"}


def stale(path: str, current: int, expected: int) -> str:
    return f"wiki page {path!r} is at revision {current}, not {expected}; read it again before editing it"


def no_page(path: str) -> str:
    return f"no wiki page {path!r}"


def no_revision(path: str, revision: int, current: int) -> str:
    return f"wiki page {path!r} has no revision {revision}; its current revision is {current}"


def conflict(request_id: str) -> str:
    return f"request_id {request_id} was already used with a different body"


def request_id() -> str:
    return "req-" + secrets.token_hex(4)


def wiki_host(tmp_path: Any, *, enabled: bool = True, posts_per_alias: int = 30) -> Host:
    """A host whose planner and worker are core roles with no flag at all, beside a guest login.

    The wiki is switched on in the TOML, as an operator would, so a host that does not know the section ignores it.
    """
    raw, digest = auth.new_token()
    extra = (
        '[roles.guest]\ndescription = "Shared login"\n'
        f"[aliases]\nmax_claims_per_alias_per_hour = 100\nmax_posts_per_alias_per_hour = {posts_per_alias}\n"
        "[conversations]\nenabled = true\n"
        + ("[wiki]\nenabled = true\n" if enabled else "")
        + f'[[auth.tokens]]\nname = "guest-token"\nrole = "guest"\nsha256 = "{digest}"\n'
    )
    host = make_config(tmp_path, extra=extra)
    host.tokens["guest"] = raw
    return host


@dataclass
class Caller:
    """A credential calling for itself, or the guest login with an alias's binding `token`."""

    host: Host
    role: str = "planner"
    token: str | None = None
    alias_id: str | None = None
    alias: str | None = None
    code: str | None = None

    async def call(self, client: Any, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        if self.token is not None:
            arguments = {**arguments, "token": self.token}
        return await tool(client, self.host, tool_name, arguments, role=self.role)

    @property
    def author(self) -> dict[str, str]:
        """The author block a revision this caller writes must carry."""
        if self.token is None:
            return {"role": self.role}
        assert self.alias_id and self.alias and self.code, self
        return {"role": "guest", "alias_id": self.alias_id, "alias": self.alias, "code": self.code}

    async def edit(self, client: Any, path: str, body: str, expected: int, **extra: Any) -> dict[str, Any]:
        return await self.call(client, "edit_wiki", path=path, body=body, expected_revision=expected, **extra)

    async def restore(self, client: Any, path: str, revision: int, expected: int, **extra: Any) -> dict[str, Any]:
        return await self.call(
            client, "restore_wiki", path=path, revision=revision, expected_revision=expected, **extra
        )


async def bound(client: Any, host: Host, alias: str = "lane-a") -> Caller:
    """A guest alias, claimed with its passphrase, holding a fresh binding token."""
    claimed = await tool(client, host, "claim_alias", {"name": alias, "passphrase": PASSPHRASE})
    assert claimed.get("binding_token"), claimed
    return Caller(host, "guest", claimed["binding_token"], claimed["alias_id"], claimed["alias"], claimed["code"])


def anonymous(host: Host) -> Caller:
    """The shared guest login with no binding token at all."""
    return Caller(host, "guest")


@contextmanager
def database(host: Host) -> Iterator[sqlite3.Connection]:
    conn = store.connect(host.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def require_tables(conn: Any) -> None:
    for name in TABLES:
        assert conn.table_exists(name), f"there is no {name} table"


def revisions_of(host: Host, path: str) -> list[sqlite3.Row]:
    with database(host) as conn:
        require_tables(conn)
        return conn.execute(
            "SELECT r.*, CAST(r.body AS BLOB) AS bytes, typeof(r.body) AS kind FROM wiki_revisions r"
            " JOIN wiki_pages p ON p.id = r.page_id WHERE p.path = ? ORDER BY r.revision",
            (path,),
        ).fetchall()


def counts(host: Host) -> dict[str, int]:
    """Row counts of every ordinary table, the keyword indexes' own tables left out."""
    with database(host) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                " AND name NOT LIKE '%\\_fts%' ESCAPE '\\' ORDER BY name"
            )
        ]
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}


def posts_counted(host: Host) -> int:
    with database(host) as conn:
        return conn.execute("SELECT COUNT(*) FROM alias_posts").fetchone()[0]


def refused_by_schema(result: dict[str, Any], argument: str) -> None:
    """The SDK refused the arguments before the tool ran: an error that names the argument."""
    text = str(result.get("unparsed", result))
    assert "validation error" in text and argument in text, result
