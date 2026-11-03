-- Schema 18 from exact head d037c9cf of the topic references change; no live database. It also carries rows: one seat session with a pre-login form, an inbox receipt and two form ids (one sent), and one generation with a committed batch, an expired open batch and a commit receipt. It has no conversation, so older-fixture tests add their own.
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
CREATE TABLE IF NOT EXISTS reference_docs (
        id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (topic_id, name)
    );
CREATE TABLE IF NOT EXISTS reference_revisions (
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
INSERT INTO batches (id, producer, scope_id, generation, conversation_id, manifest, manifest_digest, pages, state, created_at, expires_at) VALUES ('bat_committed18', 'alpha', 'all', 'gen_fixture18', 'conv_fixture18', '{}', 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 1, 'committed', '2026-10-01T00:00:00+00:00', 1790816400.0);
INSERT INTO batches (id, producer, scope_id, generation, conversation_id, manifest, manifest_digest, pages, state, created_at, expires_at) VALUES ('bat_expired18', 'alpha', 'all', 'gen_fixture18', 'conv_fixture18', '{}', 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 1, 'open', '2026-10-01T00:05:00+00:00', 1790816700.0);
INSERT INTO commit_requests (producer, request_id, digest, receipt, committed_at, expires_at) VALUES ('alpha', 'r-fixture18', '0000000000000000000000000000000000000000000000000000000000000000', '{}', '2026-10-01T00:10:00+00:00', '2026-11-01T00:10:00+00:00');
INSERT INTO generations (id, producer, scope_id, profile, active, created_at, watermark_epoch, watermark_seq, activated_at, deactivated_at, dropped_at, last_write_at) VALUES ('gen_fixture18', 'alpha', 'all', '{}', 1, '2026-10-01T00:00:00+00:00', NULL, NULL, '2026-10-01T00:00:00+00:00', NULL, NULL, '2026-10-01T00:10:00+00:00');
INSERT INTO seat_prelogin (digest, expires_at) VALUES ('cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc', 1790813100.0);
INSERT INTO seat_receipts (digest, session, role, start_seq, last_seq, expires_at) VALUES ('dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'operator', 0, 2, 1790814300.0);
INSERT INTO seat_requests (request_id, session, conversation_id, created_at, sent_at, holders) VALUES ('20261001T000000Z-000000000001', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'conv_fixture18', 1790812900.0, 1790813000.0, 0);
INSERT INTO seat_requests (request_id, session, conversation_id, created_at, sent_at, holders) VALUES ('20261001T000000Z-000000000002', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'conv_fixture18', 1790813300.0, NULL, 0);
INSERT INTO seat_sessions (digest, role, authority, created_at, last_seen) VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'operator', '{"kind": "token", "name": "operator-phone", "role": "operator", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}', 1790812800.0, 1790813400.0);
INSERT INTO schema_version VALUES (18);
