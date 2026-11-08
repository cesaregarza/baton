"""Base table and index declarations for the current database schema.

The store runtime executes these statements in order inside its schema transaction.
Version policy, upgrade steps, and optional FTS indexes remain in baton.database.store.
"""

# A message's retry identity is its own record, so a retry of its request still replays from here. A direct send
# records its `seq`; a conversation send records each recipient's in `seqs`.
_MESSAGE_REQUESTS = """CREATE TABLE IF NOT EXISTS message_requests (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER,
        seqs TEXT,
        created_at TEXT NOT NULL
    )"""

# One row per copy: `seq` numbers each recipient's messages without gaps, and
# a conversation message's copies share its `id` and its `conversation_seq`,
# the conversation's own gap-free order. A host notice has no sender. `author_kind` (`human` or
# `agent`, NULL for a host notice) is fixed when the message is sent, from the sender role's config.
_MESSAGES = """CREATE TABLE IF NOT EXISTS messages (
        id TEXT NOT NULL,
        job_id TEXT REFERENCES jobs(id),
        conversation_id TEXT REFERENCES conversations(id),
        conversation_seq INTEGER,
        sender TEXT,
        author_kind TEXT,
        alias_id TEXT REFERENCES aliases(id),
        alias TEXT,
        alias_code TEXT,
        recipient TEXT NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT NOT NULL,
        relayed TEXT,
        notice TEXT,
        reply_to TEXT,
        attention TEXT,
        needs_decision INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL,
        PRIMARY KEY (recipient, seq)
    )"""

# Created after the upgrade steps, not in `_SCHEMA`: on an older database the column arrives with step 20.
_BINDINGS_BY_ISSUE = "CREATE UNIQUE INDEX IF NOT EXISTS alias_bindings_by_issue ON alias_bindings (alias_id, issued)"

