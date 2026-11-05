"""Historical schema upgrades for the SQLite store."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable

from .driver import Connection

SCHEMA_VERSION = 21
# The older versions `connect` upgrades in place, oldest first; any other is refused.
MIGRATES_FROM = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)


# The version-2 tables a migration rebuilds, with the columns they keep.
_REBUILT = {
    "messages": "id, job_id, sender, recipient, seq, text, at",
    "message_requests": "request_id, request_digest, message_id, seq, created_at",
}
# What each version above 3 adds, by table, and the data each fixes up, keyed by the version that
# introduced it. `_ensure_schema` applies every step above the database's own version, in order, so one
# entry here is all a later version needs. SQLite adds columns in place, so no rebuild is involved.
_ADDED_COLUMNS = {
    # A reply's parent, the roles a message singles out, and whether it asks for a decision.
    # No release ever created a version-4 database: the work that added these columns reached a release
    # already combined with version 5's. The step stays so that a data directory left by a build of that
    # work on its own is upgraded rather than refused.
    4: {"messages": ("reply_to TEXT", "attention TEXT", "needs_decision INTEGER NOT NULL DEFAULT 0")},
    # Whether a human or an agent wrote it, fixed when it was sent.
    5: {"messages": ("author_kind TEXT",)},
    # A conversation's optional readable topic. The first step to alter another table, which is why each
    # one names its own.
    6: {"conversations": ("topic TEXT",)},
    # What a reminder wake needs and an ordinary wake leaves NULL: when it becomes eligible, the read
    # position the batch had when its original wake was delivered, which of the two it is, and when it
    # first took a cap slot. Added, never rewritten, so an existing wake comes through untouched.
    7: {"wakes": ("due_at REAL", "baseline_seq INTEGER", "ordinal INTEGER", "claimed_at REAL")},
    # Each conversation's source revision, bumped with every change the change log records, and when it
    # last had a message.
    9: {"conversations": ("revision INTEGER NOT NULL DEFAULT 0", "last_activity_at TEXT")},
    # A generation's watermark, when it was switched to and from, and whether its owner dropped it.
    11: {
        "generations": (
            "watermark_epoch TEXT",
            "watermark_seq INTEGER",
            "activated_at TEXT",
            "deactivated_at TEXT",
            "dropped_at TEXT",
            "last_write_at TEXT",
        )
    },
    13: {"messages": ("alias_id TEXT REFERENCES aliases(id)", "alias TEXT", "alias_code TEXT")},
    # Who started a guest thread, recorded the way a guest post is attributed. A thread a role started
    # leaves all three NULL.
    15: {
        "conversations": (
            "created_by_alias_id TEXT REFERENCES aliases(id)",
            "created_by_alias TEXT",
            "created_by_alias_code TEXT",
        )
    },
    # A topic's stable id, beside its path. SQLite cannot add a UNIQUE or NOT NULL column in place, so the
    # column is added bare here, and `_give_topic_ids` gives every existing topic its own id and the index.
    16: {"topics": ("id TEXT",)},
    # When and how a seat row ended. A row that ends is kept, and every check refuses it by this state.
    19: {
        table: ("ended_at REAL", "end_reason TEXT")
        for table in ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests")
    },
    # Each binding's place in its alias's issuance order, which picks the one a claim at the cap replaces.
    20: {"alias_bindings": ("issued INTEGER",)},
}
_BACKFILL = {
    # Before version 5 no role could be human, so every message a role sent was an agent's.
    5: ("UPDATE messages SET author_kind = 'agent' WHERE sender IS NOT NULL",),
    # Revisions start at 0 for every existing conversation; activity is the newest message still held.
    9: (
        "UPDATE conversations SET last_activity_at = COALESCE((SELECT MAX(at) FROM messages m"
        " WHERE m.conversation_id = conversations.id), created_at)",
    ),
    # A version-10 producer's one generation was active from creation.
    11: (
        "UPDATE generations SET activated_at = created_at WHERE active = 1 AND activated_at IS NULL",
        "UPDATE generations SET last_write_at = created_at WHERE last_write_at IS NULL",
    ),
}
# Tables a version adds whole. `_SCHEMA` creates them (IF NOT EXISTS) during initialization, so an upgrade needs no
# statement of its own for them; naming them here makes the version a step, and the tests check each exists.
_ADDED_TABLES = {
    # The browser seat (`baton.seat`).
    8: ("seat_sessions", "seat_prelogin", "seat_receipts", "seat_requests"),
    # The conversation change log, with its epoch and the floor an earlier prune left (`baton.changes`). The
    # search index is not listed: it exists only where SQLite has FTS5.
    9: ("change_log", "log_meta"),
    # The local indexing writer's derived data (`baton.derived`). Its keyword index is not listed, for the
    # same reason as the messages'.
    10: (
        "generations",
        "batches",
        "batch_pages",
        "artifact_versions",
        "artifact_inputs",
        "artifact_depends_on",
        "commit_requests",
    ),
    # Vectors a producer supplies for a generation's artifacts and messages, and the items it skipped.
    11: ("vectors", "vector_skips"),
    12: ("aliases", "alias_bindings", "alias_claims"),
    13: ("alias_posts",),
    # Per-call identity (`baton.aliases`): owner arms, core enrolment and the current alias-to-role mapping.
    14: ("alias_arms", "core_enrolment", "alias_roles", "events_targets"),
    # Topics as containers (`baton.topics`).
    15: ("topics",),
    # Every change a topic undergoes, one row each (`baton.topics`).
    16: ("topic_changes",),
    # The owner's topic grant records and their event log (`baton.topics.grants`).
    17: ("topic_grants", "topic_grant_events"),
    # A topic's reference documents and every revision of them (`baton.references`).
    18: ("reference_docs", "reference_revisions"),
    # Records kept where something used to be overwritten or expired: each alias rekey and each MCP Events secret
    # rotation (neither with a secret), and each MCP Events wake's occurrence, outcomes and delivery receipts.
    19: ("alias_rekeys", "events_rotations", "events_occurrences", "events_outcomes", "events_receipts"),
    # Deliverable secretary summaries outlive each private secretary's working state.
    21: ("secretary_summaries",),
}


def _discover_topics(conn: Connection) -> None:
    """Record a topic for every label the conversations already carry, and for each missing parent.

    Data-driven rather than a fixed list, so a database holding labels this release never saw still
    upgrades. A stored label that fails the topic grammar stops the upgrade and names itself: dropping
    it would leave its threads pointing at a topic that does not exist, and the whole upgrade is one
    transaction, so refusing here leaves the old database exactly as it was.

    Members come from the threads filed directly under a path, never from a subtopic's, so a parent
    never gains a recipient who was not already in one of its own threads. A membership too small to
    receive a guest thread is stored empty instead, for a core role to set with `update_topic`; the
    checks that need the config -- a role it no longer has, or more members than it allows -- belong
    to admission, which can see it, and the schema stays independent of any one host's config.
    """
    from baton import conversations, events  # lazily: both import this module

    stored = [
        row[0]
        for row in conn.execute("SELECT DISTINCT topic FROM conversations WHERE topic IS NOT NULL ORDER BY topic")
    ]
    for label in stored:
        conversations.check_topic(label, field="the stored conversation topic")
    stamp = events.now()
    for path in sorted({part for label in stored for part in (label, label.split("/")[0])}):
        members = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT p.role FROM participants p JOIN conversations c ON c.id = p.conversation_id"
                " WHERE c.topic = ? AND p.role != 'guest' ORDER BY p.role",
                (path,),
            )
        ]
        if len(members) < conversations.MIN_PARTICIPANTS:
            members = []
        conn.execute(
            "INSERT OR IGNORE INTO topics (id, path, title, members, created_by, created_at)"
            " VALUES (?, ?, ?, ?, 'migration', ?)",
            (new_topic_id(), path, path.split("/")[-1], json.dumps(members), stamp),
        )


def new_topic_id() -> str:
    """A topic's id. Random, never derived from the path: a path a rename frees and a new topic reuses gets a new id."""
    return "topic_" + secrets.token_hex(8)


