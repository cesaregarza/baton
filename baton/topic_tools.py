"""The topic tools' bodies, kept apart from the rest of the tool set so `baton.app` stays one screen of wiring.

`Tools` inherits them. Each reaches the host's configuration and its database only through `self._config` and
`self._conn()`, which `Tools` provides.

There is no `from __future__ import annotations` here, as in `baton.app`: the SDK reads a tool's annotations through
the wrapper `Guard` makes, whose namespace is not this module's, so they must already be evaluated.
"""

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from baton import conversations, topic_grants, topics

# Declared so it is in the tool's schema and the SDK passes it on. `Guard` takes it out of the arguments, so no body
# receives it: a body reads the key from `call["grant_key"]`, through `_under_grant`.
GRANT_TOKEN = Annotated[str | None, Field(description=(
    "The topic grant key, for a guest that holds a topic grant: the key the owner gave you when the grant was "
    "authorized, sent with your binding `token`. A core role needs none and any it sends is ignored."))]


class TopicTools:
    """create_topic, update_topic, rename_topic, list_topics, get_topic and set_topic."""

    def _under_grant(self, tool: str, role: str, identity: dict[str, str] | None, call: dict[str, Any] | None,
                     operate: Callable[[Any, str | None], dict[str, Any]]) -> dict[str, Any]:
        """Run `operate(conn, grant_key)`, with the key only if the caller is a guest.

        A core role's key is ignored: it acts on its flags. A refusal that names a grant is logged on that grant
        once the operation has rolled back, then raised again.
        """
        key = (call or {}).get("grant_key") if role == "guest" else None
        with self._conn() as conn:
            try:
                return operate(conn, key)
            except topic_grants.GrantRefused as refusal:
                topic_grants.record_refused_use(conn, refusal, topics.history_actor(role, identity), tool)
                raise

    def create_topic(
        self, role: str,
        path: Annotated[str, Field(description="1-2 lowercase slug segments, such as project or "
                                               "project/subtopic. A subtopic needs its parent first.")],
        title: str | None = None,
        description: str | None = None,
        members: Annotated[list[str] | None, Field(
            description="The roles a guest's thread in this topic is delivered to, copied into each new "
                        "thread. Left out means none, so no guest thread until a core role sets them.")] = None,
        request_id: str | None = None,
        grant_token: GRANT_TOKEN = None,
        identity: dict[str, str] | None = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a topic for threads to be filed in. Pass a request_id to make a retry safe.

        A guest holding a topic grant with topic.create_sub may open a subtopic directly under the grant's own
        topic, with its binding `token` and `grant_token`. It takes its parent's members and may not set them.
        """
        return self._under_grant("create_topic", role, identity, call, lambda conn, key: topics.create(
            conn, self._config, actor=role, path=path, title=title, description=description, members=members,
            request_id=request_id, identity=identity, seen=topics.seen_by(call), grant_key=key))

    def update_topic(
        self, role: str, path: str, title: str | None = None,
        description: Annotated[str | None, Field(description="Left out or null keeps the current one.")] = None,
        members: Annotated[list[str] | None, Field(
            description="Replaces the members. Left out or null keeps the current list, and an empty "
                        "list clears it. Threads that already exist keep the participants they "
                        "were created with.")] = None,
        identity: dict[str, str] | None = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Change a topic's title, description or members. An argument you leave out is left as it is."""
        with self._conn() as conn:
            return topics.update(conn, self._config, actor=role, path=path, title=title,
                                 description=topics.UNSET if description is None else description,
                                 members=topics.UNSET if members is None else members, identity=identity,
                                 seen=topics.seen_by(call))

    def rename_topic(
        self, role: str, path: str, new_path: str,
        acknowledge_topic_grants: Annotated[list[str] | None, Field(
            description="The ids of the active topic grants this rename changes the reach of. If any are "
                        "affected, name exactly those ids; the refusal lists them. Otherwise leave it out.")] = None,
        identity: dict[str, str] | None = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move a topic, its subtopics and their threads to a new path. Ids and participants never change."""
        with self._conn() as conn:
            return topics.rename(conn, self._config, actor=role, path=path, new_path=new_path, identity=identity,
                                 seen=topics.seen_by(call), acknowledge_topic_grants=acknowledge_topic_grants)

    def list_topics(
        self, role: str,
        parent: Annotated[str | None, Field(
            description="Only the subtopics directly under this path. Omit it for every topic.")] = None,
    ) -> dict[str, Any]:
        """The topics, each with the number of threads in it you can read and when it last had one."""
        with self._conn() as conn:
            return {"topics": topics.listing(conn, self._config, reader=role, parent=parent)}

    def get_topic(self, role: str, path: str, call: dict[str, Any] | None = None) -> dict[str, Any]:
        """One topic: its title, description, members, subtopics and the threads in it you can read."""
        with self._conn() as conn:
            return topics.get(conn, self._config, reader=role, path=path, seen=topics.seen_by(call))

    def set_topic(
        self, role: str, conversation_id: str,
        topic: Annotated[str | None, Field(
            description="The new topic, 1-2 lowercase slug segments, or null to clear it. Required: say "
                        "what the topic becomes, so that forgetting the argument cannot clear one.")],
        grant_token: GRANT_TOKEN = None,
        identity: dict[str, str] | None = None,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set, rename or clear a conversation's topic. Its id never changes, so nothing else moves.

        A guest holding a topic grant with thread.organise may move a thread between topics the grant covers,
        with its binding `token` and `grant_token`; the thread's participants are told. It cannot clear a topic.

        `topic` has no default on purpose. A tool that cleared the topic when an argument was left out
        would make an omission destructive, and it would let any caller that drops nulls clear one by
        accident -- the remote command does drop them, which is why it sends this argument explicitly.
        """
        return self._under_grant("set_topic", role, identity, call, lambda conn, key: conversations.set_topic(
            conn, self._config, conversation_id, topic, actor=role, identity=identity, seen=topics.seen_by(call),
            grant_key=key))
