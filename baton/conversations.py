"""Shared conversation queries and compatibility API for group conversations.

Behavior for one thread lives in :mod:`baton.conversation`; this module retains
listing, query/private helpers, and public adapters with their established signatures.

A titled thread whose every message reaches each other participant.

A conversation send stores one copy per other participant, each as that
recipient's next mailbox seq and all under one shared message_id, in one
transaction: every recipient gets its copy or none does. Each copy keeps what
a direct message guarantees: the reader's gap-free seq and cursors, and coalesced
wakes. The sender gets no copy, so it is never woken by its own
message.

`history` is separate from that delivery: it returns the conversation as a
participant has seen it, its own messages included, and moves no bookmark.

A message may answer another through `reply_to`, and may single out the
participants it asks to look at it through `attention`, or ask the
conversation for a decision through `needs_decision`. Those three are what a
reader and a quiet role's wake policy go on; none of them is an approval, and
naming a role neither adds it to the conversation nor speaks for it.

A conversation may carry an optional `topic`, a readable path of one or
two lowercase slug segments. It is a label for people, kept apart from
the conversation's id, so renaming it moves nothing; listing filters by
it a whole segment at a time.

The config says which roles may create conversations and manage
participants. A message's author is always the sender, the role its
credential is bound to. Words the sender passes on from someone else travel
in `relayed`, apart from its own text, and are never their authenticated
authorship. Messages are information, never authority: nothing here touches
a job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from baton import board_read, changes, events, messages, topic_grants, topics
from baton.config import Config, Conversations
from baton.database import Connection, Row
from baton.errors import (
    ConversationFull,
    ConversationsDisabled,
    Forbidden,
    InvalidRequest,
    LimitExceeded,
    NotAParticipant,
    UnknownConversation,
    UnknownRole,
)
from baton.topic_paths import MAX_TOPIC_SEGMENT as MAX_TOPIC_SEGMENT
from baton.topic_paths import MAX_TOPIC_SEGMENTS as MAX_TOPIC_SEGMENTS
from baton.topic_paths import TOPIC_SEGMENT as TOPIC_SEGMENT
from baton.topic_paths import check_topic as check_topic

MAX_TITLE = 200
MIN_PARTICIPANTS = 2
RATE_WINDOW = timedelta(hours=1)
# The most conversations one list_for or list_all call returns.
MAX_LISTED = 200


def set_topic(conn: Connection, config: Config, conversation_id: str, topic: Any, *,
              actor: str, identity: dict[str, str] | None = None,
              seen: dict[str, Any] | None = None, grant_key: str | None = None) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation(conn, conversation_id).set_topic(config, topic, actor=actor, identity=identity,
                                                           seen=seen, grant_key=grant_key)


def _authorize_move(conn: Connection, actor: str, conversation_id: str, before: str | None, after: str | None,
                    identity: dict[str, str] | None, grant_key: str | None) -> Row | None:
    """A core role must be in the thread. A guest needs the grant its key opens to cover both ends of the move.

    Both ends, because the source is as much a claim as the destination: a grant on the destination alone would let
    its holder pull any thread in, or one on the source alone push threads out of its reach. A thread with no
    topic, or a label with no topic row, covers nothing.
    """
    if grant_key is None:
        if actor not in _participants(conn, conversation_id):
            raise NotAParticipant(f"{actor} is not in {conversation_id}")
        return None
    grant = topic_grants.grant_for_use(conn, identity, grant_key)
    # The refusal does not name the source: the caller never supplied it and may not be able to read the thread.
    topic_grants.require_covers(conn, grant, "thread.organise", before, shown="this thread's current topic")
    topic_grants.require_covers(conn, grant, "thread.organise", after)
    return grant


def _tell_of_move(conn: Connection, conversation_id: str, grant: Row, identity: dict[str, str],
                  who: str, before: str | None, after: str | None) -> None:
    """A host notice to every participant that a grantee moved their thread, and under which grant."""
    notice = {"event": "topic_moved", "from": before, "to": after, "by": who, "alias": identity["alias"],
              "alias_code": identity["code"], "grant_id": grant["id"]}
    text = (f"alias {identity['alias']} ({identity['code']}) moved this thread from {before} to {after} "
            f"under topic grant {grant['id']}")
    _fan_out(conn, conversation_id, _participants(conn, conversation_id), sender=None, text=text, notice=notice)


def require_enabled(config: Config) -> Conversations:
    if not config.conversations.enabled:
        raise ConversationsDisabled("this host has group conversations turned off ([conversations] enabled)")
    return config.conversations


def create(conn: Connection, config: Config, *, actor: str, title: str,
           participants: list[str] | None = None, topic: Any = None, request_id: str | None = None,
           identity: dict[str, str] | None = None, seen: dict[str, Any] | None = None) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation.create(conn, config, actor=actor, title=title, participants=participants, topic=topic,
                              request_id=request_id, identity=identity, seen=seen)


def _thread_cap_refusal(conn: Connection, config: Config,
                        identity: dict[str, str]) -> LimitExceeded | None:
    """Whether this guest thread is over either hourly cap, counted inside the caller's write lock.

    Both windows are counted from `conversations` itself, so there is no second table to keep in step,
    and the write lock the transaction already holds is what stops two creates both slipping under a
    cap. A refusal is returned, not raised: the guest-wide case also stores an alert that has to be
    committed by this transaction rather than rolled back by the exception.
    """
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    settings = config.aliases
    mine = conn.execute("SELECT COUNT(*) FROM conversations WHERE created_by_alias_id = ? AND created_at > ?",
                        (identity["alias_id"], since)).fetchone()[0]
    everyone = conn.execute("SELECT COUNT(*) FROM conversations WHERE created_by = 'guest' AND created_at > ?",
                            (since,)).fetchone()[0]
    over_alias = mine >= settings.max_threads_per_alias_per_hour
    over_guests = everyone >= settings.max_threads_per_guest_per_hour
    if not over_alias and not over_guests:
        return None
    reached = []
    if over_alias:
        reached.append(f"{settings.max_threads_per_alias_per_hour} for this alias")
    if over_guests:
        reached.append(f"{settings.max_threads_per_guest_per_hour} across all guests")
        _alert_guest_cap(conn, config)
    return LimitExceeded("new guest threads are over their hourly limit: " + " and ".join(reached),
                         "threads_per_guest" if over_guests else "threads_per_alias")


def _alert_guest_cap(conn: Connection, config: Config) -> None:
    """Tell the alert role the guest-wide cap filled, at most once an hour.

    The timestamp is persisted in the same transaction as the refusal, so a restart cannot turn one
    alert into a flood, and a full mailbox leaves it unset so the next refusal tries again rather than
    the hour passing in silence. Nothing is stored when no alert role is configured.
    """
    role = config.aliases.guest_cap_alert_role
    if role is None:
        return
    now = datetime.now(UTC)
    row = conn.execute("SELECT value FROM log_meta WHERE key = 'guest_thread_cap_alerted_at'").fetchone()
    if row is not None and row["value"] > (now - RATE_WINDOW).isoformat():
        return
    if messages.unread(conn, role) >= messages.MAX_UNREAD:
        return  # Storing would raise MailboxFull and lose the refusal; the timestamp stays unset.
    limit = config.aliases.max_threads_per_guest_per_hour
    conn.execute("INSERT INTO log_meta (key, value) VALUES ('guest_thread_cap_alerted_at', ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (now.isoformat(),))
    messages.store_copy(conn, message_id=messages.new_message_id(), sender=None, recipient=role,
                        text=f"Guest threads reached their hourly limit of {limit} across all guests. "
                             "Further guest threads are refused until the window moves.",
                        notice={"event": "guest_threads.cap_reached", "limit": limit, "window": "1h"})


def _admit_guest_thread(config: Config, identity: dict[str, str] | None) -> None:
    """A guest needs a live alias binding, and a guest role that already reads the board.

    The read requirement is what keeps this from widening anything: starting a thread never grants a
    guest sight of a conversation it could not already see.
    """
    if not identity or not identity.get("alias_id"):
        raise Forbidden("alias binding is missing, invalid or expired")
    role = config.roles.get("guest")
    if role is None or not role.can_view_all_conversations:
        raise Forbidden("this host's guests do not read board conversations "
                        "(roles.guest.can_view_all_conversations), so they cannot start a thread either")


def list_for(conn: Connection, role: str, topic: Any = None, *, limit: int = MAX_LISTED,
             before: tuple[str, str] | None = None, changed_since: str | None = None,
             _lookahead: bool = False) -> list[dict[str, Any]]:
    """The conversations `role` takes part in, newest first, with their participants.

    `topic` narrows them to that topic and anything under it. It never widens what the role may see: a
    conversation it is not in stays out whether it has a topic or not. One call returns at most `limit`;
    to read on, pass the last one's `page_cursor(...)` as `before`. `changed_since`, an ISO time, keeps only
    those with a message after it: a discovery aid, not a guarantee of completeness.
    """
    where, values = _under(topic)
    after, cursor = _before(before, "c.")
    since, moment = ("", ()) if changed_since is None else (" AND COALESCE(c.last_activity_at, c.created_at) > ?",
                                                            (utc_iso(changed_since, "changed_since"),))
    rows = conn.execute("SELECT c.* FROM conversations c JOIN participants p ON p.conversation_id = c.id"
                        f" WHERE p.role = ?{where}{after}{since} ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                        (role, *values, *cursor, *moment,
                         _limit(limit + int(_lookahead), allow_lookahead=_lookahead))).fetchall()
    return [_summary(conn, row) for row in rows]


def list_all(conn: Connection, topic: Any = None, *, limit: int = MAX_LISTED,
             before: tuple[str, str] | None = None, changed_since: str | None = None,
             _lookahead: bool = False) -> list[dict[str, Any]]:
    """Every conversation, newest first, paged like `list_for`: only for a viewer the config lets see them all."""
    where, values = _under(topic)
    after, cursor = _before(before, "")
    since, moment = ("", ()) if changed_since is None else (" AND COALESCE(last_activity_at, created_at) > ?",
                                                            (utc_iso(changed_since, "changed_since"),))
    rows = conn.execute(f"SELECT * FROM conversations WHERE 1 = 1{where}{after}{since}"
                        " ORDER BY created_at DESC, id DESC LIMIT ?",
                        (*values, *cursor, *moment,
                         _limit(limit + int(_lookahead), allow_lookahead=_lookahead))).fetchall()
    return [_summary(conn, row) for row in rows]


def page_cursor(summary: dict[str, Any]) -> tuple[str, str]:
    """Where the next page of `list_for` or `list_all` starts: after this conversation."""
    return summary["created_at"], summary["conversation_id"]


def list_readable(conn: Connection, config: Config, role: str, topic: Any = None, *,
                  limit: int = MAX_LISTED, before: tuple[str, str] | None = None,
                  changed_since: str | None = None, _lookahead: bool = False,
                  seen: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List the conversations this role may read under the shared board predicate.

    The topic's id and the listing are read in one snapshot, so a rename and reuse of the path in between
    cannot make the listing describe a different topic from the one the audit line names.
    """
    with conn.snapshot():
        topics.note_path(conn, seen, check_topic(topic))
        if board_read.sees_all(config, role):
            return list_all(conn, topic, limit=limit, before=before, changed_since=changed_since,
                            _lookahead=_lookahead)
        return list_for(conn, role, topic, limit=limit, before=before, changed_since=changed_since,
                        _lookahead=_lookahead)