# One statement per entry: executescript() would commit on its own, outside
# the transaction that creates the schema.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    # Permanent ownership; only salted scrypt verifiers, never passphrases. The random id is attribution.
    """CREATE TABLE IF NOT EXISTS aliases (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        skeleton TEXT NOT NULL UNIQUE,
        verifier TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    # `issued` orders an alias's bindings oldest first. Neither rowid (a VACUUM may renumber it) nor expiry (a
    # lifetime change reorders it) is that order.
    """CREATE TABLE IF NOT EXISTS alias_bindings (
        digest TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        expires_at REAL NOT NULL,
        issued INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS alias_bindings_by_alias ON alias_bindings (alias_id, expires_at)",
    # Admitted attempts, successful or refused; bounded by the global hourly claim limit.
    """CREATE TABLE IF NOT EXISTS alias_claims (
        credential TEXT NOT NULL,
        name TEXT NOT NULL,
        at REAL NOT NULL
    )""",
    # An owner's single-use arm: `enrol` names a role that may enrol itself, `rekey` an alias whose next claim
    # with a new passphrase replaces its verifier. Each expires on its own; a new arm for a target replaces the
    # old one under a new `id`, so a claim spends only the arm it checked.
    """CREATE TABLE IF NOT EXISTS alias_arms (
        id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('enrol', 'rekey')),
        target TEXT NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (kind, target)
    )""",
    # Each role's one enrolment, kept as history and never deleted.
    """CREATE TABLE IF NOT EXISTS core_enrolment (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id),
        enrolled_at REAL NOT NULL,
        unmapped_at REAL
    )""",
    # The current mapping: a proof for `alias_id` makes a call run as `role`. Unique both ways.
    """CREATE TABLE IF NOT EXISTS alias_roles (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id)
    )""",
    # Where an OAuth grant's event subscriptions deliver: the role its alias is mapped to, checked on every wake.
    # `authority` is the grant's `auth.credential_authority`, so a sweep can drop rows whose grant has ended.
    """CREATE TABLE IF NOT EXISTS events_targets (
        grant_id TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        authority TEXT NOT NULL,
        bound_at REAL NOT NULL
    )""",
    # When an owner's armed rekey replaced an alias's verifier. Neither verifier is kept.
    "CREATE TABLE IF NOT EXISTS alias_rekeys (alias_id TEXT NOT NULL REFERENCES aliases(id), at REAL NOT NULL)",
    # MCP Events records, never pruned. A rotation keeps only when it happened and whose it was: no secret, digest
    # or fingerprint. A wake's occurrence time and each destination's first final outcome outlive the core wake,
    # and every delivery attempt leaves a receipt; `status` shows only the recent ones.
    "CREATE TABLE IF NOT EXISTS events_rotations (subscription TEXT NOT NULL, owner TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS events_occurrences (wake_id TEXT PRIMARY KEY, at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS events_outcomes (wake_id TEXT NOT NULL, subscription TEXT NOT NULL,"
    " outcome TEXT NOT NULL, PRIMARY KEY (wake_id, subscription))",
    "CREATE TABLE IF NOT EXISTS events_receipts (at REAL NOT NULL, wake_id TEXT NOT NULL, subscription TEXT NOT NULL,"
    " owner TEXT NOT NULL, outcome TEXT NOT NULL, UNIQUE (wake_id, subscription, at))",
    "CREATE INDEX IF NOT EXISTS events_receipts_by_time ON events_receipts (at)",  # `status` reads the recent ones
    # Admitted guest post attempts, bounded by hourly alias/credential limits; never proof values.
    """CREATE TABLE IF NOT EXISTS alias_posts (
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        credential TEXT NOT NULL,
        at REAL NOT NULL
    )""",
    _MESSAGE_REQUESTS,
    _MESSAGES,
    "CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages (conversation_id, conversation_seq)",
    "CREATE INDEX IF NOT EXISTS messages_by_id ON messages (id)",
    # Delivered secretary summaries live in the shared Baton database, outside each secretary's private
    # working state. Rows are append-only; the cursor is stable for principal-scoped paging.
    """CREATE TABLE IF NOT EXISTS secretary_summaries (
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
        secretary_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (secretary_id, decision_id)
    )""",
    "CREATE INDEX IF NOT EXISTS secretary_summaries_by_principal ON secretary_summaries (principal, cursor)",
    # `last_seq` is the last seq handed out, kept here so deleting old messages
    # never renumbers; `read_seq` is the last one delivered to the recipient.
    """CREATE TABLE IF NOT EXISTS mailboxes (
        recipient TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        read_seq INTEGER NOT NULL
    )""",
    # `last_seq` is the last conversation_seq handed out; `escalated_at` is when
    # the escalation role was last woken about the conversation's hourly limit.
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        title TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seq INTEGER NOT NULL DEFAULT 0,
        escalated_at TEXT,
        topic TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        last_activity_at TEXT,
        created_by_alias_id TEXT REFERENCES aliases(id),
        created_by_alias TEXT,
        created_by_alias_code TEXT
    )""",
    # A topic a core role opened on purpose: the container a thread is filed in, keyed by the same
    # 1-2 segment path that `conversations.topic` holds, so every existing filter keeps working on the
    # same text. The parent is implied by the path and depth stays at two, so there is no parent column.
    # `members` is the JSON list of roles a guest thread here is delivered to, read at creation and
    # copied into that thread's participants: it is a default for new threads, never a live grant.
    """CREATE TABLE IF NOT EXISTS topics (
        path TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        members TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        request_id TEXT UNIQUE,
        request_digest TEXT,
        id TEXT NOT NULL UNIQUE
    )""",
    # Every change to a topic, appended and never edited: what it was, what it became, and who made the change
    # (`actor` names the alias behind a call when there was one). `topic_id` is the topic acted on, so a rename
    # keeps the history with the topic and a reused path starts a new one. `grant_id` stays NULL until a later
    # change fills it. A set_topic row names the topic the conversation moved into, or left when it was cleared.
    # No foreign key to `topics`: this table is created before an upgrade adds `topics.id`, and nothing deletes a topic.
    """CREATE TABLE IF NOT EXISTS topic_changes (
        topic_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        actor TEXT NOT NULL,
        grant_id TEXT,
        before TEXT,
        after TEXT,
        at TEXT NOT NULL
    )""",
    # An owner's topic grant for one alias: proposed, then authorized, then possibly revoked. The proposed
    # fields never change, and an authorization or revocation only fills its own. No state is stored; the state
    # is derived from these columns and the clock (`baton.topics.grants`). Only the key's sha256 is kept.
    """CREATE TABLE IF NOT EXISTS topic_grants (
        id TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL,
        topic_id TEXT NOT NULL,
        capabilities TEXT NOT NULL,
        inherit INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        proposed_by TEXT NOT NULL,
        proposed_at TEXT NOT NULL,
        proposal_expires_at TEXT NOT NULL,
        nonce TEXT NOT NULL,
        digest TEXT NOT NULL,
        authorized_by TEXT,
        authorized_at TEXT,
        authorized_digest TEXT,
        proof_digest TEXT,
        revoked_at TEXT,
        revoked_by TEXT,
        revoke_reason TEXT,
        alias_name_seen TEXT NOT NULL,
        alias_code_seen TEXT NOT NULL,
        topic_path_seen TEXT NOT NULL
    )""",
    # Every grant event, appended and never edited. `detail` is JSON or NULL, and never holds a key.
    # No foreign keys, as with `topic_changes`: nothing deletes a row.
    """CREATE TABLE IF NOT EXISTS topic_grant_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        at TEXT NOT NULL
    )""",
    # A reference: a named document in a topic, kept by the topic's id and never its path, so a rename carries it
    # along and a path a rename frees starts again with none. Its text is in `reference_revisions`.
    # No foreign keys, as with `topic_changes`: nothing deletes a row.
    """CREATE TABLE IF NOT EXISTS reference_docs (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (topic_id, name)
    )""",
    # Every revision of every reference, appended and never edited or deleted. A document's current text is its
    # highest revision, so no counter is stored. The alias columns are all NULL (a direct credential) or all set: the
    # name and code are what the alias was called then, for reading only. `grant_id` is the topic grant a guest acted
    # under, NULL for a core role. `request_id` is the retry identity, and lives as long as the revision does.
    """CREATE TABLE IF NOT EXISTS reference_revisions (
        doc_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        body TEXT NOT NULL,
        author_role TEXT NOT NULL,
        author_alias_id TEXT,
        author_alias_name TEXT,
        author_alias_code TEXT,
        grant_id TEXT,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        base_revision INTEGER NOT NULL CHECK (base_revision = revision - 1),
        at TEXT NOT NULL,
        PRIMARY KEY (doc_id, revision)
    )""",
    # Every change to a conversation's source, in order: a message added, a participant added or removed, and,
    # in logs from before copies stopped expiring, a copy expired by retention. Each carries the revision it
    # bumped the conversation to. Only source changes are logged; reading logs nothing. See baton.changes.
    """CREATE TABLE IF NOT EXISTS change_log (
        change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message_id TEXT,
        role TEXT,
        at TEXT NOT NULL
    )""",
    # The log's identity (`log_epoch`) and how far an earlier prune reached (`pruned_through`).
    "CREATE TABLE IF NOT EXISTS log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # Derived data, written only by the local indexing writer (baton.derived). A generation is one producer's
    # set of artifacts; each producer has one so far, created by its first batch.
    """CREATE TABLE IF NOT EXISTS generations (
        id TEXT PRIMARY KEY,
        producer TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        active INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        watermark_epoch TEXT,
        watermark_seq INTEGER,
        activated_at TEXT,
        deactivated_at TEXT,
        dropped_at TEXT,
        last_write_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS generations_by_producer ON generations (producer, active)",
    # A read batch: the manifest Baton issued, which a commit is checked against. `expires_at` is epoch seconds.
    """CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY,
        producer TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        generation TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        manifest TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        pages INTEGER NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""",
    # Batches are kept once committed or expired. The writer's open-batch count and the retirement probe read only
    # open ones, by producer or generation and expiry, so these index nothing else.
    "CREATE INDEX IF NOT EXISTS open_batches_by_producer ON batches (producer, expires_at) WHERE state = 'open'",
    "CREATE INDEX IF NOT EXISTS open_batches_by_generation ON batches (generation, expires_at) WHERE state = 'open'",
    """CREATE TABLE IF NOT EXISTS batch_pages (
        batch_id TEXT NOT NULL,
        page INTEGER NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, page)
    )""",
    # Immutable once written, apart from `state`, which goes from valid to invalid and never back.
    """CREATE TABLE IF NOT EXISTS artifact_versions (
        id TEXT PRIMARY KEY,
        producer TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        generation TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        citations TEXT NOT NULL,
        covered_through_seq INTEGER NOT NULL,
        source_revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (producer, scope_id, generation, conversation_id, kind, version)
    )""",
    # Every conversation seq a version was made from, its prior's included, as merged ranges.
    """CREATE TABLE IF NOT EXISTS artifact_inputs (
        version_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        from_seq INTEGER NOT NULL,
        to_seq INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS artifact_inputs_by_seq ON artifact_inputs (conversation_id, from_seq, to_seq)",
    "CREATE INDEX IF NOT EXISTS artifact_inputs_by_version ON artifact_inputs (version_id)",
    "CREATE TABLE IF NOT EXISTS artifact_depends_on (version_id TEXT PRIMARY KEY, prior_id TEXT NOT NULL)",
    # A summary version's optional canonical graph snapshot (baton.graph.snapshot), written in the same commit
    # transaction as the version itself. Immutable; it lives and dies with its version, and nothing indexes its text.
    """CREATE TABLE IF NOT EXISTS graph_snapshots (
        version_id TEXT PRIMARY KEY REFERENCES artifact_versions(id) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL,
        covered_through_seq INTEGER NOT NULL,
        graph_digest TEXT NOT NULL,
        revision INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # One vector per item per generation, as little-endian float32; Baton stores them and never computes one.
    """CREATE TABLE IF NOT EXISTS vectors (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        data BLOB NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    )""",
    """CREATE TABLE IF NOT EXISTS vector_skips (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    )""",
    # A commit's retry identity and receipt, kept apart from batches and versions so a replay is answered
    # from this row alone until `expires_at`.
    """CREATE TABLE IF NOT EXISTS commit_requests (
        producer TEXT NOT NULL,
        request_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        receipt TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (producer, request_id)
    )""",
    "CREATE INDEX IF NOT EXISTS commits_by_producer_time ON commit_requests (producer, committed_at)",  # hourly count
    """CREATE TABLE IF NOT EXISTS participants (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, role)
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        state TEXT NOT NULL,
        waiting_on TEXT,
        revision INTEGER NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # The retry identity of each accepted move_job, and the outcome a retry replays.
    """CREATE TABLE IF NOT EXISTS job_moves (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # `pos` orders every event for `wait` and the wake dispatcher; (job_id, seq)
    # is the per-job sequence readers page through. A message outside any job
    # is logged with a NULL job_id and its mailbox seq.
    """CREATE TABLE IF NOT EXISTS events (
        pos INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT REFERENCES jobs(id),
        seq INTEGER NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        audience TEXT,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        data TEXT NOT NULL,
        UNIQUE (job_id, seq)
    )""",
    "CREATE INDEX IF NOT EXISTS events_by_audience ON events (audience, pos)",
    # Read positions in the event log: one per role for `wait`, one for the dispatcher.
    "CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, pos INTEGER NOT NULL)",
    # One row per wake owed to a role. `key` identifies what it is about, so a
    # repeated or replayed event never wakes twice.
    """CREATE TABLE IF NOT EXISTS wakes (
        role TEXT NOT NULL,
        key TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        lease_until REAL NOT NULL,
        attempts INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        due_at REAL,
        baseline_seq INTEGER,
        ordinal INTEGER,
        claimed_at REAL,
        PRIMARY KEY (role, key)
    )""",
    # The browser seat (`baton.seat`). Nothing secret is stored: a session, a pre-login form and an inbox
    # receipt are kept only as SHA-256 digests of their random values, and a session's credential only as
    # the name, digest and role that `auth.authority_live` checks again on every request. A row that ends
    # stays, with when and how it ended, and is refused by that.
    """CREATE TABLE IF NOT EXISTS seat_sessions (
        digest TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        authority TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT
    )""",
    "CREATE TABLE IF NOT EXISTS seat_prelogin (digest TEXT PRIMARY KEY, expires_at REAL NOT NULL, ended_at REAL,"
    " end_reason TEXT)",
    # One rendered inbox page: acknowledging it advances the cursor from `start_seq` to `last_seq`, or nothing.
    """CREATE TABLE IF NOT EXISTS seat_receipts (
        digest TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        role TEXT NOT NULL,
        start_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT
    )""",
    # A compose form's request id, bound to the session and conversation it was rendered for. `sent_at` is its
    # place in the hourly quota, held while `holders` requests for it are in flight or once it is stored.
    """CREATE TABLE IF NOT EXISTS seat_requests (
        request_id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        sent_at REAL,
        holders INTEGER NOT NULL DEFAULT 0,
        ended_at REAL,
        end_reason TEXT
    )""",
    # Ended seat rows are kept, so each cap check finds its live rows through these instead of scanning them all.
    "CREATE INDEX IF NOT EXISTS seat_prelogin_by_expiry ON seat_prelogin (expires_at)",
    "CREATE INDEX IF NOT EXISTS seat_receipts_by_session ON seat_receipts (session)",
    "CREATE INDEX IF NOT EXISTS seat_requests_by_session ON seat_requests (session)",
)
