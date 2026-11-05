"""Change-feed cursors keep their refusal contract at the numeric and SQLite boundaries."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from baton import changes, cli
from baton.database import Connection, memory
from baton.errors import CursorExpired, InvalidRequest

INVALID_DETAIL = "a cursor is <log_epoch>:<change_seq>, as next_cursor gave it"
EXPIRED_DETAIL = (
    "that cursor is from another log or older than the oldest change kept; rebuild from "
    "state, then continue from reset_cursor"
)
INVALID_NUMBERS = [
    pytest.param("²", id="non-decimal-digit"),
    pytest.param("1²", id="mixed-digits"),
    pytest.param("9223372036854775808", id="sqlite-overflow"),
    pytest.param("9" * 4301, id="python-digit-limit"),
]


@pytest.fixture
def log() -> Iterator[Connection]:
    conn = memory()
    statements = """
        CREATE TABLE log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO log_meta VALUES ('log_epoch', 'current'), ('pruned_through', '0');
        CREATE TABLE change_log (
            change_seq INTEGER PRIMARY KEY, conversation_id TEXT, revision INTEGER, kind TEXT,
            message_id TEXT, role TEXT, at TEXT
        );
        INSERT INTO change_log VALUES (5, 'conversation', 1, 'message_added', 'm1', NULL, '2026-01-01');
        INSERT INTO change_log VALUES (11, 'conversation', 2, 'message_added', 'm2', NULL, '2026-01-02');
    """
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("PRAGMA query_only = ON")
    yield conn
    conn.close()


@pytest.fixture(params=["read", "wait"])
def operation(request) -> str:
    return request.param


def follow(conn: Connection, operation: str, cursor: str) -> dict:
    driver = conn._driver_connection()
    before = driver.total_changes
    try:
        if operation == "read":
            return changes.read(conn, changes.Scope.ALL, cursor=cursor)
        return changes.wait(conn, changes.Scope.ALL, cursor=cursor, timeout_seconds=0)
    finally:
        assert driver.total_changes == before
        assert not conn.in_transaction


@pytest.mark.parametrize("number", INVALID_NUMBERS)
def test_invalid_numbers_are_refusals(log, operation, number: str) -> None:
    with pytest.raises(InvalidRequest) as raised:
        follow(log, operation, f"current:{number}")
    assert str(raised.value) == INVALID_DETAIL


@pytest.mark.parametrize(
    ("number", "position", "remaining"),
    [
        ("0", 0, [5, 11]),
        ("0005", 5, [11]),
        ("٥", 5, [11]),
        ("٠5", 5, [11]),
        ("１２", 12, []),
        ("100", 100, []),
        ("9223372036854775807", 9223372036854775807, []),
        pytest.param("0" * 1000 + "5", 5, [11], id="many-leading-zeros"),
    ],
)
def test_accepted_numbers_keep_their_position(log, operation, number: str, position: int, remaining: list[int]) -> None:
    result = follow(log, operation, f"current:{number}")
    if operation == "read":
        assert [entry["change_seq"] for entry in result["entries"]] == remaining
        assert result["next_cursor"] == f"current:{remaining[-1] if remaining else position}"
    else:
        assert result == ({"change_seq": remaining[0]} if remaining else {"timed_out": True})


@pytest.mark.parametrize("number", ["0", "٥", *INVALID_NUMBERS])
def test_foreign_epoch_is_expired_before_numeric_conversion(log, operation, number: str) -> None:
    with pytest.raises(CursorExpired) as raised:
        follow(log, operation, f"foreign:{number}")
    assert str(raised.value) == EXPIRED_DETAIL
    assert raised.value.extra == {"resync": True, "reset_cursor": "current:0"}


@pytest.mark.parametrize("stamp", ["current", "foreign"])
@pytest.mark.parametrize("number", ["", "abc", "-1", "+1", " 1", "1_0", "1:2"])
def test_invalid_syntax_is_refused_before_epoch_checks(log, operation, stamp: str, number: str) -> None:
    with pytest.raises(InvalidRequest) as raised:
        follow(log, operation, f"{stamp}:{number}")
    assert str(raised.value) == INVALID_DETAIL


@pytest.mark.parametrize("number", ["0002", "٢"])
def test_pruned_cursors_keep_the_reset_position(log, operation, number: str) -> None:
    log.execute("PRAGMA query_only = OFF")
    log.execute("UPDATE log_meta SET value = '3' WHERE key = 'pruned_through'")
    log.execute("PRAGMA query_only = ON")
    with pytest.raises(CursorExpired) as raised:
        follow(log, operation, f"current:{number}")
    assert str(raised.value) == EXPIRED_DETAIL
    assert raised.value.extra == {"resync": True, "reset_cursor": "current:3"}


@pytest.mark.parametrize("command", ["changes", "wait"])
@pytest.mark.parametrize("number", INVALID_NUMBERS)
def test_cli_refuses_invalid_numbers_as_json_without_writing(host, conn, capsys, command: str, number: str) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = {path.name: path.read_bytes() for path in host.config.data_dir.glob("baton.db*")}
    config_path = host.config.data_dir.parent / "baton.toml"
    args = ["--config", str(config_path), "index", command, "--after", f"{changes.epoch(conn)}:{number}"]
    if command == "wait":
        args.extend(["--timeout", "0"])

    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "invalid_request", "detail": INVALID_DETAIL}
    assert captured.err == ""
    assert {path.name: path.read_bytes() for path in host.config.data_dir.glob("baton.db*")} == before
