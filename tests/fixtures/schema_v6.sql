-- The schema-version-6 tables exactly as the conversation-topic change created them, for the migration test.
CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL,
        title TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seq INTEGER NOT NULL DEFAULT 0,
        escalated_at TEXT,
        topic TEXT
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
CREATE INDEX events_by_audience ON events (audience, pos);
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
CREATE INDEX messages_by_conversation ON messages (conversation_id, conversation_seq);
CREATE TABLE participants (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, role)
    );
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE wakes (
        role TEXT NOT NULL,
        key TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        lease_until REAL NOT NULL,
        attempts INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        PRIMARY KEY (role, key)
    );
INSERT INTO schema_version (version) VALUES (6);
