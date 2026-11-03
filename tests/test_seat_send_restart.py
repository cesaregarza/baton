"""A seat send cut off by the process exiting, a second host started over the same database, and a send whose
sender leaves mid-way.

The first is a real exit: a child process takes a form's place in the hourly quota, then dies inside the store
step, before its release runs. The second is two hosts over one database: only the host that holds the database's
lock may free the places a crash left, so a second startup, whatever its port, must leave a live send alone. The
third is the seat's membership checks around core's send, which itself checks membership before it replays.
Everything is synthetic: generic roles and throwaway tokens.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import free_port
from seat_helpers import conversation, get, hidden, log_in, make_seat_host, post, stored_by_seat

from baton import config as config_module
from baton import conversations, seat
from baton.config import ConfigError
from baton.store import connect

TESTS = Path(__file__).parent
CRASH = 17
# The child: everything as the server would do it, except that the store step ends the process on the spot, as a
# kill or a power cut would, so `_send`'s release (its `finally`) never runs.
CHILD = f"""
import json, os, sys
from baton import config as config_module, seat
from seat_helpers import post
args = json.loads(sys.argv[1])
seat._store = lambda *a, **k: os._exit({CRASH})
post(config_module.load(args["config"]), args["target"], args["fields"], cookies=args["cookies"])
"""


def _form(host, cid, cookies, text: str) -> dict[str, str]:
    page = get(host.config, f"/seat/c/{cid}", cookies=cookies)
    return {"csrf": hidden(page.body, "csrf"), "request_id": hidden(page.body, "request_id"), "text": text}


def _row(host, request_id: str) -> tuple:
    db = sqlite3.connect(host.config.db_path)
    try:
        return db.execute("SELECT sent_at IS NOT NULL, holders FROM seat_requests WHERE request_id = ?",
                          (request_id,)).fetchone()
    finally:
        db.close()


def _counted(host) -> int:
    """The seat's sends this hour, as its quota counts them."""
    db = sqlite3.connect(host.config.db_path)
    try:
        return db.execute("SELECT COUNT(*) FROM seat_requests WHERE sent_at IS NOT NULL").fetchone()[0]
    finally:
        db.close()


def _reservations(host) -> list[tuple]:
    """Every form's place in the quota exactly as the table holds it: its id, when it was taken, its holders."""
    db = sqlite3.connect(host.config.db_path)
    try:
        return db.execute("SELECT request_id, sent_at, holders FROM seat_requests ORDER BY request_id").fetchall()
    finally:
        db.close()


def _crash_mid_send(host, cid, cookies, text: str) -> dict[str, str]:
    """A form whose send takes its place and then ends its process inside the store step. Returns the form."""
    cut = _form(host, cid, cookies, text)
    child = subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps({"config": str(host.path), "target": f"/seat/c/{cid}/send",
                                                  "fields": cut, "cookies": cookies})],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS.parent), str(TESTS)])},
        capture_output=True, text=True, timeout=60)
    assert child.returncode == CRASH, child.stderr
    return cut


def _on_port(host, port: int):
    """The host's config again with the seat on `port`: a second config over the same database."""
    second = host.path.with_name("second.toml")
    second.write_text(host.path.read_text().replace(f"listen_port = {host.config.seat.listen_port}",
                                                    f"listen_port = {port}"))
    return config_module.load(second)


def _start(config):
    """A seat startup for `config`: (its server, None) if it came up, (None, the refusal) if it was refused."""
    try:
        return seat.server(config), None
    except ConfigError as refusal:
        return None, refusal


def _send_meanwhile(monkeypatch, host, cid, cookies, text: str, meanwhile) -> tuple[dict[str, str], object]:
    """Post a new form for `text`. `meanwhile(form)` runs once the send has taken its place, before it stores.

    The send is whole and in this process, so what `meanwhile` does, it does while the place is truly held.
    """
    form = _form(host, cid, cookies, text)
    real = seat._store

    def store_after_meanwhile(*args, **kwargs):
        meanwhile(form)
        return real(*args, **kwargs)

    monkeypatch.setattr(seat, "_store", store_after_meanwhile)
    return form, post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies)


def test_a_send_cut_off_by_the_process_exiting_frees_its_place_when_the_seat_starts_again(tmp_path) -> None:
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    done = _form(host, cid, cookies, "sent before the crash")
    assert post(host.config, f"/seat/c/{cid}/send", done, cookies=cookies).status == 303
    cut = _crash_mid_send(host, cid, cookies, "cut off mid-send")
    # What the exit left: the place taken and held, nothing stored under the form's id.
    assert _row(host, cut["request_id"]) == (1, 1)
    assert [row[2] for row in stored_by_seat(host)] == ["sent before the crash"]
    assert _counted(host) == 2

    bound = seat.server(host.config)   # the seat starting again, as the host does at startup
    try:
        assert _row(host, cut["request_id"]) == (0, 0), "the cut-off send still holds a place in the quota"
        assert _row(host, done["request_id"]) == (1, 0), "a stored send must stay counted"
        assert _counted(host) == 1
        # The same form can now be sent, once, and is counted once.
        assert post(host.config, f"/seat/c/{cid}/send", cut, cookies=cookies).status == 303
        assert [row[2] for row in stored_by_seat(host)] == ["sent before the crash", "cut off mid-send"]
        assert _row(host, cut["request_id"]) == (1, 0) and _counted(host) == 2
    finally:
        if bound is not None:
            bound.server_close()