def list_readable_page(conn: Connection, config: Config, role: str, topic: Any = None, *,
                       limit: int = MAX_LISTED, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
    """Return the authorized total and one numbered page in a single read snapshot.

    This is for views whose page number is part of the link, rather than a cursor. The count and
    rows use the same board-read rule and topic filter; the page query never materializes earlier
    conversations just to skip them.
    """
    where, values = _under(topic)
    limit = _limit(limit)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRequest("offset must be a non-negative integer")
    with conn.snapshot():
        if board_read.sees_all(config, role):
            total = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE 1 = 1{where}", values).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM conversations WHERE 1 = 1{where} "
                                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                                (*values, limit, offset)).fetchall()
        else:
            # participants has one row per (conversation, role), so this join keeps count and page at conversation
            # grain while enforcing the same membership boundary as list_for.
            total = conn.execute("SELECT COUNT(*) FROM conversations c JOIN participants p "
                                 "ON p.conversation_id = c.id WHERE p.role = ?" + where,
                                 (role, *values)).fetchone()[0]
            rows = conn.execute("SELECT c.* FROM conversations c JOIN participants p "
                                "ON p.conversation_id = c.id WHERE p.role = ?" + where +
                                " ORDER BY c.created_at DESC, c.id DESC LIMIT ? OFFSET ?",
                                (role, *values, limit, offset)).fetchall()
        return total, [_summary(conn, row) for row in rows]


