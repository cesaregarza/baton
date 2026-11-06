"""Topic grant records: propose, authorize, revoke, list, the derived states and the event log.

What this pins (the spec is docs for S1, "topic grant records and the owner CLI"):
* `propose` writes one row and one `propose` event and returns the proposal the human reads: the alias and
  topic as they were named, the capabilities, the dates and a digest anyone can recompute.
* `authorize` takes that digest back, mints a key shown once, and stores only the key's sha256.
* `revoke` only ever fills the revocation fields. Nothing is deleted.
* A grant's state is derived from the row and the clock, never stored, and checked in a fixed order.
* Every operation is one transaction, and a refused one writes nothing.

Tests of new behaviour fail as assertions against a base without `baton/topic_grants.py` (see
topic_grant_support.py). Mutant numbers in comments refer to the spec's list of one-line mutants.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta

import pytest
import topic_grant_support as support
from topic_grant_support import (
    CAPABILITIES,
    CHILD,
    GRANT_COLUMNS,
    PROPOSAL_COLUMNS,
    ROOT,
    START,
    alias_code,
    bring_to,
    canonical_digest,
    every_cell,
    refusal,
)

from baton import topics

build_world = support.build_world  # the fixture, under the name the tests ask for

MICRO = timedelta(microseconds=1)


def words(error: Exception) -> str:
    return str(error).lower()


# 1. propose --------------------------------------------------------------------------------------------


def test_propose_returns_the_rendered_proposal(build_world):
    w = build_world()
    proposal = w.propose("lane-a", ROOT, ("topic.create_sub", "reference.edit", "reference.edit"), True, 7)
    assert re.fullmatch(r"tgr_[0-9a-f]{16}", proposal["id"])
    assert (proposal["alias_name"], proposal["alias_id"]) == ("lane-a", w.alias_a["alias_id"])
    assert proposal["alias_code"] == alias_code(w.alias_a["alias_id"])
    assert (proposal["topic_path"], proposal["topic_id"]) == (ROOT, w.topic_id(ROOT))
    assert proposal["capabilities"] == ["reference.edit", "topic.create_sub"], "sorted, duplicates collapsed"
    assert int(proposal["inherit"]) == 1
    assert proposal["expires_at"] == (START + timedelta(days=7)).isoformat()
    assert proposal["proposal_expires_at"] == (START + timedelta(hours=24)).isoformat()
    assert proposal["state"] == "pending"
    assert proposal["digest"] == canonical_digest(w.row(proposal["id"])), "the digest is the spec's, recomputable"
    assert "grant_key" not in proposal


def test_propose_stores_one_row_with_every_proposal_field_and_nothing_else(build_world):
    w = build_world()
    proposal = w.propose("lane-b", CHILD, ("thread.organise",), False, 3)
    [row] = w.rows()
    assert set(row.keys()) == GRANT_COLUMNS
    assert row["id"] == proposal["id"]
    assert (row["alias_id"], row["topic_id"]) == (w.alias_b["alias_id"], w.topic_id(CHILD))
    assert json.loads(row["capabilities"]) == ["thread.organise"]
    assert row["inherit"] == 0 and isinstance(row["inherit"], int)
    assert row["proposed_by"] == "owner"
    assert row["proposed_at"] == START.isoformat()
    assert row["expires_at"] == (START + timedelta(days=3)).isoformat()
    assert row["proposal_expires_at"] == (START + timedelta(hours=24)).isoformat()
    assert re.fullmatch(r"[0-9a-f]{32}", row["nonce"])
    assert re.fullmatch(r"[0-9a-f]{64}", row["digest"])
    assert (row["alias_name_seen"], row["alias_code_seen"], row["topic_path_seen"]) == (
        "lane-b",
        alias_code(w.alias_b["alias_id"]),
        CHILD,
    )
    for column in (
        "authorized_by",
        "authorized_at",
        "authorized_digest",
        "proof_digest",
        "revoked_at",
        "revoked_by",
        "revoke_reason",
    ):
        assert row[column] is None, column


def test_propose_writes_one_propose_event_by_the_owner(build_world):
    w = build_world()
    proposal = w.propose()
    [event] = w.events()
    assert (event["seq"], event["grant_id"], event["event"], event["actor"]) == (1, proposal["id"], "propose", "owner")
    assert event["at"] == START.isoformat()
    assert event["detail"] is None or isinstance(json.loads(event["detail"]), (dict, list))


def test_two_proposals_for_the_same_pair_are_two_grants_with_nothing_shared(build_world):
    w = build_world()
    first, second = w.propose(), w.propose()
    assert first["id"] != second["id"]
    assert first["digest"] != second["digest"], "a fresh nonce makes every proposal's digest its own"
    assert w.row(first["id"])["nonce"] != w.row(second["id"])["nonce"]


@pytest.mark.parametrize(
    "what,alias,topic,code",
    [
        ("an unknown alias", "nobody", ROOT, "forbidden"),
        ("a prefix of an alias", "lane", ROOT, "forbidden"),
        ("an unknown topic", "lane-a", "nowhere", "unknown_topic"),
        ("a prefix of a topic", "lane-a", "job-hun", "unknown_topic"),
        ("a topic with a trailing slash", "lane-a", ROOT + "/", "unknown_topic"),
    ],
)
def test_propose_refuses_a_missing_alias_or_topic_and_writes_nothing(build_world, what, alias, topic, code):
    w = build_world()
    error = refusal(w.propose, alias, topic)
    assert error.code == code, f"{what}: {error}"
    assert w.rows() == [] and w.events() == []


# Mutant 8: `days` of 0 or 31 must be refused, and 1 and 30 must not be.
@pytest.mark.parametrize("days", [0, 31, -1, 365, "7", 7.5, None, True])
def test_propose_refuses_days_outside_1_to_30(build_world, days):
    w = build_world()
    assert refusal(w.propose, days=days).code == "invalid_request"
    assert w.rows() == [] and w.events() == []


@pytest.mark.parametrize("days", [1, 30])
def test_propose_accepts_the_first_and_last_day(build_world, days):
    w = build_world()
    proposal = w.propose(days=days)
    assert proposal["expires_at"] == (START + timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    "capabilities",
    [[], ["bogus"], ["reference.edit", "reference.delete"], ["Reference.Edit"], [""], ["reference.edit "]],
)
def test_propose_refuses_no_capability_and_any_name_but_the_three(build_world, capabilities):
    w = build_world()
    assert refusal(w.propose, capabilities=capabilities).code == "invalid_request"
    assert w.rows() == [] and w.events() == []


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_each_of_the_three_capabilities_is_accepted_alone(build_world, capability):
    w = build_world()
    assert w.propose(capabilities=(capability,))["capabilities"] == [capability]


# 2. The digest -------------------------------------------------------------------------------------------


# Mutant 4 (the recomputed-digest check) and the digest's field list: changing any one field the digest covers
# after authorization makes the grant invalid, and changing a field it does not cover does not.
@pytest.mark.parametrize(
    "column,value",
    [
        ("alias_id", "al_0000000000000002"),
        ("topic_id", "topic_somebody_elses"),
        ("capabilities", json.dumps(list(CAPABILITIES))),
        ("inherit", 1),
        ("expires_at", (START + timedelta(days=29)).isoformat()),
        ("proposed_by", "someone"),
        ("proposed_at", (START - timedelta(hours=1)).isoformat()),
        ("nonce", "f" * 32),
        ("alias_name_seen", "lane-z"),
        ("alias_code_seen", "0000"),
        ("topic_path_seen", "elsewhere"),
    ],
)
def test_editing_any_digested_field_after_authorization_makes_the_grant_invalid(build_world, column, value):
    w = build_world()
    made = w.activate()["id"]
    assert w.state(made) == "active"
    w.edit(made, **{column: value})
    assert w.state(made) == "invalid", f"editing {column} left the grant {w.state(made)}"
    assert w.covering(ROOT) is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("proposal_expires_at", (START + timedelta(days=9)).isoformat()),
        ("digest", "0" * 64),
    ],
)
def test_editing_a_field_the_digest_does_not_cover_changes_nothing(build_world, column, value):
    """The check recomputes from the covered fields, so the stored `digest` copy is not what it trusts."""
    w = build_world()
    made = w.activate()["id"]
    w.edit(made, **{column: value})
    assert w.state(made) == "active"
    assert w.covering(ROOT) == made


def test_an_edited_authorized_digest_makes_the_grant_invalid(build_world):
    w = build_world()
    made = w.activate()["id"]
    w.edit(made, authorized_digest="0" * 64)
    assert w.state(made) == "invalid"
    assert w.covering(ROOT) is None


# 3. States, in order -------------------------------------------------------------------------------------


def test_a_proposal_is_pending_until_its_day_is_up_and_then_lapsed(build_world):
    w = build_world()
    made = w.propose()["id"]
    assert w.state(made) == "pending"
    w.clock.now = START + timedelta(hours=24) - MICRO
    assert w.state(made) == "pending"
    # Boundary: at proposal_expires_at == now the proposal has lapsed.
    w.clock.now = START + timedelta(hours=24)
    assert w.state(made) == "lapsed"
    w.clock.now = START + timedelta(days=30)
    assert w.state(made) == "lapsed"


# Mutant 3: expires_at > now flipped to >= or <.
def test_an_authorized_grant_is_active_until_the_exact_moment_it_expires(build_world):
    w = build_world()
    made = w.activate(days=7)["id"]
    expiry = START + timedelta(days=7)
    w.clock.now = expiry - MICRO
    assert w.state(made) == "active"
    w.clock.now = expiry
    assert w.state(made) == "expired", "at expires_at == now the grant is expired"
    w.clock.now = expiry + MICRO
    assert w.state(made) == "expired"


def test_authorizing_late_in_the_day_does_not_move_the_grants_expiry(build_world):
    w = build_world()
    proposal = w.propose(days=2)
    w.clock.advance(hours=23)
    w.authorize(proposal)
    assert w.row(proposal["id"])["expires_at"] == (START + timedelta(days=2)).isoformat()


# Mutant 3: dropping the `revoked_at` term.
@pytest.mark.parametrize("name", ["pending", "lapsed", "active", "expired", "invalid"])
def test_revoked_is_checked_first_whatever_the_grant_would_otherwise_be(build_world, name):
    w = build_world()
    made = bring_to(w, name)
    assert w.state(made) == name
    w.revoke(made)
    assert w.state(made) == "revoked"
    assert w.covering(ROOT) is None


def test_a_state_is_never_stored(build_world):
    w = build_world()
    columns = {row["name"] for row in w.db.execute("PRAGMA table_info(topic_grants)")}
    assert columns == GRANT_COLUMNS, f"unexpected columns: {sorted(columns ^ GRANT_COLUMNS)}"


# 4. authorize --------------------------------------------------------------------------------------------


def test_authorize_mints_a_key_and_stores_only_its_hash(build_world):
    w = build_world()
    proposal = w.propose()
    w.clock.advance(hours=1)
    granted = w.authorize(proposal)
    key = granted["grant_key"]
    assert re.fullmatch(r"tgk_[A-Za-z0-9_-]{43}", key), "tgk_ plus 256 bits, URL-safe"
    row = w.row(proposal["id"])
    # Mutant 7: the stored proof must be sha256(key), never the key.
    assert row["proof_digest"] == hashlib.sha256(key.encode()).hexdigest()
    assert row["proof_digest"] != key
    assert (row["authorized_by"], row["authorized_at"]) == ("owner", w.clock.now.isoformat())
    assert row["authorized_digest"] == proposal["digest"] == row["digest"]
    assert granted["id"] == proposal["id"] and granted["state"] == "active"
    assert w.state(proposal["id"]) == "active"


def test_each_authorization_mints_its_own_key(build_world):
    w = build_world()
    first, second = w.activate(), w.activate()
    assert first["grant_key"] != second["grant_key"]
    assert w.row(first["id"])["proof_digest"] != w.row(second["id"])["proof_digest"]


# Mutant 7: the key appears nowhere but the return value of authorize.
def test_the_key_is_stored_and_logged_nowhere(build_world, caplog):
    w = build_world()
    caplog.set_level(logging.DEBUG)
    granted = w.activate()
    key = granted["grant_key"]
    assert key
    assert json.dumps(granted, default=str).count(key) == 1, "the key appears once in the return value"
    assert not [cell for cell in every_cell(w.db) if key in cell], "the key is in the database"
    assert key not in caplog.text
    w.revoke(granted["id"])
    listed = json.dumps([dict(one) for one in w.tg.list_grants(w.db, include_all=True)], default=str)
    assert key not in listed, "a listing must never carry the key"
    assert "proof_digest" not in listed and w.row(granted["id"])["proof_digest"] not in listed


def test_authorize_writes_an_authorize_event_by_the_owner(build_world):
    w = build_world()
    proposal = w.propose()
    w.clock.advance(minutes=5)
    w.authorize(proposal)
    assert [(e["seq"], e["event"], e["actor"], e["at"]) for e in w.events()] == [
        (1, "propose", "owner", START.isoformat()),
        (2, "authorize", "owner", w.clock.now.isoformat()),
    ]
    assert {e["grant_id"] for e in w.events()} == {proposal["id"]}


# Mutant 6: a wrong digest must be refused. Each of these is a different way to be wrong.
def test_authorize_refuses_every_digest_but_the_exact_one(build_world):
    w = build_world()
    proposal = w.propose()
    other = w.propose()
    before = dict(w.row(proposal["id"]))
    wrong = {
        "another hash": "0" * 64,
        "the same hash in capitals": proposal["digest"].upper(),
        "a prefix of the right one": proposal["digest"][:32],
        "the right one with a space": proposal["digest"] + " ",
        "another grant's digest": other["digest"],
        "nothing": "",
    }
    for label, given in wrong.items():
        refusal(w.tg.authorize, w.db, proposal["id"], given)
        assert dict(w.row(proposal["id"])) == before, f"{label} changed the row"
        assert w.state(proposal["id"]) == "pending", label
    assert [e["event"] for e in w.events()] == ["propose", "propose"], "a refusal writes no event"
    assert w.authorize(proposal)["grant_key"], "the right digest still works after the wrong ones"


def test_authorize_compares_against_the_recomputed_digest_not_the_stored_copy(build_world):
    """The proposal was edited after it was shown: the stored digest still matches what the human saw, the
    fields no longer do, and the authorization must be refused."""
    w = build_world()
    proposal = w.propose(capabilities=("reference.edit",))
    w.edit(proposal["id"], capabilities=json.dumps(list(CAPABILITIES)))
    refusal(w.tg.authorize, w.db, proposal["id"], proposal["digest"])
    assert w.state(proposal["id"]) == "pending" and w.row(proposal["id"])["proof_digest"] is None


# Mutant 6: authorizing after the proposal lapsed, with the boundary on each side.
def test_authorize_is_refused_from_the_moment_the_proposal_lapses(build_world):
    w = build_world()
    proposal = w.propose()
    w.clock.now = START + timedelta(hours=24)
    error = refusal(w.authorize, proposal)
    assert "lapsed" in words(error) and "propose again" in words(error)
    assert w.row(proposal["id"])["proof_digest"] is None and w.state(proposal["id"]) == "lapsed"
    assert [e["event"] for e in w.events()] == ["propose"]


def test_authorize_still_works_in_the_last_instant_of_the_proposal(build_world):
    w = build_world()
    proposal = w.propose()
    w.clock.now = START + timedelta(hours=24) - MICRO
    assert w.authorize(proposal)["grant_key"]


def test_authorize_refuses_a_grant_already_authorized_and_keeps_its_key(build_world):
    w = build_world()
    proposal = w.propose()
    first = w.authorize(proposal)
    before = dict(w.row(proposal["id"]))
    error = refusal(w.authorize, proposal)
    assert "already authorized" in words(error)
    assert dict(w.row(proposal["id"])) == before
    assert [e["event"] for e in w.events()] == ["propose", "authorize"]
    assert first["grant_key"] not in str(error)


def test_authorize_refuses_a_revoked_proposal_and_says_so(build_world):
    w = build_world()
    proposal = w.propose()
    w.revoke(proposal["id"], "changed my mind")
    error = refusal(w.authorize, proposal)
    assert "revoked" in words(error)
    assert w.row(proposal["id"])["proof_digest"] is None and w.state(proposal["id"]) == "revoked"


def test_authorize_refuses_an_unknown_grant_id(build_world):
    w = build_world()
    refusal(w.tg.authorize, w.db, "tgr_0000000000000000", "0" * 64)
    assert w.events() == []


def test_authorize_succeeds_when_the_alias_and_topic_are_as_they_were_shown(build_world):
    """The control for the three refusals below."""
    w = build_world()
    assert w.authorize(w.propose())["grant_key"]


def test_authorize_refuses_when_the_alias_was_renamed_since_the_proposal(build_world):
    w = build_world()
    proposal = w.propose()
    w.db.execute("UPDATE aliases SET name = 'lane-renamed', skeleton = 'lane-renamed' WHERE name = 'lane-a'")
    error = refusal(w.authorize, proposal)
    assert "propose again" in words(error)
    assert w.state(proposal["id"]) == "pending" and w.row(proposal["id"])["proof_digest"] is None


def test_authorize_refuses_when_the_topic_was_renamed_since_the_proposal(build_world):
    w = build_world()
    proposal = w.propose()
    topics.rename(w.db, w.config, actor="orchestrator", path=ROOT, new_path="work-search")
    error = refusal(w.authorize, proposal)
    assert "propose again" in words(error)
    assert w.state(proposal["id"]) == "pending" and w.row(proposal["id"])["proof_digest"] is None


@pytest.mark.parametrize("table,column,value", [("aliases", "name", "lane-a"), ("topics", "path", ROOT)])
def test_authorize_refuses_when_the_alias_or_topic_is_gone(build_world, table, column, value):
    w = build_world()
    proposal = w.propose()
    w.db.execute(f"DELETE FROM {table} WHERE {column} = ?", (value,))
    error = refusal(w.authorize, proposal)
    assert "propose again" in words(error)
    assert w.row(proposal["id"])["proof_digest"] is None


# 5. revoke -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["", "   ", "\n\t ", "x" * 501, None])
def test_revoke_refuses_a_missing_blank_or_overlong_reason(build_world, reason):
    w = build_world()
    made = w.activate()["id"]
    before = dict(w.row(made))
    assert refusal(w.tg.revoke, w.db, made, reason).code == "invalid_request"
    assert dict(w.row(made)) == before and w.state(made) == "active"
    assert [e["event"] for e in w.events()] == ["propose", "authorize"]


def test_revoke_accepts_a_reason_of_exactly_500_characters(build_world):
    w = build_world()
    made = w.propose()["id"]
    w.revoke(made, "x" * 500)
    assert w.row(made)["revoke_reason"] == "x" * 500


@pytest.mark.parametrize("name", ["pending", "lapsed", "active", "expired", "invalid"])
def test_revoke_fills_only_its_own_fields_and_deletes_nothing(build_world, name):
    # Mutant 9: revoke must not delete the row or touch the proposal's fields.
    w = build_world()
    made = bring_to(w, name)
    before = dict(w.row(made))
    count = len(w.rows())
    events = len(w.events())
    w.clock.advance(minutes=1)
    w.revoke(made, "the lane is done")
    after = dict(w.row(made))
    assert len(w.rows()) == count, "revoke deleted a row"
    assert {k: after[k] for k in after if not k.startswith("revoke")} == {
        k: before[k] for k in before if not k.startswith("revoke")
    }, "revoke changed a field that is not its own"
    assert (after["revoked_at"], after["revoked_by"], after["revoke_reason"]) == (
        w.clock.now.isoformat(),
        "owner",
        "the lane is done",
    )
    assert all(before[column] == after[column] for column in PROPOSAL_COLUMNS)
    assert [e["event"] for e in w.events()][events:] == ["revoke"]
    assert w.events()[-1]["actor"] == "owner"
    assert w.events()[-1]["grant_id"] == made


def test_revoking_a_pending_proposal_cancels_it(build_world):
    w = build_world()
    proposal = w.propose()
    w.revoke(proposal["id"], "wrong topic")
    assert w.state(proposal["id"]) == "revoked"
    refusal(w.authorize, proposal)


def test_revoke_refuses_a_grant_already_revoked_and_keeps_the_first_reason(build_world):
    w = build_world()
    made = w.activate()["id"]
    w.revoke(made, "first reason")
    w.clock.advance(hours=1)
    refusal(w.tg.revoke, w.db, made, "second reason")
    row = w.row(made)
    assert (row["revoke_reason"], row["revoked_at"]) == ("first reason", START.isoformat())
    assert [e["event"] for e in w.events()] == ["propose", "authorize", "revoke"]


def test_revoke_refuses_an_unknown_grant_id(build_world):
    w = build_world()
    refusal(w.tg.revoke, w.db, "tgr_0000000000000000", "reason")
    assert w.events() == []


# 6. list_grants ------------------------------------------------------------------------------------------


def test_the_default_listing_holds_only_pending_and_active_grants(build_world):
    w = build_world()
    ids = {
        "expired": w.activate(days=1)["id"],
        "lapsed": w.propose()["id"],
        "invalid": w.activate()["id"],
        "revoked": w.activate()["id"],
    }
    w.edit(ids["invalid"], capabilities=json.dumps(list(CAPABILITIES)))
    w.revoke(ids["revoked"])
    w.clock.now = START + timedelta(days=1)  # the first one expires and the unauthorized one lapses just now
    ids["pending"] = w.propose()["id"]
    ids["active"] = w.activate()["id"]
    listed = {one["id"]: one["state"] for one in w.tg.list_grants(w.db)}
    assert listed == {ids["pending"]: "pending", ids["active"]: "active"}
    everything = {one["id"]: one["state"] for one in w.tg.list_grants(w.db, include_all=True)}
    assert everything == {ids[name]: name for name in ids}


def test_a_listing_reads_the_clock_each_time(build_world):
    w = build_world()
    made = w.propose()["id"]
    assert [one["id"] for one in w.tg.list_grants(w.db)] == [made]
    w.clock.now = START + timedelta(hours=24)
    assert w.tg.list_grants(w.db) == []
    assert [one["state"] for one in w.tg.list_grants(w.db, include_all=True)] == ["lapsed"]


def test_a_listed_grant_shows_what_the_human_needs_and_never_the_proof_or_nonce(build_world):
    w = build_world()
    made = w.activate(capabilities=("thread.organise",), inherit=True)
    [shown] = w.tg.list_grants(w.db)
    assert set(shown.keys()) >= {
        "id",
        "alias_id",
        "alias_name",
        "alias_code",
        "topic_id",
        "topic_path",
        "capabilities",
        "inherit",
        "expires_at",
        "proposal_expires_at",
        "digest",
        "state",
    }
    assert shown["id"] == made["id"] and shown["capabilities"] == ["thread.organise"]
    assert (shown["alias_name"], shown["topic_path"]) == ("lane-a", ROOT)
    assert "proof_digest" not in shown.keys() and "nonce" not in shown.keys()
    assert "grant_key" not in shown.keys()


def test_listing_orders_by_proposal_time_then_id(build_world):
    w = build_world()
    w.clock.now = START + timedelta(hours=2)
    late = w.propose()["id"]
    w.clock.now = START
    early = w.propose()["id"]
    w.clock.now = START + timedelta(hours=1)
    middle = w.propose()["id"]
    assert [one["id"] for one in w.tg.list_grants(w.db)] == [early, middle, late]
    w.clock.now = START + timedelta(hours=5)
    tied = [w.reid(w.propose(), f"tgr_000000000000000{number}")["id"] for number in (4, 3, 2, 1)]
    assert [one["id"] for one in w.tg.list_grants(w.db, include_all=True)][3:] == sorted(tied)


def test_listing_writes_nothing(build_world):
    w = build_world()
    w.activate()
    driver = w.db._driver_connection()
    changes = driver.total_changes
    w.tg.list_grants(w.db)
    w.tg.list_grants(w.db, include_all=True)
    assert driver.total_changes == changes and len(w.events()) == 2


# 7. The event log and atomicity ----------------------------------------------------------------------------


def test_the_event_sequence_follows_the_operations_and_refusals_add_nothing(build_world):
    w = build_world()
    first = w.propose()
    second = w.propose(topic=CHILD)
    refusal(w.tg.authorize, w.db, first["id"], "0" * 64)
    w.clock.advance(minutes=1)
    w.authorize(first)
    refusal(w.authorize, first)
    w.revoke(second["id"], "not needed")
    refusal(w.tg.revoke, w.db, second["id"], "again")
    w.clock.advance(minutes=1)
    w.revoke(first["id"], "done")
    events = w.events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [(e["grant_id"], e["event"], e["actor"]) for e in events] == [
        (first["id"], "propose", "owner"),
        (second["id"], "propose", "owner"),
        (first["id"], "authorize", "owner"),
        (second["id"], "revoke", "owner"),
        (first["id"], "revoke", "owner"),
    ]
    assert [e["at"] for e in events] == [START.isoformat()] * 2 + [(START + timedelta(minutes=1)).isoformat()] * 2 + [
        (START + timedelta(minutes=2)).isoformat()
    ]


def test_the_event_columns_are_the_ones_the_spec_names(build_world):
    w = build_world()
    columns = {row["name"]: row for row in w.db.execute("PRAGMA table_info(topic_grant_events)")}
    assert set(columns) == {"seq", "grant_id", "event", "actor", "detail", "at"}
    assert columns["seq"]["pk"] == 1
    assert [name for name, row in columns.items() if row["notnull"]] == ["grant_id", "event", "actor", "at"]


@pytest.mark.parametrize("operation", ["propose", "authorize", "revoke"])
def test_an_operation_is_one_transaction_so_a_failed_event_write_undoes_it(build_world, operation):
    w = build_world()
    proposal = w.propose()
    if operation == "revoke":
        w.authorize(proposal)
    before = [dict(row) for row in w.rows()]
    events = len(w.events())
    w.db.execute(
        "CREATE TRIGGER no_more_events BEFORE INSERT ON topic_grant_events"
        " BEGIN SELECT RAISE(ABORT, 'no more events'); END"
    )
    with pytest.raises(Exception, match="no more events"):
        {
            "propose": lambda: w.propose(),
            "authorize": lambda: w.authorize(proposal),
            "revoke": lambda: w.revoke(proposal["id"]),
        }[operation]()
    assert not w.db.in_transaction, "the failed operation left its transaction open"
    w.db.execute("DROP TRIGGER no_more_events")
    assert [dict(row) for row in w.rows()] == before
    assert len(w.events()) == events
