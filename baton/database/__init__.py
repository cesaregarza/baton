"""Database connections and schema lifecycle.

Use connection() to own a connection's lifetime; operations own their transactions.
The driver, checked files, current schema and migrations have separate modules.
"""

from .driver import Connection, OperationalError, Row, connect_sqlite, memory, snapshot, transaction
from .store import SCHEMA_VERSION, SchemaMismatch, connect, connect_read_only, connection

__all__ = [
    "Connection",
    "OperationalError",
    "Row",
    "SCHEMA_VERSION",
    "SchemaMismatch",
    "connect",
    "connect_read_only",
    "connect_sqlite",
    "connection",
    "memory",
    "snapshot",
    "transaction",
]