def _leave_before_core_sends(monkeypatch, host, cid: str) -> None:
    """Remove the seat's role from the conversation after the seat's own membership check, just before core's
    send runs: the window between the two checks."""
    real = conversations.send

    def removed_first(conn, config, **kw):
        conversations.remove(conn, config, cid, "operator", actor="orchestrator")
        monkeypatch.setattr(conversations, "send", real)
        return real(conn, config, **kw)

    monkeypatch.setattr(conversations, "send", removed_first)


def test_a_retried_form_does_not_report_success_to_someone_who_left_meanwhile(tmp_path, monkeypatch) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host, ("orchestrator", "reviewer", "operator"))   # two stay after the seat's role leaves
    _, cookies = log_in(host.config, host.seat_token)
    form = _form(host, cid, cookies, "sent while still a member")
    assert post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies).status == 303
    _leave_before_core_sends(monkeypatch, host, cid)
    retried = post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies)
    assert retried.status == 403, "the earlier send's success was reported to someone no longer in the conversation"
    assert "no longer in this conversation" in retried.body.decode()
    assert [row[2] for row in stored_by_seat(host)] == ["sent while still a member"]


def test_a_new_form_from_someone_who_left_meanwhile_is_refused_and_stores_nothing(tmp_path, monkeypatch) -> None:
    host = make_seat_host(tmp_path)
    cid = conversation(host, ("orchestrator", "reviewer", "operator"))
    _, cookies = log_in(host.config, host.seat_token)
    form = _form(host, cid, cookies, "never sent")
    _leave_before_core_sends(monkeypatch, host, cid)
    refused = post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies)
    assert refused.status == 403 and "nothing was sent" in refused.body.decode()
    assert stored_by_seat(host) == []
    conn = connect(host.config.db_path)
    try:
        assert tuple(conn.execute("SELECT sent_at, holders FROM seat_requests WHERE request_id = ?",
                                  (form["request_id"],)).fetchone()) == (None, 0), "a refused send must free its place"
    finally:
        conn.close()


def test_a_second_startup_on_another_port_is_refused_and_a_live_send_counts_once(tmp_path, monkeypatch) -> None:
    """A second host started over a live send's database leaves that send's place alone.

    The first seat is up and a send is in flight: its place taken, nothing stored yet. A second startup, on another
    port so that binding can't stop it, must be refused by name before it touches any reservation. The send then
    stores and is counted exactly once, not left uncounted with its holders at -1.
    """
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    running = seat.server(host.config)
    seen: dict = {}

    def a_second_host_starts(form) -> None:
        seen["held"] = _row(host, form["request_id"])
        seen["server"], seen["refusal"] = _start(_on_port(host, free_port()))
        seen["still"] = _row(host, form["request_id"])

    try:
        form, sent = _send_meanwhile(monkeypatch, host, cid, cookies, "sent as a second host starts",
                                     a_second_host_starts)
    finally:
        for bound in (running, seen.get("server")):
            if bound is not None:
                bound.server_close()
    assert sent.status == 303 and seen["held"] == (1, 1), "the send was not in flight with its place held"
    stored = [row[2] for row in stored_by_seat(host)]
    assert (seen["still"], _row(host, form["request_id"]), _counted(host), stored) == (
        (1, 1), (1, 0), 1, ["sent as a second host starts"]), "the second startup freed a live send's place"
    refusal = seen["refusal"]
    assert refusal is not None and refusal.code == "seat_already_running", "the second startup was not refused by name"


def test_a_startup_that_cannot_take_the_lock_frees_nothing(tmp_path, monkeypatch) -> None:
    """A refused startup changes no reservation, whatever state each one is in.

    With one seat up, the table holds a stored send, a form never sent and a send in flight. A second startup is
    refused, and every row is then exactly as it was. Not even a place that looks cut off may be freed by a host
    that does not hold the lock: it can't tell a cut-off place from a live one.
    """
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    done = _form(host, cid, cookies, "sent earlier")
    assert post(host.config, f"/seat/c/{cid}/send", done, cookies=cookies).status == 303
    _form(host, cid, cookies, "opened, never sent")
    running = seat.server(host.config)
    seen: dict = {}

    def a_second_host_starts(_) -> None:
        seen["before"] = _reservations(host)
        seen["server"], seen["refusal"] = _start(_on_port(host, free_port()))
        seen["after"] = _reservations(host)

    try:
        _send_meanwhile(monkeypatch, host, cid, cookies, "in flight", a_second_host_starts)
    finally:
        for bound in (running, seen.get("server")):
            if bound is not None:
                bound.server_close()
    states = sorted((at is not None, holders) for _, at, holders in seen["before"])
    assert states == [(False, 0), (True, 0), (True, 1)], "the table did not hold a never-sent, a stored and a live form"
    assert seen["after"] == seen["before"], "a startup that was refused freed a place"
    refusal = seen["refusal"]
    assert refusal is not None and refusal.code == "seat_already_running", "the second startup was not refused by name"


