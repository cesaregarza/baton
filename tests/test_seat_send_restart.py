"""A seat send cut off by the process exiting, and a send whose sender leaves mid-way.

The first is a real exit: a child process takes a form's place in the hourly quota, then dies inside the store
step, before its release runs. The second pins the order of the seat's membership check and core's retry lookup.
Everything is synthetic: generic roles and throwaway tokens.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import free_port
from seat_helpers import conversation, get, hidden, log_in, make_seat_host, post, stored_by_seat

from baton import conversations, seat
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


def test_a_send_cut_off_by_the_process_exiting_frees_its_place_when_the_seat_starts_again(tmp_path) -> None:
    host = make_seat_host(tmp_path, seat_extra=f"listen_port = {free_port()}\n")
    cid = conversation(host)
    _, cookies = log_in(host.config, host.seat_token)
    done = _form(host, cid, cookies, "sent before the crash")
    assert post(host.config, f"/seat/c/{cid}/send", done, cookies=cookies).status == 303
    cut = _form(host, cid, cookies, "cut off mid-send")
    child = subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps({"config": str(host.path), "target": f"/seat/c/{cid}/send",
                                                  "fields": cut, "cookies": cookies})],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS.parent), str(TESTS)])},
        capture_output=True, text=True, timeout=60)
    assert child.returncode == CRASH, child.stderr
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
