# Reading the code

Start with the domain objects and follow one operation through the connection they receive.

```python
from baton.participants import Participant
from baton.store import connection

with connection(config.db_path) as db:
    planner = Participant(db, config, "planner")
    worker = Participant(db, config, "worker")
    receipt = planner.send("Please check this", to="worker")
    page = worker.inbox.read()
```

This is trusted host-side code. MCP and browser entrypoints authenticate the caller before choosing its role.

## Domain concepts

- `participants.Participant` is a runtime actor: an agent or person acting as a role. It binds outgoing
  messages to that role, derives authorship from configuration, checks destinations and owns its inbox
  access. The CLI and MCP use the same send behavior. `config.Role` remains the role's static settings.
- `messages.DirectMessage` is a submission addressed to one role. It owns validation, replay identity,
  mailbox admission and the atomic send. Reusing the same object reuses its request ID. The dictionary
  receipt reports whether that submission created a message or replayed an earlier one.
- `posts.Post` is the stored message read model. It has explicit fields for text, sender, conversation,
  attribution and attention. It decodes a database row and renders the existing public payload. A reply's
  visible parent is supplied separately after checking the reader's access.
- `inbox.Inbox` belongs to a role and a connection. `read()` delivers messages; `take()` delivers a page
  of events; `wait()` waits for events. It owns the delivery cursor and advances it atomically with the read.
- `conversation.Conversation` owns one thread's delivery, participant changes, topic moves and history.
  It checks current membership on every send, including retries. A handle never caches membership.
  Collection queries and shared conversation helpers live in `conversations.py`.
- `jobs.Job` owns creation, transitions, revision checks, retry receipts and event history. It reloads
  state for each operation so an existing handle sees changes made through another connection.
- `message_content` contains pure content and retry-ID validation.

The older message, inbox, conversation and job functions remain compatibility entrypoints. Their
behavior lives in the objects; the adapters preserve existing integrations and wire payloads.
Topics, references and collection queries keep their named function APIs.

For a host configured with conversations and an `open` to `working` job transition:

```python
from baton.conversation import Conversation
from baton.jobs import Job

with connection(config.db_path) as db:
    created = Conversation.create(db, config, actor="planner", title="Release",
                                  participants=["planner", "worker"])
    thread = Conversation(db, created["conversation_id"])
    thread.send(config, sender="planner", text="Please check the release")
    history = thread.history("worker", config=config)

    created = Job.create(db, config.jobs, actor="planner", request_id="release-job-1", title="Release")
    job = Job(db, created["job_id"])
    current = job.read()
    result = job.move(config.jobs, "working", actor="worker", expected_revision=current["revision"])
```

Creation returns the public receipt, including whether it replayed an earlier request. A `Conversation`
or `Job` handle binds an existing ID to a connection; construction alone does not create or authorize it.
Those handles and inboxes live within their connection's lifetime. A `DirectMessage` is independent of
the connection: retain it, or retain its request ID, to retry the same submission safely.

## Database boundary

`database.Connection` owns SQLAlchemy execution, result rows, connection lifetime, write transactions
and read snapshots. All production driver imports live in this package. Domain code receives this
connection and uses `db.transaction()` or `db.snapshot()` for operations that must see one consistent state.
Closing a connection rolls back unfinished work. A snapshot inside an existing transaction borrows it.

`database.files.open_private` checks file ownership, permissions and path identity before returning a
connection. `store.connection` adds Baton's schema policy and closes the connection after use.
`store/schema.py` defines the current schema; `store/migrations.py` owns historical upgrade steps.
The secretary's private database uses the same connection boundary with its own schema.

SQLite is currently the supported backend. SQLAlchemy is the foundation for the intended PostgreSQL
backend; existing SQL, FTS5 search and writer-serialization assumptions still need explicit porting.
The connection does not rewrite SQL dialects or claim that a PostgreSQL URL alone enables support.