def readable_summary(conn: Connection, config: Config, role: str,
                     conversation_id: str) -> dict[str, Any] | None:
    """Return one conversation's summary only if the same board-read rule permits it."""
    with conn.snapshot():
        if not board_read.may_read_conversation(conn, config, role, conversation_id):
            return None
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return None if row is None else _summary(conn, row)


def attention_signals(conn: Connection, config: Config, viewer: str, subject: str,
                      topic: Any = None, *, human_replies_only: bool = False
                      ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]],
                                 list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    """Collect and classify retained attention/decision messages readable by `viewer`.

    `subject` filters signals but never grants access. A projected role's replies can be limited to
    person-authored messages; a human seat always requests that restriction for its own replies.
    """
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    replied: set[str] = set()
    before = None
    while True:
        listed = list_readable(conn, config, viewer, topic, before=before)
        for conversation in listed:
            after = 0
            while True:
                page = history(conn, viewer, conversation["conversation_id"], after,
                               messages.MAX_PAGE_MESSAGES, config=config)
                entries = page["messages"]
                found.extend((conversation, item) for item in entries
                             if item["from_role"] not in (None, viewer, subject)
                             and (item.get("needs_decision") or subject in (item.get("attention") or [])))
                replied.update(item["reply_to"]["message_id"] for item in entries
                               if item["from_role"] == subject and item.get("reply_to")
                               and (not human_replies_only or item.get("author_kind") == "human"))
                if not entries:
                    break
                after = page["next_seq"]
        if len(listed) < MAX_LISTED:
            break
        before = page_cursor(listed[-1])
    found.sort(key=lambda pair: (pair[1]["at"], pair[1]["seq"]), reverse=True)
    mine = [pair for pair in found if subject in (pair[1].get("attention") or [])]
    others = [pair for pair in found if subject not in (pair[1].get("attention") or [])]
    return mine, others, replied


