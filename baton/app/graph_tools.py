"""MCP adapters for source-bound graph reads; `baton.graph.reader` owns authorization and paging.

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
    """Read-only graph tools. Every page re-authorizes against current source visibility; nothing here writes."""

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
