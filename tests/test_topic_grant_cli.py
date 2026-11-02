"""The owner's `baton topic-grant` commands, and what `baton alias arm-rekey` says about topic grants.

Driven through `baton.cli.main` with a config file, as the other owner commands are, and read from the
JSON it prints. The round trip is the one the owner follows: propose, authorize with the printed digest,
list, revoke. The key is printed once, by `authorize`, and shows up in nothing else.

Tests marked GUARD pin output that must not change and pass on a base without topic grants.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_per_call_identity import build_host
from test_topic_containers import ALIAS, OTHER_ALIAS, seed_aliases
from topic_backing import back_topics
from topic_grant_support import Clock, every_cell, grants_module

from baton import cli, store


@pytest.fixture
def host(tmp_path):
    made = build_host(tmp_path)
    conn = store.connect(made.config.db_path)
    seed_aliases(conn, ALIAS, OTHER_ALIAS)
    back_topics(conn, "job-hunt", "job-hunt/x", "reviews")
    conn.close()
    return made


class Owner:
    """Runs owner commands and keeps everything they printed."""

    def __init__(self, host, capsys) -> None:
        self.host, self.capsys, self.printed = host, capsys, []

    def run(self, *argv: str) -> tuple[int, object]:
        try:
            code = cli.main(["--config", str(self.host.config_path), *argv])
        except SystemExit as error:  # argparse refusing the command line
            code = error.code
        out = self.capsys.readouterr().out
        self.printed.append(out)
        return code, (json.loads(out) if out.strip() else None)

    def ok(self, *argv: str) -> object:
        code, result = self.run(*argv)
        assert code == 0, f"`baton {' '.join(argv)}` exited {code}: {result}"
        return result

    def refused(self, *argv: str) -> dict:
        """A command that must exit 1 with a refusal: its `{"error", "detail"}` object."""
        code, result = self.run(*argv)
        assert code == 1, f"`baton {' '.join(argv)}` exited {code}, not 1: {result}"
        assert isinstance(result, dict) and "error" in result, result
        return result

    def propose(self, alias="lane-a", topic="job-hunt", *extra: str):
        return self.ok("topic-grant", "propose", alias, topic, "--capability", "reference.edit", "--days", "7", *extra)

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.host.config.db_path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def owner(host, capsys):
    return Owner(host, capsys)


# 1. The round trip ---------------------------------------------------------------------------------------

def test_propose_prints_the_proposal_the_owner_must_read_before_authorizing(owner):
    proposal = owner.propose("lane-a", "job-hunt", "--capability", "thread.organise", "--capability", "reference.edit",
                             "--inherit")
    assert re.fullmatch(r"tgr_[0-9a-f]{16}", proposal["id"])
    assert (proposal["alias_name"], proposal["topic_path"]) == ("lane-a", "job-hunt")
    assert proposal["capabilities"] == ["reference.edit", "thread.organise"]
    assert int(proposal["inherit"]) == 1
    assert proposal["state"] == "pending" and re.fullmatch(r"[0-9a-f]{64}", proposal["digest"])
    expires = datetime.fromisoformat(proposal["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(expires.tzinfo) <= timedelta(days=7)
    assert "grant_key" not in proposal


def test_propose_without_inherit_makes_a_grant_for_that_topic_alone(owner):
    assert int(owner.propose()["inherit"]) == 0


def test_the_whole_round_trip_propose_authorize_list_revoke(owner):
    proposal = owner.propose()
    granted = owner.ok("topic-grant", "authorize", proposal["id"], proposal["digest"])
    key = granted["grant_key"]
    assert re.fullmatch(r"tgk_[A-Za-z0-9_-]{43}", key)
    assert granted["state"] == "active"
    note = granted["note"].lower()
    assert "chat" in note and "thread" in note, "the note says the key goes through Cesar's own chat, never a thread"

    [listed] = owner.ok("topic-grant", "list")
    assert (listed["id"], listed["state"]) == (proposal["id"], "active")

    owner.ok("topic-grant", "revoke", proposal["id"], "--reason", "the lane is finished")
    assert owner.ok("topic-grant", "list") == []
    [kept] = owner.ok("topic-grant", "list", "--all")
    assert (kept["id"], kept["state"]) == (proposal["id"], "revoked")

    # The key was printed once, by authorize, and by nothing else; it is in no table either.
    assert [out.count(key) for out in owner.printed] == [0, 1, 0, 0, 0, 0]
    with owner.db() as conn:
        assert not [cell for cell in every_cell(conn) if key in cell]
        assert [(e["event"], e["actor"]) for e in conn.execute("SELECT * FROM topic_grant_events ORDER BY seq")] == \
            [("propose", "owner"), ("authorize", "owner"), ("revoke", "owner")]
        row = conn.execute("SELECT * FROM topic_grants").fetchone()
        assert (row["revoked_by"], row["revoke_reason"]) == ("owner", "the lane is finished")


def test_list_shows_pending_and_active_grants_and_all_shows_the_rest(owner):
    pending = owner.propose("lane-a", "job-hunt")
    active = owner.propose("lane-b", "reviews")
    owner.ok("topic-grant", "authorize", active["id"], active["digest"])
    cancelled = owner.propose("lane-a", "reviews")
    owner.ok("topic-grant", "revoke", cancelled["id"], "--reason", "wrong topic")
    assert {one["id"]: one["state"] for one in owner.ok("topic-grant", "list")} == \
        {pending["id"]: "pending", active["id"]: "active"}
    assert {one["id"]: one["state"] for one in owner.ok("topic-grant", "list", "--all")} == \
        {pending["id"]: "pending", active["id"]: "active", cancelled["id"]: "revoked"}


def test_a_listing_carries_neither_the_proof_nor_the_nonce(owner):
    proposal = owner.propose()
    owner.ok("topic-grant", "authorize", proposal["id"], proposal["digest"])
    with owner.db() as conn:
        row = conn.execute("SELECT proof_digest, nonce FROM topic_grants").fetchone()
    text = json.dumps(owner.ok("topic-grant", "list", "--all"))
    assert row["proof_digest"] not in text and row["nonce"] not in text


# 2. Refusals ---------------------------------------------------------------------------------------------

def test_authorize_with_the_wrong_digest_exits_1_prints_no_key_and_leaves_it_pending(owner):
    proposal = owner.propose()
    refused = owner.refused("topic-grant", "authorize", proposal["id"], "0" * 64)
    assert "grant_key" not in refused
    [still] = owner.ok("topic-grant", "list")
    assert still["state"] == "pending"


@pytest.mark.parametrize("days", ["0", "31"])
def test_propose_refuses_days_outside_1_to_30(owner, days):
    refused = owner.refused("topic-grant", "propose", "lane-a", "job-hunt", "--capability", "reference.edit",
                            "--days", days)
    assert refused["error"] == "invalid_request"
    assert owner.ok("topic-grant", "list", "--all") == []


def test_propose_refuses_an_unknown_capability(owner):
    refused = owner.refused("topic-grant", "propose", "lane-a", "job-hunt", "--capability", "reference.delete",
                            "--days", "7")
    assert refused["error"] == "invalid_request"


def test_propose_without_a_capability_or_without_days_is_refused(owner):
    owner.ok("topic-grant", "list")  # the command exists; only these two command lines are wrong
    assert owner.run("topic-grant", "propose", "lane-a", "job-hunt", "--days", "7")[0] != 0
    assert owner.run("topic-grant", "propose", "lane-a", "job-hunt", "--capability", "reference.edit")[0] != 0
    assert owner.ok("topic-grant", "list", "--all") == []


def test_propose_names_a_missing_alias_or_topic(owner):
    owner.refused("topic-grant", "propose", "nobody", "job-hunt", "--capability", "reference.edit", "--days", "7")
    refused = owner.refused("topic-grant", "propose", "lane-a", "nowhere", "--capability", "reference.edit",
                            "--days", "7")
    assert refused["error"] == "unknown_topic"


def test_revoke_needs_a_reason(owner):
    proposal = owner.propose()
    assert owner.run("topic-grant", "revoke", proposal["id"])[0] != 0
    assert owner.refused("topic-grant", "revoke", proposal["id"], "--reason", "   ")["error"] == "invalid_request"
    assert owner.ok("topic-grant", "list")[0]["state"] == "pending"


def test_authorize_and_revoke_refuse_an_unknown_grant(owner):
    owner.refused("topic-grant", "authorize", "tgr_0000000000000000", "0" * 64)
    owner.refused("topic-grant", "revoke", "tgr_0000000000000000", "--reason", "x")


# 3. arm-rekey and topic grants ---------------------------------------------------------------------------

def test_arm_rekey_prints_what_it_always_has_when_the_alias_has_no_grants(owner):  # GUARD
    code, armed = owner.run("alias", "arm-rekey", "lane-a")
    assert code == 0, armed
    assert set(armed) == {"armed", "target", "expires_at"}
    assert (armed["armed"], armed["target"]) == ("rekey", "lane-a")
    assert isinstance(armed["expires_at"], float)


def test_arm_rekey_names_the_aliass_active_topic_grants_and_only_those(owner, monkeypatch):
    mine = owner.propose("lane-a", "job-hunt")
    owner.ok("topic-grant", "authorize", mine["id"], mine["digest"])
    mine_too = owner.propose("lane-a", "reviews")
    owner.ok("topic-grant", "authorize", mine_too["id"], mine_too["digest"])
    pending = owner.propose("lane-a", "reviews")
    cancelled = owner.propose("lane-a", "reviews")
    owner.ok("topic-grant", "revoke", cancelled["id"], "--reason", "wrong")
    short = owner.ok("topic-grant", "propose", "lane-a", "job-hunt", "--capability", "reference.edit", "--days", "1")
    owner.ok("topic-grant", "authorize", short["id"], short["digest"])
    theirs = owner.propose("lane-b", "job-hunt")
    owner.ok("topic-grant", "authorize", theirs["id"], theirs["digest"])
    # The one-day grant runs out; the clock is the module's, as everywhere.
    clock = Clock(datetime.now(UTC) + timedelta(days=2))
    monkeypatch.setattr(grants_module(), "_now", clock)

    armed = owner.ok("alias", "arm-rekey", "lane-a")
    assert {"armed", "target", "expires_at"} <= set(armed)
    assert sorted(armed["active_topic_grants"]) == sorted([mine["id"], mine_too["id"]]), \
        "pending, revoked, expired and another alias's grants are not listed"
    assert pending["id"] not in armed["active_topic_grants"]


def test_arm_rekey_for_the_other_alias_lists_its_own_grant(owner):
    theirs = owner.propose("lane-b", "job-hunt")
    owner.ok("topic-grant", "authorize", theirs["id"], theirs["digest"])
    assert owner.ok("alias", "arm-rekey", "lane-b")["active_topic_grants"] == [theirs["id"]]
    assert "active_topic_grants" not in owner.ok("alias", "arm-rekey", "lane-a")
