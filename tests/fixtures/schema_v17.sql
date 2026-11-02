-- Schema 17 from exact head ebdf531a of the topic grant checks; no live database. Like the v16 fixture it also carries rows: two topics with ids, one alias, one conversation filed in a topic, and one authorized topic grant with its events. No thread has `reviewer` in it: older-fixture tests add one carried-over thread for that role.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alias_arms (
        id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('enrol', 'rekey')),
        target TEXT NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (kind, target)
    );
CREATE TABLE IF NOT EXISTS alias_bindings (
        digest TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        expires_at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS alias_claims (
        credential TEXT NOT NULL,
        name TEXT NOT NULL,
        at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS alias_posts (
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        credential TEXT NOT NULL,
        at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS alias_roles (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id)
    );
CREATE TABLE IF NOT EXISTS aliases (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        skeleton TEXT NOT NULL UNIQUE,
        verifier TEXT NOT NULL,
        created_at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS artifact_depends_on (version_id TEXT PRIMARY KEY, prior_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_inputs (
        version_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        from_seq INTEGER NOT NULL,
        to_seq INTEGER NOT NULL
    );
CREATE TABLE IF NOT EXISTS artifact_versions (
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
CREATE TABLE IF NOT EXISTS batch_pages (
        batch_id TEXT NOT NULL,
        page INTEGER NOT NULL,
        delivered_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, page)
    );
CREATE TABLE IF NOT EXISTS batches (
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
CREATE TABLE IF NOT EXISTS change_log (
        change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message_id TEXT,
        role TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS commit_requests (
        producer TEXT NOT NULL,
        request_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        receipt TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (producer, request_id)
    );
CREATE TABLE IF NOT EXISTS conversations (
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
CREATE TABLE IF NOT EXISTS core_enrolment (
        role TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(id),
        enrolled_at REAL NOT NULL,
        unmapped_at REAL
    );
CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, pos INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
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
CREATE TABLE IF NOT EXISTS events_targets (
        grant_id TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL REFERENCES aliases(id),
        authority TEXT NOT NULL,
        bound_at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS generations (
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
CREATE TABLE IF NOT EXISTS job_moves (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id),
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS jobs (
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
CREATE TABLE IF NOT EXISTS log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mailboxes (
        recipient TEXT PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        read_seq INTEGER NOT NULL
    );
CREATE TABLE IF NOT EXISTS message_requests (
        request_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        message_id TEXT NOT NULL,
        seq INTEGER,
        seqs TEXT,
        created_at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS messages (
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
CREATE TABLE IF NOT EXISTS participants (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, role)
    );
CREATE TABLE IF NOT EXISTS seat_prelogin (digest TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS seat_receipts (
        digest TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        role TEXT NOT NULL,
        start_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        expires_at REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS seat_requests (
        request_id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        sent_at REAL,
        holders INTEGER NOT NULL DEFAULT 0
    );
CREATE TABLE IF NOT EXISTS seat_sessions (
        digest TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        authority TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL
    );
CREATE TABLE IF NOT EXISTS topic_changes (
        topic_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        actor TEXT NOT NULL,
        grant_id TEXT,
        before TEXT,
        after TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS topic_grant_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT,
        at TEXT NOT NULL
    );
CREATE TABLE IF NOT EXISTS topic_grants (
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
CREATE TABLE IF NOT EXISTS topics (
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
CREATE TABLE IF NOT EXISTS vector_skips (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    );
CREATE TABLE IF NOT EXISTS vectors (
        generation TEXT NOT NULL,
        item_kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        data BLOB NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (generation, item_kind, item_id)
    );
CREATE TABLE IF NOT EXISTS wakes (
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
CREATE INDEX artifact_inputs_by_seq ON artifact_inputs (conversation_id, from_seq, to_seq);
CREATE INDEX artifact_inputs_by_version ON artifact_inputs (version_id);
CREATE TRIGGER artifacts_fts_delete AFTER DELETE ON artifact_versions BEGIN DELETE FROM artifacts_fts WHERE rowid = old.rowid AND version_id = old.id; END;
CREATE TRIGGER artifacts_fts_insert AFTER INSERT ON artifact_versions BEGIN INSERT INTO artifacts_fts (rowid, body, version_id) VALUES (new.rowid, new.body, new.id); END;
CREATE INDEX events_by_audience ON events (audience, pos);
CREATE INDEX generations_by_producer ON generations (producer, active);
CREATE INDEX messages_by_conversation ON messages (conversation_id, conversation_seq);
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN DELETE FROM messages_fts WHERE rowid = old.rowid AND recipient = old.recipient AND seq = old.seq; END;
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO messages_fts (rowid, text, recipient, seq) VALUES (new.rowid, new.text, new.recipient, new.seq); END;
INSERT INTO aliases (id, name, skeleton, verifier, created_at) VALUES ('al_0000000000000001', 'job-search', 'job-search', 'synthetic-verifier', 0.0);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_1d9f246762224cce', '20261102T122159Z-4adf5e6a01cb', '2da92dd16e8732fab6d2a2c926e4a2a2ada647049d608b84baed3df8d087cdc1', 'Which boards to watch', 'orchestrator', '2026-11-02T12:21:59.760311+00:00', 0, NULL, 'job-hunt/boards', 0, NULL, NULL, NULL, NULL);
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_1d9f246762224cce', 'orchestrator', '2026-11-02T12:21:59.760311+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_1d9f246762224cce', 'assistant', '2026-11-02T12:21:59.760311+00:00');
INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at) VALUES ('topic_50f795d03e5c1996', 'create_topic', 'orchestrator', NULL, NULL, '{"path": "job-hunt", "title": "Job hunt", "description": "Where the search is kept", "members": ["orchestrator", "assistant"]}', '2026-11-02T12:21:59.758260+00:00');
INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at) VALUES ('topic_47c984f60032475f', 'create_topic', 'orchestrator', NULL, NULL, '{"path": "job-hunt/boards", "title": "boards", "description": null, "members": ["orchestrator", "assistant"]}', '2026-11-02T12:21:59.759336+00:00');
INSERT INTO topic_grant_events (seq, grant_id, event, actor, detail, at) VALUES (1, 'tgr_63caf9381b7e1e7c', 'propose', 'owner', NULL, '2026-11-03T12:00:00+00:00');
INSERT INTO topic_grant_events (seq, grant_id, event, actor, detail, at) VALUES (2, 'tgr_63caf9381b7e1e7c', 'authorize', 'owner', NULL, '2026-11-03T12:00:00+00:00');
INSERT INTO topic_grants (id, alias_id, topic_id, capabilities, inherit, expires_at, proposed_by, proposed_at, proposal_expires_at, nonce, digest, authorized_by, authorized_at, authorized_digest, proof_digest, revoked_at, revoked_by, revoke_reason, alias_name_seen, alias_code_seen, topic_path_seen) VALUES ('tgr_63caf9381b7e1e7c', 'al_0000000000000001', 'topic_50f795d03e5c1996', '["reference.edit", "thread.organise", "topic.create_sub"]', 1, '2026-12-03T12:00:00+00:00', 'owner', '2026-11-03T12:00:00+00:00', '2026-11-04T12:00:00+00:00', '2f44c8a69f0c9dd644bf7010e4f6c5f5', '78fbaff52eda89ac95bf0ef9b19b0611a4f1417e1c6f1c901c97cbf8f0bd15b6', 'owner', '2026-11-03T12:00:00+00:00', '78fbaff52eda89ac95bf0ef9b19b0611a4f1417e1c6f1c901c97cbf8f0bd15b6', '5522221a434840361021eab12a3d7e04bca23490d5134cc951eab37be54a04e8', NULL, NULL, NULL, 'job-search', 'fce5', 'job-hunt');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest, id) VALUES ('job-hunt', 'Job hunt', 'Where the search is kept', '["orchestrator", "assistant"]', 'orchestrator', '2026-11-02T12:21:59.757840+00:00', '20261102T122159Z-67d3c851644e', 'e47e446a38c1597a7596e0db884afcbfc27391a650f267ced7996e14a996a4d7', 'topic_50f795d03e5c1996');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest, id) VALUES ('job-hunt/boards', 'boards', NULL, '["orchestrator", "assistant"]', 'orchestrator', '2026-11-02T12:21:59.759115+00:00', '20261102T122159Z-84375c44d3ef', '54ea2612bd516b81c2f22b1bf18bc94e119dc1f0f7aebef789cd9417aff7afbe', 'topic_47c984f60032475f');
INSERT INTO schema_version VALUES (17);