def test_a_startup_that_takes_the_lock_but_not_the_port_frees_nothing_and_gives_the_lock_back(tmp_path) -> None:
    """A host that gets the lock and then finds its port taken changes nothing and keeps nothing.

    The port may belong to a host that is serving this database, so the failed startup must not free any place:
    it binds first and frees only once it is going to serve. It must not keep the lock either: the next startup,
    on a free port, takes it and frees the place the crash left.
    """
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    done = _form(host, cid, cookies, "sent before the crash")
    assert post(host.config, f"/seat/c/{cid}/send", done, cookies=cookies).status == 303
    cut = _crash_mid_send(host, cid, cookies, "cut off mid-send")
    before = _reservations(host)
    with socket.socket() as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        with pytest.raises(OSError) as failed:
            seat.server(_on_port(host, occupant.getsockname()[1]))
    assert failed.value.errno == errno.EADDRINUSE
    assert _reservations(host) == before, "a startup that could not bind freed a place"
    bound, refusal = _start(host.config)
    try:
        assert refusal is None, "the startup that could not bind kept the lock"
        assert _row(host, cut["request_id"]) == (0, 0) and _row(host, done["request_id"]) == (1, 0)
    finally:
        if bound is not None:
            bound.server_close()


def test_a_startup_whose_recovery_fails_gives_back_its_port_and_its_lock(tmp_path, monkeypatch) -> None:
    """A startup that fails last gives back both of the things it took first.

    With the lock held and the port bound, freeing the cut-off sends fails. The error reaches the caller, and the
    next startup, on the same port over the same database, comes up: neither the listener nor the lock was kept.
    The caller keeps the error, and so the failed startup's frame and the listener in it: only the startup closing
    the listener itself frees the port, not the listener being collected later.
    """
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    real = seat._free_cut_off_sends

    def locked_out(config) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(seat, "_free_cut_off_sends", locked_out)
    with pytest.raises(sqlite3.OperationalError) as failed:
        seat.server(host.config)
    monkeypatch.setattr(seat, "_free_cut_off_sends", real)
    bound, refusal = _start(host.config)
    try:
        assert refusal is None and bound is not None, "the failed startup kept the lock"
        assert "database is locked" in str(failed.value)   # also keeps `failed` in use until here
    finally:
        if bound is not None:
            bound.server_close()


def _leave_after_core_replays(monkeypatch, host, cid: str, created: list[bool]) -> None:
    """Remove the seat's role from the conversation as core's send returns, before the seat reads the result: the
    window the seat's recheck of a replayed send covers. `created` collects what core reported."""
    real = conversations.send

    def removed_after(conn, config, **kw):
        result = real(conn, config, **kw)
        created.append(result["created"])
        conversations.remove(conn, config, cid, "operator", actor="orchestrator")
        return result

    monkeypatch.setattr(conversations, "send", removed_after)


def test_a_member_removed_after_core_replays_a_retry_is_told_they_left(tmp_path, monkeypatch) -> None:
    """The seat rechecks membership after core answers a retry from its record.

    Core checks membership before it replays, so the removal tests above end in core's refusal and never reach the
    seat's recheck. This one removes the role after core has replayed the earlier send and before the seat reads
    the result. The seat must say the role is no longer in the conversation, without saying nothing was sent (the
    earlier send is stored and stays counted), instead of redirecting to a message the role can no longer open.
    """
    host = make_seat_host(tmp_path)
    cid = conversation(host, ("orchestrator", "reviewer", "operator"))   # two stay after the seat's role leaves
    _, cookies = log_in(host.config, host.seat_token)
    form = _form(host, cid, cookies, "sent while still a member")
    assert post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies).status == 303
    created: list[bool] = []
    _leave_after_core_replays(monkeypatch, host, cid, created)
    retried = post(host.config, f"/seat/c/{cid}/send", form, cookies=cookies)
    assert created == [False], "core did not answer from its record, so the seat's recheck was not reached"
    body = retried.body.decode()
    assert retried.status == 403, "a replayed send was reported to someone who had left the conversation"
    assert "no longer in this conversation." in body and "nothing was sent" not in body
    assert [row[2] for row in stored_by_seat(host)] == ["sent while still a member"]
    assert _row(host, form["request_id"]) == (1, 0), "the earlier send must stay counted, with its holder released"
