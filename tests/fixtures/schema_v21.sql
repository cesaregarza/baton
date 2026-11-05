-- Schema 21 from runtime initialization at 7bbf0b312bee0b849c71d545bad6d0ff042addb0; synthetic schema only.
CREATE TABLE alias_arms (
        id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('enrol', 'rekey')),
        target TEXT NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (kind, target)
    );
CREATE TABLE alias_bindings (
        digest TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        expires_at REAL NOT NULL,
        issued INTEGER
    );
CREATE TABLE alias_claims (
        credential TEXT NOT NULL,
        name TEXT NOT NULL,
        at REAL NOT NULL
    );
CREATE TABLE alias_posts (
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        credential TEXT NOT NULL,
        at REAL NOT NULL
    );
CREATE TABLE alias_rekeys (alias_id TEXT NOT NULL REFERENCES aliases(id), at REAL NOT NULL);
CREATE TABLE alias_roles (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id)
    );
CREATE TABLE aliases (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        skeleton TEXT NOT NULL UNIQUE,
        verifier TEXT NOT NULL,
        created_at REAL NOT NULL
    );
CREATE TABLE artifact_depends_on (version_id TEXT PRIMARY KEY, prior_id TEXT NOT NULL);
CREATE TABLE artifact_inputs (
        version_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        from_seq INTEGER NOT NULL,
        to_seq INTEGER NOT NULL
    );
CREATE TABLE artifact_versions (
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
    );
CREATE VIRTUAL TABLE artifacts_fts USING fts5(body, version_id UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE batch_pages (
        batch_id TEXT NOT NULL,
        page INTEGER NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, page)
    );
CREATE TABLE batches (
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
    );
CREATE TABLE change_log (
        change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message_id TEXT,
        role TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE commit_requests (
        producer TEXT NOT NULL,
        request_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        receipt TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (producer, request_id)
    );
CREATE TABLE conversations (
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
    );
CREATE TABLE core_enrolment (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id),
        enrolled_at REAL NOT NULL,
        unmapped_at REAL
    );
CREATE TABLE cursors (name TEXT PRIMARY KEY, pos INTEGER NOT NULL);
CREATE TABLE events (
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
    );
CREATE TABLE events_occurrences (wake_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE events_outcomes (wake_id TEXT NOT NULL, subscription TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY (wake_id, subscription));
CREATE TABLE events_receipts (at REAL NOT NULL, wake_id TEXT NOT NULL, subscription TEXT NOT NULL, owner TEXT NOT NULL, outcome TEXT NOT NULL, UNIQUE (wake_id, subscription, at));
CREATE TABLE events_rotations (subscription TEXT NOT NULL, owner TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE events_targets (
        grant_id TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        authority TEXT NOT NULL,
        bound_at REAL NOT NULL
    );
CREATE TABLE generations (
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
    );
CREATE TABLE job_moves (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
CREATE TABLE jobs (
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
    );
CREATE TABLE log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE mailboxes (
        recipient TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        read_seq INTEGER NOT NULL
    );
CREATE TABLE message_requests (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER,
        seqs TEXT,
        created_at TEXT NOT NULL
    );
CREATE TABLE messages (
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
    );
CREATE VIRTUAL TABLE messages_fts USING fts5(text, recipient UNINDEXED, seq UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE participants (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, role)
    );
CREATE TABLE reference_docs (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (topic_id, name)
    );
CREATE TABLE reference_revisions (
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
    );
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE seat_prelogin (digest TEXT PRIMARY KEY, expires_at REAL NOT NULL, ended_at REAL, end_reason TEXT);
CREATE TABLE seat_receipts (
        digest TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        role TEXT NOT NULL,
        start_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT
    );
CREATE TABLE seat_requests (
        request_id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        sent_at REAL,
        holders INTEGER NOT NULL DEFAULT 0,
        ended_at REAL,
        end_reason TEXT
    );
CREATE TABLE seat_sessions (
        digest TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        authority TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT
    );
CREATE TABLE secretary_summaries (
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
        secretary_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (secretary_id, decision_id)
    );
CREATE TABLE topic_changes (
        topic_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        actor TEXT NOT NULL,
        grant_id TEXT,
        before TEXT,
        after TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE topic_grant_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE topic_grants (
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
    );
CREATE TABLE topics (
        path TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        members TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        request_id TEXT UNIQUE,
        request_digest TEXT,
        id TEXT NOT NULL UNIQUE
    );
CREATE TABLE vector_skips (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    );
CREATE TABLE vectors (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        data BLOB NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    );
CREATE TABLE wakes (
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
    );
CREATE INDEX alias_bindings_by_alias ON alias_bindings (alias_id, expires_at);
CREATE UNIQUE INDEX alias_bindings_by_issue ON alias_bindings (alias_id, issued);
CREATE INDEX artifact_inputs_by_seq ON artifact_inputs (conversation_id, from_seq, to_seq);
CREATE INDEX artifact_inputs_by_version ON artifact_inputs (version_id);
CREATE INDEX commits_by_producer_time ON commit_requests (producer, committed_at);
CREATE INDEX events_by_audience ON events (audience, pos);
CREATE INDEX events_receipts_by_time ON events_receipts (at);
CREATE INDEX generations_by_producer ON generations (producer, active);
CREATE INDEX messages_by_conversation ON messages (conversation_id, conversation_seq);
CREATE INDEX open_batches_by_generation ON batches (generation, expires_at) WHERE state = 'open';
CREATE INDEX open_batches_by_producer ON batches (producer, expires_at) WHERE state = 'open';
CREATE INDEX seat_prelogin_by_expiry ON seat_prelogin (expires_at);
CREATE INDEX seat_receipts_by_session ON seat_receipts (session);
CREATE INDEX seat_requests_by_session ON seat_requests (session);
CREATE INDEX secretary_summaries_by_principal ON secretary_summaries (principal, cursor);
CREATE TRIGGER artifacts_fts_delete AFTER DELETE ON artifact_versions BEGIN DELETE FROM artifacts_fts WHERE rowid = old.rowid AND version_id = old.id; END;
CREATE TRIGGER artifacts_fts_insert AFTER INSERT ON artifact_versions BEGIN INSERT INTO artifacts_fts (rowid, body, version_id) VALUES (new.rowid, new.body, new.id); END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM messages_fts WHERE rowid = old.rowid AND recipient = old.recipient AND seq = old.seq; END;
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO messages_fts (rowid, text, recipient, seq) VALUES (new.rowid, new.text, new.recipient, new.seq); END;
INSERT INTO schema_version VALUES (21);