def _before(before: tuple[str, str] | None, alias: str) -> tuple[str, tuple[str, ...]]:
    if before is None:
        return "", ()
    return f" AND ({alias}created_at, {alias}id) < (?, ?)", (str(before[0]), str(before[1]))


def _limit(limit: int, *, allow_lookahead: bool = False) -> int:
    maximum = MAX_LISTED + int(allow_lookahead)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise InvalidRequest(f"limit must be 1-{MAX_LISTED}")
    return limit


def _under(topic: Any) -> tuple[str, tuple[str, ...]]:
    """A filter for a topic and everything under it, matching whole segments only.

    `project` takes `project` and `project/anything`, and never `projectile`, because the only other
    match is on a full segment boundary. The topic is validated first, so it carries no LIKE
    metacharacter and needs no escaping.
    """
    topic = check_topic(topic)
    if topic is None:
        return "", ()
    return " AND (topic = ? OR topic LIKE ?)", (topic, f"{topic}/%")


def add(conn: Connection, config: Config, conversation_id: str, participant: str, *, actor: str) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation(conn, conversation_id).add(config, participant, actor=actor)


def remove(conn: Connection, config: Config, conversation_id: str, participant: str, *, actor: str) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation(conn, conversation_id).remove(config, participant, actor=actor)


def send(conn: Connection, config: Config, *, sender: str, conversation_id: str, text: str,
         relayed: Any = None, request_id: str | None = None, job_id: str | None = None,
         reply_to: str | None = None, attention: Any = None, needs_decision: bool = False,
         identity: dict[str, str] | None = None) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation(conn, conversation_id).send(config, sender=sender, text=text, relayed=relayed,
                                                    request_id=request_id, job_id=job_id, reply_to=reply_to,
                                                    attention=attention, needs_decision=needs_decision,
                                                    identity=identity)


def history(conn: Connection, role: str, conversation_id: str, after_seq: int = 0,
            limit: int = 50, before_seq: int | None = None, config: Config | None = None) -> dict[str, Any]:
    from baton.conversation import Conversation

    return Conversation(conn, conversation_id).history(role, after_seq=after_seq, limit=limit,
                                                       before_seq=before_seq, config=config)


def _access_start_seq(conn: Connection, conversation_id: str, role: str,
                      participant: Row | None, conversation: Row, view_all: bool) -> int | None:
    """Earliest sequence whose visibility is independently established, or None if unknown.

    A client cursor is not proof that a role could read earlier messages. For late joiners, the retained
    participant-added notice is the only sequence-level evidence available without a schema change.
    """
    if view_all or (participant is not None and participant["added_at"] == conversation["created_at"]):
        return 1
    if participant is None:
        return None
    rows = conn.execute("SELECT conversation_seq, notice FROM messages WHERE conversation_id = ? "
                        "AND recipient = ? AND notice IS NOT NULL AND at >= ? ORDER BY conversation_seq",
                        (conversation_id, role, participant["added_at"]))
    for row in rows:
        notice = json.loads(row["notice"])
        if notice.get("event") == "participant_added" and notice.get("participant") == role:
            return row["conversation_seq"]
    return None


