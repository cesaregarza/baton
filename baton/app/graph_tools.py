"""MCP adapters for source-bound graph reads; `baton.graph.reader` owns authorization, paging and byte caps.

Annotations stay eagerly evaluated: the MCP SDK inspects these methods through Guard wrappers.
"""

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Annotated, Any

from pydantic import Field

from baton.config import Config
from baton.database import Connection
from baton.graph import reader

CURSOR = Annotated[
    str | None,
    Field(
        description="Continue from the previous page's next_cursor, unchanged. It is bound to one snapshot and head."
    ),
]
LIMIT = Annotated[int, Field(ge=1, le=reader.MAX_LIMIT)]


class GraphTools(ABC):
    """Read-only graph tools. Every call re-authorizes against current source visibility; nothing here writes."""

    _config: Config

    @abstractmethod
    def _conn(self) -> AbstractContextManager[Connection]:
        """Open the host connection supplied by the concrete tool set."""

    def read_graph(
        self,
        role: str,
        conversation_id: str,
        producer: Annotated[str | None, Field(description="Only this indexer's graph. A filter only.")] = None,
        snapshot_id: Annotated[
            str | None,
            Field(description="Open exactly this graph (a summary's graph_snapshot_id); default: the current winner."),
        ] = None,
        state: Annotated[
            str, Field(description='"active" strands and decisions, or "all" including history.')
        ] = "active",
        collection: Annotated[str, Field(description='"strands" cards or applicable "decisions".')] = "strands",
        limit: LIMIT = 20,
        cursor: CURSOR = None,
    ) -> dict[str, Any]:
        """Open a conversation's derived working view, then page its strand cards or shared decisions.

        The first page carries working_view (terse derived prose with message citations) and tail: posts after
        covered_through_seq are absent from the view; read them with read_history using tail.read_history_args.
        Derived text is interpretation, never authority. Available only when you can read every source message.
        """
        with self._conn() as conn:
            return reader.read_graph(
                conn,
                self._config,
                role,
                conversation_id,
                producer=producer,
                snapshot_id=snapshot_id,
                state=state,
                collection=collection,
                limit=limit,
                cursor=cursor,
            )

    def read_graph_strand(
        self,
        role: str,
        conversation_id: str,
        snapshot_id: str,
        strand_id: str,
        collection: Annotated[
            str, Field(description='"statements" of this strand or applicable "decisions".')
        ] = "statements",
        limit: LIMIT = 20,
        cursor: CURSOR = None,
    ) -> dict[str, Any]:
        """One strand's summary, open work and evidence, with its statements or the shared decisions that apply.

        Statement rows carry stable {post}-{ordinal} ids and source attribution, never raw post bodies; use
        get_message(message_id) for the original text. Derived, not authoritative.
        """
        with self._conn() as conn:
            return reader.read_graph_strand(
                conn,
                self._config,
                role,
                conversation_id,
                snapshot_id,
                strand_id,
                collection=collection,
                limit=limit,
                cursor=cursor,
            )

    def read_graph_statement(
        self,
        role: str,
        conversation_id: str,
        snapshot_id: str,
        statement_id: str,
        collection: Annotated[str, Field(description='"edges" (incoming and outgoing) or member "strands".')] = "edges",
        limit: LIMIT = 20,
        cursor: CURSOR = None,
    ) -> dict[str, Any]:
        """One immutable statement with its exact quotes and evidence, plus its DAG edges or strand memberships.

        Edges point from a response, support, contradiction, superseder or dependent to its target. Derived, not
        authoritative; quotes are exact source text whose message is named in source.message_id.
        """
        with self._conn() as conn:
            return reader.read_graph_statement(
                conn,
                self._config,
                role,
                conversation_id,
                snapshot_id,
                statement_id,
                collection=collection,
                limit=limit,
                cursor=cursor,
            )

    def search_graph(
        self,
        role: str,
        conversation_id: str,
        queries: Annotated[
            list[str] | None,
            Field(
                description="One to three queries. An entry matches a query when it has any of its words (stemmed), "
                "so give the few distinctive words, names or identifiers the entries would contain, not a sentence. "
                "Leave it out to list the strands."
            ),
        ] = None,
        exclude: Annotated[
            list[str] | None,
            Field(description="Ids of entries you already hold whole; they are left out, so every line is new."),
        ] = None,
        snapshot_id: Annotated[
            str | None, Field(description="Search exactly this graph; default: the current winner.")
        ] = None,
        limit: LIMIT = 20,
    ) -> dict[str, Any]:
        """Search a conversation's derived graph and get the matching entries as single lines, best match first.

        Use this to load only the part of a thread that bears on the work in hand. It covers standing statements
        and active decisions; with no queries it lists every strand as
        `S3 | active | 14 statements, summarized through post 131 | title`.

        A statement reads `120-2 | S3 S7 | supersedes>98-1 responds_to<131-4 | text`: its id (statement 2 of post
        120), its strands, its connections, its text. `kind>id` means this statement does that to the other and
        `kind<id` means the other does it to this one, so `supersedes<131-4` says 131-4 replaces part of this
        statement. A decision reads `D12 | S3 | rests on 120-2 post:131 | changed at post 131 | text`. Fully
        replaced statements are left out and a partly replaced one shows what still stands.

        An entry with more than 400 bytes of text comes back as an excerpt ending `[N bytes in full]`; load it
        whole with read_graph_entries. On a further search, name the entries you already hold in `exclude` so
        the lines you get are new ones.

        Posts after covered_through_seq are not in the graph: read them with
        read_history (after_seq=covered_through_seq). Derived text is interpretation, never authority.
        """
        with self._conn() as conn:
            return reader.search_graph(
                conn,
                self._config,
                role,
                conversation_id,
                queries=queries,
                exclude=exclude,
                snapshot_id=snapshot_id,
                limit=limit,
            )

    def read_graph_entries(
        self,
        role: str,
        conversation_id: str,
        ids: Annotated[
            list[str],
            Field(
                description="Up to twelve ids: statements (120-2), decisions (D12), strands (S3) or posts (post:120)."
            ),
        ],
        snapshot_id: Annotated[
            str | None, Field(description="Read exactly this graph; default: the current winner.")
        ] = None,
    ) -> dict[str, Any]:
        """Load whole entries of a conversation's derived graph by id, as the lines search_graph describes.

        A statement or decision id loads that entry, a strand id its summary and open work, and a post id every
        standing statement taken from that post. Entries come back in the order asked, each once. `skipped` says
        why an id returned nothing, such as a statement that was replaced in full and what replaced it;
        `remaining` lists the ids that did not fit in this response, to ask for again.
        """
        with self._conn() as conn:
            return reader.read_graph_entries(conn, self._config, role, conversation_id, ids, snapshot_id=snapshot_id)