def _give_topic_ids(conn: Connection) -> None:
    """Give every topic that has no id its own, then make the id unique for every topic from here on.

    Each id is drawn in code, so no two topics can share one even on a database that began without the
    column. A fresh database declares the column unique and never runs this.
    """
    for (path,) in conn.execute("SELECT path FROM topics WHERE id IS NULL").fetchall():
        conn.execute("UPDATE topics SET id = ? WHERE path = ?", (new_topic_id(), path))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS topics_by_id ON topics (id)")


def _number_bindings(conn: Connection) -> None:
    """Give every carried-over binding its place in issuance order, which schema 19 did not record.

    Expiry is the nearest record of it, since each binding's lifetime was fixed when it was issued; rowid
    breaks ties, so the order is deterministic.
    """
    ordered = conn.execute("SELECT digest FROM alias_bindings ORDER BY expires_at, rowid").fetchall()
    conn.executemany(
        "UPDATE alias_bindings SET issued = ? WHERE digest = ?",
        [(issued, digest) for issued, (digest,) in enumerate(ordered, 1)],
    )


# Steps a plain statement cannot express, because they read the data and may refuse the upgrade.
_PROCEDURES: dict[int, tuple[Callable[[Connection], None], ...]] = {
    15: (_discover_topics,),
    16: (_give_topic_ids,),
    20: (_number_bindings,),
}
_STEPS = tuple(sorted(_ADDED_COLUMNS.keys() | _BACKFILL.keys() | _ADDED_TABLES.keys() | _PROCEDURES.keys()))