def _has_readable_seq(conn: Connection, condition: str, values: dict[str, Any],
                      conversation_id: str, start: int, end: int) -> bool:
    if end < start:
        return False
    return conn.execute(f"SELECT 1 FROM messages m WHERE m.conversation_id = :conversation_id "
                        "AND m.conversation_seq BETWEEN :start_seq AND :end_seq "
                        f"AND {condition} LIMIT 1", {**values, "conversation_id": conversation_id,
                                                       "start_seq": start, "end_seq": end}).fetchone() is not None


def gap_words(gap: dict[str, int]) -> str:
    """What a page says where `read_history` reports a gap."""
    first, last = gap["from_seq"], gap["to_seq"]
    return (f"Message #{first} is" if first == last else f"Messages #{first}–#{last} are") + " no longer kept."


def _retention_gaps(page: list[dict[str, Any]], lower_seq: int | None, upper_seq: int | None,
                    access_start: int | None, joined_at: str | None) -> list[dict[str, int]]:
    """Report only missing ranges after access is independently established."""
    if not page:
        return []
    gaps = []
    first_seq = page[0]["seq"]
    if lower_seq is not None and first_seq > lower_seq:
        gaps.append({"from_seq": lower_seq, "to_seq": first_seq - 1})
    for previous, current in zip(page, page[1:], strict=False):
        start = previous["seq"] + 1
        if access_start is not None:
            start = max(start, access_start)
        elif joined_at is None or previous["at"] < joined_at:
            continue
        if current["seq"] > start:
            gaps.append({"from_seq": start, "to_seq": current["seq"] - 1})
    last = page[-1]
    start = last["seq"] + 1
    if access_start is not None:
        start = max(start, access_start)
    elif joined_at is None or last["at"] < joined_at:
        return gaps
    if upper_seq is not None and upper_seq >= start:
        gaps.append({"from_seq": start, "to_seq": upper_seq})
    return gaps


def _fan_out(conn: Connection, conversation_id: str, recipients: list[str], *, sender: str | None,
             text: str, relayed: list[dict] | None = None, notice: dict[str, str] | None = None,
             job_id: str | None = None, message_id: str | None = None, author_kind: str | None = None,
             reply_to: str | None = None, attention: list[str] | None = None,
             needs_decision: bool = False, identity: dict[str, str] | None = None) -> dict[str, int]:
    """Store one copy for each recipient under one message_id, or refuse before storing any."""
    recipients = [role for role in recipients if role != "guest"]
    full = [role for role in recipients if messages.unread(conn, role) >= messages.MAX_UNREAD]
    if full:
        raise ConversationFull(f"{', '.join(full)} has {messages.MAX_UNREAD} unread messages, so no participant "
                               "got this; retry after it reads")
    message_id = message_id or messages.new_message_id()
    conn.execute("UPDATE conversations SET last_seq = last_seq + 1, last_activity_at = ? WHERE id = ?",
                 (events.now(), conversation_id))
    conversation_seq = _load(conn, conversation_id)["last_seq"]
    changes.record(conn, conversation_id, "message_added", message_id=message_id)
    return {role: messages.store_copy(conn, message_id=message_id, sender=sender, recipient=role, text=text,
                                      job_id=job_id, conversation_id=conversation_id,
                                      conversation_seq=conversation_seq, relayed=relayed, notice=notice,
                                      author_kind=author_kind, reply_to=reply_to, attention=attention,
                                      needs_decision=needs_decision, identity=identity)
            for role in recipients}


