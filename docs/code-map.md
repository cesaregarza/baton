# Reading the code

Start with the domain objects and follow one operation through the connection they receive.

```python
from baton.inbox import Inbox
from baton.messages import DirectMessage
from baton.store import connection

message = DirectMessage(sender="planner", recipient="worker", text="Please check this")
with connection(config.db_path) as db:
    receipt = message.send(db)
    page = Inbox(db, "worker", config).read()
```

This is trusted host-side code. MCP and browser entrypoints authenticate the caller before choosing its role.

## Domain concepts

- `messages.DirectMessage` is a submission addressed to one role. It owns validation, replay identity,
  mailbox admission and the atomic send. Reusing the same object reuses its request ID. The dictionary
  receipt reports whether that submission created a message or replayed an earlier one.
- `posts.Post` is the stored message read model. It has explicit fields for text, sender, conversation,
  attribution and attention. It decodes a database row and renders the existing public payload. A reply's
  visible parent is supplied separately after checking the reader's access.
- `inbox.Inbox` belongs to a role and a connection. `read()` delivers messages; `take()` delivers a page
  of events; `wait()` waits for events. It owns the delivery cursor and advances it atomically with the read.
- `message_content` contains pure content and retry-ID validation.

The older `messages.send/read` and `inbox.take/wait` functions remain compatibility entrypoints.
The CLI and MCP tools use the objects directly. Conversation posting, jobs, topics and references still
have named function APIs; those can be evolved independently while retaining their existing contracts.

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
