"""MCP tool bodies and their argument schemas; domain operations keep their own rules.

Annotations stay eagerly evaluated: the MCP SDK inspects these methods through
Guard wrappers defined in another module.
"""

from contextlib import AbstractContextManager
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import Field

from baton import aliases, conversations, jobs, messages, topics
from baton import search as search_module
from baton.agents import Agent
from baton.app.reference_tools import ReferenceTools
from baton.app.topic_tools import TopicTools
from baton.config import Config
from baton.database import Connection
from baton.database.store import connection
from baton.errors import InvalidRequest
from baton.jobs import Job

MAX_WAIT_SECONDS = 30


def _conversation_cursor(cursor: dict[str, str] | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, dict)
        or set(cursor) != {"created_at", "conversation_id"}
        or not all(isinstance(value, str) and value for value in cursor.values())
    ):
        raise InvalidRequest("cursor must carry created_at and conversation_id from next_cursor")
    return cursor["created_at"], cursor["conversation_id"]


class Tools(TopicTools, ReferenceTools):
    """The tool bodies. `role` is the caller's; each call opens its own connection."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def whoami(self, role: str, call: dict[str, Any] | None = None) -> dict[str, Any]:
        """Your role, the other roles you can message, and the job states with who each waits on.

        `role` is the role this call runs as; `credential_role` is your login's, and `alias` and `code` name the
        alias whose token you passed, if any.
        """
        call = call or {"credential_role": role, "identity": None}
        identity = call["identity"] or {}
        return {
            "role": role,
            "credential_role": call["credential_role"],
            "effective_role": role,
            "alias": identity.get("alias"),
            "code": identity.get("code"),
            "roles": {name: entry.description for name, entry in self._config.roles.items()},
            "job_states": {
                name: {"waiting_on": state.waiting_on, "next": sorted(state.next)}
                for name, state in self._config.jobs.states.items()
            },
            "initial_job_state": self._config.jobs.initial,
            **(
                {"search": {"enabled": True, "keyword_index": self._keyword_mode()}}
                if self._config.search.enabled
                else {}
            ),
        }

    def bind_events(self, role: str, call: dict[str, Any]) -> dict[str, Any]:
        """Send this OAuth grant's event subscriptions the wakes of the role your token's alias is mapped to.

        Needs the token of an alias mapped to a role, on a guest OAuth login. A grant has one target: unbind it
        before binding another. Mapping changes and the grant's end drop it.
        """
        with self._conn() as conn:
            return aliases.bind_events(conn, call["authority"], call["identity"])

    def unbind_events(self, role: str, call: dict[str, Any]) -> dict[str, Any]:
        """Stop this grant's event subscriptions receiving a role's wakes. Needs the bound alias's token."""
        with self._conn() as conn:
            return aliases.unbind_events(conn, call["authority"], call["identity"])

    def alias_exists(self, role: str, name: str) -> dict[str, Any]:
        """Whether an exact normalized alias name is taken; reveals no owner, code or binding."""
        with self._conn() as conn:
            return {"exists": aliases.exists(conn, name)}

    def claim_alias(self, role: str, name: str, passphrase: str) -> dict[str, Any]:
        """Claim or renew a permanent alias with a passphrase; return a fresh expiring `binding_token`.

        Tokens work on any guest login. An alias enrolled as a role returns that `effective_role`, and its token
        makes a call run as the role; any other alias's names and codes grant no role or human authority.
        """
        with self._conn() as conn:
            return aliases.claim(conn, self._config, get_access_token(), name, passphrase)

    def send_message(
        self,
        role: str,
        text: str,
        to: str | None = None,
        conversation_id: Annotated[
            str | None,
            Field(description="Send to every other participant of this conversation instead of to one role."),
        ] = None,
        job_id: str | None = None,
        relayed: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Words you pass on from someone else, kept apart from your own text: a list of "
                "{source, at (ISO 8601 with offset), text}. They are never that person's authenticated "
                "authorship; the message is still yours."
            ),
        ] = None,
        reply_to: Annotated[
            str | None,
            Field(
                description="The message_id this message answers. Only in a conversation, and only a message you "
                "can see."
            ),
        ] = None,
        attention: Annotated[
            list[str] | None,
            Field(
                description="The participants you ask to look at this message. Only in a conversation, and only "
                "its current participants. It draws attention and nothing else: it never adds anyone "
                "to the conversation and never speaks for them."
            ),
        ] = None,
        identity: dict[str, str] | None = None,
        needs_decision: Annotated[
            bool,
            Field(
                description="Ask the conversation for a decision. A label for attention only: it is not an "
                "approval and changes no job."
            ),
        ] = False,
        request_id: Annotated[
            str | None,
            Field(
                description="YYYYMMDDTHHMMSSZ-<suffix>, its UTC creation time first. Reuse it to retry safely; "
                "omit it and the host makes one."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Send a message to one role (`to`) or a conversation (`conversation_id`), optionally about a job.

        Guests require a binding `token` and readable conversation, without direct recipient, job or decision.
        Returns its seq in the recipient's mailbox, or for a conversation each recipient's seq in `seqs`.
        A conversation message may answer another (`reply_to`) and may ask particular participants to look
        (`attention`, `needs_decision`); a direct message is already addressed to its recipient.
        """
        with self._conn() as conn:
            return Agent(conn, self._config, role).send(
                text,
                to=to,
                conversation_id=conversation_id,
                job_id=job_id,
                relayed=relayed,
                reply_to=reply_to,
                attention=attention,
                identity=identity,
                needs_decision=needs_decision,
                request_id=request_id,
            )

    def read_messages(
        self,
        role: str,
        after_seq: Annotated[int | None, Field(ge=0)] = None,
        limit: Annotated[int, Field(ge=1, le=messages.MAX_PAGE_MESSAGES)] = 50,
    ) -> dict[str, Any]:
        """Messages to you after `after_seq` (default: the ones not yet delivered), oldest first, with next_seq."""
        with self._conn() as conn:
            return Agent(conn, self._config, role).inbox.read(after_seq, limit)

    def create_conversation(
        self,
        role: str,
        title: str,
        participants: Annotated[
            list[str] | None,
            Field(
                description="The roles in the conversation, yourself included. A guest leaves this out: its "
                "thread goes to the topic's members."
            ),
        ] = None,
        topic: Annotated[
            str | None,
            Field(
                description="The topic to file this thread in: an existing path of 1-2 lowercase slug "
                "segments, such as project or project/subtopic. A core role may leave it out; a "
                "guest must give one. list_topics shows them."
            ),
        ] = None,
        request_id: str | None = None,
        identity: dict[str, str] | None = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a thread. Name `participants`, or as a guest give a `topic` and its members receive it."""
        with self._conn() as conn:
            return Agent(conn, self._config, role).create_thread(
                title,
                participants=participants,
                topic=topic,
                request_id=request_id,
                identity=identity,
                seen=topics.seen_by(call),
            )

    def list_conversations(
        self,
        role: str,
        topic: Annotated[
            str | None,
            Field(
                description="Only the conversations on this topic or under it: project takes project and "
                "project/subtopic, never projectile. Omit it for all of them, untagged "
                "included."
            ),
        ] = None,
        changed_since: Annotated[
            str | None,
            Field(
                description="Only conversations with a message after this ISO 8601 time (with its offset). A "
                "discovery aid: revision and last_seq are what tell you exactly what changed."
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=conversations.MAX_LISTED)] = conversations.MAX_LISTED,
        cursor: Annotated[
            dict[str, str] | None,
            Field(
                description="Continue after the previous page's next_cursor object, carrying created_at and "
                "conversation_id unchanged."
            ),
        ] = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return {conversations: page, next_cursor: cursor or null}; repeat with that cursor for the next page."""
        conversations.require_enabled(self._config)
        before = _conversation_cursor(cursor)
        with self._conn() as conn:
            rows = conversations.list_readable(
                conn,
                self._config,
                role,
                topic,
                limit=limit,
                before=before,
                changed_since=changed_since,
                _lookahead=True,
                seen=topics.seen_by(call),
            )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = conversations.page_cursor(page[-1]) if has_more and page else None
        return {
            "conversations": page,
            "next_cursor": ({"created_at": next_cursor[0], "conversation_id": next_cursor[1]} if next_cursor else None),
        }

    def read_history(
        self,
        role: str,
        conversation_id: str,
        after_seq: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=messages.MAX_PAGE_MESSAGES)] = 50,
        before_seq: Annotated[
            int | None,
            Field(
                ge=1,
                description="Read backwards: the messages just before this seq, still oldest first. Continue "
                "from the result's prev_seq.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Retained conversation messages readable by your role, numbered in conversation order.

        Keep next_seq locally for catch-up. This marks nothing read and moves no shared delivery cursor.
        """
        conversations.require_enabled(self._config)
        with self._conn() as conn:
            return Agent(conn, self._config, role).thread(conversation_id).read(after_seq, limit, before_seq=before_seq)

    def search(
        self,
        role: str,
        query: Annotated[
            str,
            Field(
                description=(
                    "Words to find; every word must appear. Plain words only, "
                    f"at most {search_module.MAX_TERMS} words and "
                    f"{search_module.MAX_QUERY} characters: a longer query is refused, never shortened."
                )
            ),
        ],
        conversation_id: str | None = None,
        topic: str | None = None,
        kinds: Annotated[
            list[str] | None,
            Field(
                description=(
                    'What to search: "message" (the default), and the derived kinds "summary", "decisions", '
                    '"open_questions" and "summary_recent". Any other kind is refused.'
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=search_module.MAX_HITS)] = 20,
        before: Annotated[
            str | None,
            Field(
                description=(
                    "An ISO 8601 time with its UTC offset (Z or +hh:mm), compared as an instant: only messages sent "
                    "before it, and only artifacts written before it "
                    "(when the artifact was made, not when the messages "
                    "it covers were sent)."
                )
            ),
        ] = None,
        producer: Annotated[str | None, Field(description="Only artifacts from this producer. A filter only.")] = None,
        query_vector: Annotated[
            list[float] | None,
            Field(
                description=(
                    "Optional, when embeddings are on: re-ranks the keyword matches you can read by similarity to this "
                    "vector. Keyword-first: it never adds a match."
                )
            ),
        ] = None,
        model: Annotated[str | None, Field(description="The embedding model query_vector was made with.")] = None,
    ) -> dict[str, Any]:
        """Messages, and derived artifacts if `kinds` asks for them, matching every word of `query`, best first.

        Searching marks nothing read and moves nothing. Hits never include a message you couldn't read with
        read_history or get_message, or an artifact made from one. Artifact hits are labelled derived and not
        authoritative.
        """
        with self._conn() as conn:
            return search_module.search(
                conn,
                self._config,
                role,
                query,
                conversation_id=conversation_id,
                topic=topic,
                kinds=kinds,
                limit=limit,
                before=before,
                producer=producer,
                query_vector=query_vector,
                model=model,
            )

    def read_artifacts(self, role: str, conversation_id: str, producer: str | None = None) -> dict[str, Any]:
        """A conversation's derived summaries, decisions and open questions that you can read: each only if you
        could read every message it was made from. They are derived readings, never authority; each says how
        far it covers. `producer` only filters which indexer's you see."""
        with self._conn() as conn:
            return search_module.read_artifacts(conn, self._config, role, conversation_id, producer=producer)

    def read_secretary_summaries(
        self,
        role: str,
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """Page your own archived secretary summaries, oldest first; continue with `next` as `after`."""
        from baton.agents.secretary.attention.archive import SummaryArchive

        with self._conn() as conn:
            return SummaryArchive(self._config).page_for(Agent(conn, self._config, role), after=after, limit=limit)

    def get_message(self, role: str, message_id: str) -> dict[str, Any]:
        """One message by its id, if you can read it: one you sent or hold, or any conversation message if
        your role may view them all. Unknown and not permitted give the same refusal."""
        with self._conn() as conn:
            return search_module.get_message(conn, self._config, role, message_id)

    def _keyword_mode(self) -> str:
        with self._conn() as conn:
            return search_module.keyword_mode(conn)

    def add_participant(self, role: str, conversation_id: str, participant: str) -> dict[str, Any]:
        """Add a role to a conversation; every participant gets a host notice. Adding one already in changes nothing."""
        with self._conn() as conn:
            return Agent(conn, self._config, role).thread(conversation_id).add_participant(participant)

    def remove_participant(self, role: str, conversation_id: str, participant: str) -> dict[str, Any]:
        """Remove a role from a conversation; it receives nothing more, and the others get a host notice."""
        with self._conn() as conn:
            return Agent(conn, self._config, role).thread(conversation_id).remove_participant(participant)

    def wait(self, role: str, timeout_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)] = 20) -> dict:
        """Block until messages or job changes are addressed to you, then return each exactly once."""
        with self._conn() as conn:
            return Agent(conn, self._config, role).inbox.wait(timeout_seconds=timeout_seconds)

    def create_job(self, role: str, title: str, body: str = "", request_id: str | None = None) -> dict[str, Any]:
        """Open a job in the initial state. Pass a request_id to make a retry safe."""
        with self._conn() as conn:
            return Job.create(
                conn,
                self._config.jobs,
                request_id=request_id or messages.new_request_id(),
                actor=role,
                title=title,
                body=body,
            )

    def move_job(
        self,
        role: str,
        job_id: str,
        to_state: str,
        note: str = "",
        request_id: Annotated[
            str | None,
            Field(
                description="YYYYMMDDTHHMMSSZ-<suffix>. Reuse it to retry: a retry returns the original outcome "
                "and moves nothing. Omit it and the host makes one."
            ),
        ] = None,
        expected_revision: Annotated[
            int | None,
            Field(
                ge=1,
                description="The job's revision when you read it. If its state has changed since, even back "
                "to the same state, the move is refused as stale_revision.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Move a job to one of its current state's next states, with an optional short note."""
        with self._conn() as conn:
            return Job(conn, job_id).move(
                self._config.jobs,
                to_state,
                actor=role,
                note=note,
                request_id=request_id,
                expected_revision=expected_revision,
            )

    def get_job(self, role: str, job_id: str) -> dict[str, Any]:
        """A job's title, body, state and who it waits on."""
        with self._conn() as conn:
            return Job(conn, job_id).read()

    def list_jobs(self, role: str, state: str | None = None, waiting_on: str | None = None) -> list[dict[str, Any]]:
        """Recent jobs, newest first, optionally filtered by state or by the role they wait on."""
        with self._conn() as conn:
            return jobs.list_jobs(conn, state=state, waiting_on=waiting_on)

    def read_job_events(
        self,
        role: str,
        job_id: str,
        after_seq: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=jobs.MAX_PAGE_EVENTS)] = 50,
    ) -> dict[str, Any]:
        """A job's history after `after_seq`, oldest first, with next_seq to continue from."""
        with self._conn() as conn:
            return Job(conn, job_id).read_events(after_seq, limit)

    def _conn(self) -> AbstractContextManager[Connection]:
        return connection(self._config.db_path)
