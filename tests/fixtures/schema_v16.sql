-- Schema 16 from exact head 41a8a87f of the stable topic ids change; no live database. Like the v15 fixture it also carries rows: topics with ids, conversations filed in them, one guest alias. No thread has `reviewer` in it: older-fixture tests add one carried-over thread for that role.
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
INSERT INTO aliases (id, name, skeleton, verifier, created_at) VALUES ('al_0000000000000001', 'lane-a', 'lane-a', 'synthetic-verifier', 0.0);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_4c033f6af5474142', '20261102T092638Z-ae20fabeac5e', '80dd1597dce30f6871f174d0d4207c3385329a247e47cf245bc84009d39f76f6', 'Release plan', 'orchestrator', '2026-11-02T09:26:38.961625+00:00', 0, NULL, 'baton', 0, NULL, NULL, NULL, NULL);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_847f6dfff4754a9e', '20261102T092638Z-215c9f850eba', 'b928768cc68b55d6fce401583e16d66df3e0b4f69b8de8568c20a1356b4abcbb', 'Schema work', 'orchestrator', '2026-11-02T09:26:38.963359+00:00', 0, NULL, 'baton/development', 0, NULL, NULL, NULL, NULL);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_bdaf89ac0a0a4764', '20261102T092638Z-0d7aec1923b4', '1642155165426649b8275e440563b4aa0ca0fab56621a157da2fc9ba31f11384', 'No topic', 'orchestrator', '2026-11-02T09:26:38.964362+00:00', 0, NULL, NULL, 0, NULL, NULL, NULL, NULL);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_db98a4ed98b84bd6', '20261102T092638Z-c12cd9259bdb', 'bb256b0d389ab7f35c3c615bd40fd9d177c8bc4a8fa01476ba1b61fa6faf0274', 'A guest question', 'guest', '2026-11-02T09:26:38.966518+00:00', 0, NULL, 'baton', 0, NULL, 'al_0000000000000001', 'lane-a', 'ab12');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_4c033f6af5474142', 'orchestrator', '2026-11-02T09:26:38.961625+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_4c033f6af5474142', 'assistant', '2026-11-02T09:26:38.961625+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_847f6dfff4754a9e', 'orchestrator', '2026-11-02T09:26:38.963359+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_847f6dfff4754a9e', 'assistant', '2026-11-02T09:26:38.963359+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_bdaf89ac0a0a4764', 'orchestrator', '2026-11-02T09:26:38.964362+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_bdaf89ac0a0a4764', 'outsider', '2026-11-02T09:26:38.964362+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_db98a4ed98b84bd6', 'orchestrator', '2026-11-02T09:26:38.966518+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_db98a4ed98b84bd6', 'assistant', '2026-11-02T09:26:38.966518+00:00');
INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at) VALUES ('topic_7361e49ce0e05942', 'create_topic', 'orchestrator', NULL, NULL, '{"path": "baton", "title": "Baton", "description": "The relay itself", "members": ["orchestrator", "assistant"]}', '2026-11-02T09:26:38.958190+00:00');
INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at) VALUES ('topic_3912705a8cf2924c', 'create_topic', 'orchestrator', NULL, NULL, '{"path": "baton/development", "title": "development", "description": null, "members": ["orchestrator", "outsider"]}', '2026-11-02T09:26:38.959377+00:00');
INSERT INTO topic_changes (topic_id, operation, actor, grant_id, before, after, at) VALUES ('topic_26fcf4475c72e878', 'create_topic', 'orchestrator', NULL, NULL, '{"path": "reviews", "title": "reviews", "description": "Reviews, one thread each", "members": []}', '2026-11-02T09:26:38.960513+00:00');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest, id) VALUES ('baton', 'Baton', 'The relay itself', '["orchestrator", "assistant"]', 'orchestrator', '2026-11-02T09:26:38.957791+00:00', '20261102T092638Z-6164e8628fc2', '080ff64e198213f5637def38861811afef36047bc5e43832f04829c1c3563752', 'topic_7361e49ce0e05942');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest, id) VALUES ('baton/development', 'development', NULL, '["orchestrator", "outsider"]', 'orchestrator', '2026-11-02T09:26:38.959132+00:00', '20261102T092638Z-fee337d8b9f0', '881bacb9febf6dfb8a57c58c658c81d4bdfac0ddb906e325c1aa3caf18420ede', 'topic_3912705a8cf2924c');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest, id) VALUES ('reviews', 'reviews', 'Reviews, one thread each', '[]', 'orchestrator', '2026-11-02T09:26:38.960294+00:00', '20261102T092638Z-17633538e0c2', '2f3c539aa0f87170f4a490099eb39aab8e6d136c90b40e3c054cb5136e96bc9b', 'topic_26fcf4475c72e878');
INSERT INTO schema_version VALUES (16);
