-- Schema 15 from exact main head e17bbfb; no live database. Unlike the older fixtures it also carries rows: topics, conversations filed in them, one guest alias. No thread has `reviewer` in it: older-fixture tests add one carried-over thread for that role.
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
CREATE TABLE IF NOT EXISTS 'artifacts_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'artifacts_fts_content'(id INTEGER PRIMARY KEY, c0, c1);
CREATE TABLE IF NOT EXISTS 'artifacts_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'artifacts_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'artifacts_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
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
CREATE TABLE IF NOT EXISTS 'messages_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'messages_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2);
CREATE TABLE IF NOT EXISTS 'messages_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'messages_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'messages_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
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
CREATE TABLE IF NOT EXISTS topics (
        path TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        members TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        request_id TEXT UNIQUE,
        request_digest TEXT
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
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_e10de6b6cb244711', '20261102T081319Z-43dce8be8dc5', '80dd1597dce30f6871f174d0d4207c3385329a247e47cf245bc84009d39f76f6', 'Release plan', 'orchestrator', '2026-11-02T08:13:19.499546+00:00', 0, NULL, 'baton', 0, NULL, NULL, NULL, NULL);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_c83d0be7c6674cd2', '20261102T081319Z-9385aa2454f6', '229126b0308ebf56612a73dd055d0c8c2e6ef9523f9456c678b32f28b669ef90', 'Schema work', 'orchestrator', '2026-11-02T08:13:19.500562+00:00', 0, NULL, 'baton/development', 0, NULL, NULL, NULL, NULL);
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_974d043d362b463d', '20261102T081319Z-7543b1afc330', 'bb256b0d389ab7f35c3c615bd40fd9d177c8bc4a8fa01476ba1b61fa6faf0274', 'A guest question', 'guest', '2026-11-02T08:13:19.501477+00:00', 0, NULL, 'baton', 0, NULL, 'al_0000000000000001', 'lane-a', 'ab12');
INSERT INTO conversations (id, request_id, request_digest, title, created_by, created_at, last_seq, escalated_at, topic, revision, last_activity_at, created_by_alias_id, created_by_alias, created_by_alias_code) VALUES ('conv_9250a8a9f8794794', '20261102T081319Z-fb9276968a0e', '1642155165426649b8275e440563b4aa0ca0fab56621a157da2fc9ba31f11384', 'No topic', 'orchestrator', '2026-11-02T08:13:19.502006+00:00', 0, NULL, NULL, 0, NULL, NULL, NULL, NULL);
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_e10de6b6cb244711', 'orchestrator', '2026-11-02T08:13:19.499546+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_e10de6b6cb244711', 'assistant', '2026-11-02T08:13:19.499546+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_c83d0be7c6674cd2', 'orchestrator', '2026-11-02T08:13:19.500562+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_c83d0be7c6674cd2', 'outsider', '2026-11-02T08:13:19.500562+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_974d043d362b463d', 'orchestrator', '2026-11-02T08:13:19.501477+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_974d043d362b463d', 'assistant', '2026-11-02T08:13:19.501477+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_9250a8a9f8794794', 'orchestrator', '2026-11-02T08:13:19.502006+00:00');
INSERT INTO participants (conversation_id, role, added_at) VALUES ('conv_9250a8a9f8794794', 'outsider', '2026-11-02T08:13:19.502006+00:00');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest) VALUES ('baton', 'Baton', 'The relay itself', '["orchestrator", "assistant"]', 'orchestrator', '2026-11-02T08:13:19.497802+00:00', '20261102T081319Z-abb9292797d9', '080ff64e198213f5637def38861811afef36047bc5e43832f04829c1c3563752');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest) VALUES ('baton/development', 'development', NULL, '["orchestrator", "outsider"]', 'orchestrator', '2026-11-02T08:13:19.498520+00:00', '20261102T081319Z-2bd8537ad146', '881bacb9febf6dfb8a57c58c658c81d4bdfac0ddb906e325c1aa3caf18420ede');
INSERT INTO topics (path, title, description, members, created_by, created_at, request_id, request_digest) VALUES ('reviews', 'reviews', 'Reviews, one thread each', '[]', 'orchestrator', '2026-11-02T08:13:19.498928+00:00', '20261102T081319Z-d7e2927739c0', '2f3c539aa0f87170f4a490099eb39aab8e6d136c90b40e3c054cb5136e96bc9b');
INSERT INTO schema_version VALUES (15);