def _sent_within_window(conn: Connection, conversation_id: str) -> int:
    """Messages participants sent to the conversation in the last hour; host notices don't count."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    return conn.execute("SELECT COUNT(DISTINCT id) FROM messages WHERE conversation_id = ? AND sender IS NOT NULL"
                        " AND at > ?", (conversation_id, since)).fetchone()[0]


def _escalate(conn: Connection, settings: Conversations, conversation: Row, sender: str) -> None:
    """Address one content-free event to the escalation role, unless one already went out within the window."""
    since = (datetime.now(UTC) - RATE_WINDOW).isoformat()
    if settings.escalation_role is None or (conversation["escalated_at"] or "") > since:
        return
    conn.execute("UPDATE conversations SET escalated_at = ? WHERE id = ?", (events.now(), conversation["id"]))
    events.append(conn, job_id=None, seq=0, actor=sender, audience=settings.escalation_role,
                  kind="conversation.rate_limited", summary=f"{conversation['id']} is over its hourly message limit",
                  data={"conversation_id": conversation["id"]})


def _may_manage(config: Config, actor: str, participant: str, *, allow_guest: bool = False) -> Conversations:
    settings = require_enabled(config)
    if not config.roles[actor].can_manage_participants:
        raise Forbidden(f"{actor} may not manage participants (roles.{actor}.can_manage_participants)")
    _check_roles(config, [participant], allow_guest=allow_guest)
    return settings


def _check_roles(config: Config, roles: Any, field: str = "participants", *,
                 allow_guest: bool = False) -> list[str]:
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise InvalidRequest(f"{field} must be a list of role names")
    unknown = [role for role in roles if role not in config.roles]
    if unknown:
        raise UnknownRole(f"no role {unknown[0]!r}; call whoami for the list")
    if "guest" in roles and not allow_guest:
        raise InvalidRequest("guest cannot be a conversation participant")
    if len(set(roles)) != len(roles):
        raise InvalidRequest(f"{field} lists a role twice")
    return roles


def _check_attention(config: Config, attention: Any) -> list[str] | None:
    """The participants a message asks to look at it: a structured list of roles, never @names in the text.

    Checked against the config here and against the conversation's current participants by `send`, so a
    message can neither single out a role that left nor reach one that was never in.
    """
    if attention is None:
        return None
    roles = _check_roles(config, attention, "attention")
    if not roles:
        raise InvalidRequest("attention must name at least one participant, or be left out")
    return roles


def _check_reply_to(conn: Connection, config: Config, conversation_id: str, sender: str,
                    reply_to: Any) -> None:
    """A reply answers a message of this conversation that the sender can see, and nothing else.

    Refusing a parent the sender cannot see keeps a reply from pointing at a message that was never
    the sender's to answer: one from before it joined, one from another conversation or none at all.
    """
    if reply_to is None:
        return
    condition, values = board_read.message_condition(config, sender)
    seen = isinstance(reply_to, str) and conn.execute(
        f"SELECT 1 FROM messages m WHERE m.conversation_id = :conversation_id AND m.id = :reply_to "
        f"AND {condition} LIMIT 1", {**values, "conversation_id": conversation_id, "reply_to": reply_to}
    ).fetchone() is not None
    if not seen:
        raise InvalidRequest(f"reply_to must be the message_id of a message in {conversation_id} that you can "
                             "see; this host has no such message for you")


def _load(conn: Connection, conversation_id: str) -> Row:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise UnknownConversation(conversation_id)
    return row


def _participants(conn: Connection, conversation_id: str) -> list[str]:
    rows = conn.execute("SELECT role FROM participants WHERE conversation_id = ? ORDER BY role", (conversation_id,))
    return [row["role"] for row in rows]


def _history_entry(row: Row, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """A message as history shows it: numbered in the conversation, not in any one mailbox."""
    return {**messages.as_dict(row, reply_to), "seq": row["conversation_seq"]}


def _summary(conn: Connection, row: Row) -> dict[str, Any]:
    # The alias pair appears only on a thread a guest started, the way a guest post's attribution does,
    # so a role's thread reads exactly as it did before.
    alias = ({"created_by_alias": row["created_by_alias"], "created_by_alias_code": row["created_by_alias_code"]}
             if row["created_by_alias_id"] else {})
    return {"conversation_id": row["id"], "title": row["title"], "topic": row["topic"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "participants": _participants(conn, row["id"]), "revision": row["revision"],
            "last_seq": row["last_seq"], "last_activity_at": row["last_activity_at"] or row["created_at"],
            **alias}


def utc_iso(moment: str, name: str) -> str:
    """A time to compare with stored times, in the form they are stored in: UTC `isoformat()`.

    Stored times all come from `datetime.now(UTC).isoformat()`, so once a given time is converted to UTC and
    written the same way, comparing the strings compares the instants. One in another offset is converted;
    one without an offset, or that doesn't parse, is refused.
    """
    try:
        parsed = datetime.fromisoformat(str(moment))
    except ValueError:
        raise InvalidRequest(f"{name} must be an ISO 8601 time with its UTC offset") from None
    if parsed.tzinfo is None:
        raise InvalidRequest(f"{name} must carry its UTC offset")
    return parsed.astimezone(UTC).isoformat()
