"""Small SQLAlchemy-backed connection boundary for Baton's SQLite stores.

This module owns only connection mechanics. Schema policy and filesystem checks
remain with the stores that open authoritative or private databases.
"""

from __future__ import annotations

import sqlite3 as _sqlite3
from collections import OrderedDict
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.engine import Connection as _EngineConnection
from sqlalchemy.exc import DBAPIError, NoResultFound
from sqlalchemy.pool import NullPool, StaticPool

OperationalError = _sqlite3.OperationalError
__all__ = ["Connection", "Row", "OperationalError", "connect_sqlite", "memory", "transaction", "snapshot"]


class Row:
    """A result row with both SQLite-style positional and mapping access."""

    __slots__ = ("_values", "_keys")

    def __init__(self, row: Any) -> None:
        mapping = row._mapping
        self._values = tuple(row)
        self._keys = tuple(mapping.keys())

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._keys.index(key)]
            except ValueError as exc:
                raise KeyError(key) from exc
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Row):
            return NotImplemented
        return self._keys == other._keys and self._values == other._values

    def __hash__(self) -> int:
        return hash((self._keys, self._values))

    def keys(self) -> tuple[str, ...]:
        return self._keys


class _Result:
    __slots__ = ("_result",)

    def __init__(self, result: Any) -> None:
        self._result = result

    @property
    def rowcount(self) -> int:
        return 0 if self._result is None else self._result.rowcount

    def fetchone(self) -> Row | None:
        if self._result is None:
            return None
        row = self._result.fetchone()
        return None if row is None else Row(row)

    def one(self) -> Row:
        """Return exactly one row, refusing a missing or non-unique result."""
        if self._result is None:
            raise NoResultFound("expected exactly one row")
        return Row(self._result.one())

    def fetchall(self) -> list[Row]:
        return [] if self._result is None else [Row(row) for row in self._result.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        if self._result is None:
            return
        for row in self._result:
            yield Row(row)


class Connection:
    """An owned SQLAlchemy connection with explicit, SQLite-aware transactions."""

    __slots__ = ("_engine", "_connection", "_closed", "_dispose_engine_on_close", "_max_parameters")

    def __init__(self, engine: Engine, *, dispose_engine_on_close: bool = False) -> None:
        self._engine = engine
        self._dispose_engine_on_close = dispose_engine_on_close
        try:
            self._connection: _EngineConnection = engine.connect()
        except DBAPIError as exc:
            engine.dispose()
            self._translate_error(exc)
        self._closed = False
        self._max_parameters: int | None = None

    def execute(self, statement: Any, params: Sequence[Any] | Mapping[str, Any] = ()) -> _Result:
        if isinstance(statement, str):
            bindings = dict(params) if isinstance(params, Mapping) else tuple(params)
            result = self._call(self._connection.exec_driver_sql, statement, bindings)
        else:
            # Core Executables use SQLAlchemy's named-parameter interface.
            core_params = dict(params) if isinstance(params, Mapping) else (params or {})
            result = self._call(self._connection.execute, statement, core_params)
        return _Result(result)

    def executemany(self, statement: str, params: Sequence[tuple[Any, ...] | Mapping[str, Any]]) -> _Result:
        batch = list(params)
        # SQLAlchemy treats [] as an execution without parameters; an empty batch must do no work.
        return _Result(self._call(self._connection.exec_driver_sql, statement, batch) if batch else None)

    def set_trace_callback(self, callback: Any) -> None:
        """Set SQLite's supported SQL trace callback for diagnostics and empirical checks."""
        self._driver_connection().set_trace_callback(callback)

    def _driver_connection(self) -> Any:
        # Deliberately private: callers get only the explicit tracing hook above.
        return self._connection.connection.driver_connection

    @staticmethod
    def _call(function: Any, *args: Any) -> Any:
        try:
            return function(*args)
        except DBAPIError as exc:
            Connection._translate_error(exc)

    @staticmethod
    def _translate_error(exc: DBAPIError) -> None:
        if isinstance(exc.orig, _sqlite3.Error):
            raise exc.orig from exc
        raise exc

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._connection.close()
        finally:
            if self._dispose_engine_on_close:
                self._engine.dispose()
            self._closed = True

    def commit(self) -> None:
        """Commit pending work and clear SQLAlchemy's transaction bookkeeping."""
        self._call(self._connection.commit)

    def rollback(self) -> None:
        """Roll back pending work, including a transaction opened explicitly."""
        self._call(self._connection.rollback)

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def in_transaction(self) -> bool:
        return self._driver_connection().in_transaction

    @property
    def max_parameters(self) -> int:
        if self._max_parameters is None:
            self._max_parameters = self._driver_connection().getlimit(_sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        return self._max_parameters

    def table_exists(self, name: str) -> bool:
        return (
            self.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
            is not None
        )

    def transaction(self) -> AbstractContextManager[Connection]:
        return transaction(self)

    def snapshot(self) -> AbstractContextManager[None]:
        return snapshot(self)


@contextmanager
def transaction(conn: Any) -> Generator[Any]:
    """Run one immediate write transaction on this connection."""
    if conn.in_transaction:
        raise RuntimeError("nested write transactions are not supported")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def snapshot(conn: Any) -> Generator[None]:
    """Use an existing transaction, or own one read snapshot for this block."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


_ENGINE_CACHE_SIZE = 32
_ENGINE_CACHE: OrderedDict[URL, Engine] = OrderedDict()
_ENGINE_CACHE_LOCK = RLock()


def _engine(url: URL, *, memory_db: bool = False) -> Engine:
    if not memory_db:
        with _ENGINE_CACHE_LOCK:
            cached = _ENGINE_CACHE.get(url)
            if cached is not None:
                _ENGINE_CACHE.move_to_end(url)
                return cached
            engine = _create_engine(url)
            _ENGINE_CACHE[url] = engine
            if len(_ENGINE_CACHE) > _ENGINE_CACHE_SIZE:
                _, evicted = _ENGINE_CACHE.popitem(last=False)
                evicted.dispose()
            return engine
    return _create_engine(url, memory_db=True)


def _create_engine(url: URL, *, memory_db: bool = False) -> Engine:
    options: dict[str, Any] = {
        "isolation_level": "AUTOCOMMIT",
        "connect_args": {"timeout": 30, "check_same_thread": False},
    }
    if memory_db:
        options["poolclass"] = StaticPool
    else:
        options["poolclass"] = NullPool
    return create_engine(url, **options)


def connect_sqlite(path: Path | str, *, read_only: bool = False) -> Connection:
    """Open SQLite without applying store schema, PRAGMA, or filesystem policy."""
    database = str(Path(path))
    query: dict[str, str] = {}
    if read_only:
        # URI mode is required for SQLite to honor mode=ro. Quote URI delimiters
        # in filenames so '?' and '#' remain part of the path.
        database = "file:" + quote(Path(path).resolve().as_posix(), safe="/")
        query = {"mode": "ro", "uri": "true"}
    url = URL.create("sqlite+pysqlite", database=database, query=query)
    return Connection(_engine(url))


def memory() -> Connection:
    """Open an isolated in-memory SQLite connection."""
    return Connection(
        _engine(URL.create("sqlite+pysqlite", database=":memory:"), memory_db=True), dispose_engine_on_close=True
    )
