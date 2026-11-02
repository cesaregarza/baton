"""Which topic a grant covers: `covering_grant(conn, alias_id, topic_path, capability)`.

A grant covers a topic when it is active, belongs to the alias, lists the capability, and either is on that
very topic (by id) or is an `inherit` grant on the topic's parent (by the parent's id, read at its current
path). Everything matches on ids: a rename moves a grant with its topic, and a topic created later at a
freed path is a stranger. Paths are compared segment by segment, never as text prefixes.

Topics in these tests: `job-hunt` and `job-hunt/x`, and beside them `job-hunting` and `job-hunting/x`, whose
names merely begin with the first one's. Mutant numbers in comments refer to the spec's list.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import topic_grant_support as support
from topic_backing import back_topics
from topic_grant_support import (
    CAPABILITIES,
    CHILD,
    LOOKALIKE,
    LOOKALIKE_CHILD,
    ROOT,
    START,
    World,
    bring_to,
)

from baton import topics

build_world = support.build_world  # the fixture, under the name the tests ask for

MICRO = timedelta(microseconds=1)
ALL = tuple(CAPABILITIES)


def covered(w: World, grant_id: str, *topics_: str, capability: str = "reference.edit") -> list[str]:
    """Which of these topics the grant covers, for the alias it belongs to."""
    return [path for path in topics_ if w.covering(path, capability) == grant_id]


# 1. Segments, not prefixes -------------------------------------------------------------------------------

# Mutant 1: segment equality replaced by startswith or LIKE.
@pytest.mark.parametrize("inherit", [False, True])
def test_a_grant_on_job_hunt_never_covers_job_hunting_or_below_it(build_world, inherit):
    w = build_world()
    made = w.activate(topic=ROOT, inherit=inherit)["id"]
    assert covered(w, made, LOOKALIKE, LOOKALIKE_CHILD) == []
    assert w.covering(LOOKALIKE) is None and w.covering(LOOKALIKE_CHILD) is None


def test_a_grant_on_job_hunting_never_covers_job_hunt_or_below_it(build_world):
    """The same mistake the other way round: the longer name is not a parent of the shorter one's subtopics."""
    w = build_world()
    made = w.activate(topic=LOOKALIKE, inherit=True)["id"]
    assert covered(w, made, ROOT, CHILD) == []
    assert covered(w, made, LOOKALIKE, LOOKALIKE_CHILD) == [LOOKALIKE, LOOKALIKE_CHILD]


# 2. What a grant reaches ---------------------------------------------------------------------------------

def test_a_grant_without_inherit_covers_its_own_topic_only(build_world):
    w = build_world()
    made = w.activate(topic=ROOT, inherit=False)["id"]
    assert covered(w, made, ROOT, CHILD, LOOKALIKE, "reviews") == [ROOT]


# Mutant 5: ignoring `inherit`.
def test_a_grant_with_inherit_covers_its_topic_and_the_subtopics_under_it(build_world):
    w = build_world()
    back_topics(w.db, "job-hunt/y")
    made = w.activate(topic=ROOT, inherit=True)["id"]
    assert covered(w, made, ROOT, CHILD, "job-hunt/y", LOOKALIKE, LOOKALIKE_CHILD, "reviews") == \
        [ROOT, CHILD, "job-hunt/y"]


def test_a_grant_on_a_subtopic_covers_neither_its_parent_nor_its_siblings(build_world):
    w = build_world()
    back_topics(w.db, "job-hunt/y")
    made = w.activate(topic=CHILD, inherit=True)["id"]
    assert covered(w, made, ROOT, CHILD, "job-hunt/y") == [CHILD]


@pytest.mark.parametrize("path", ["nowhere", "job-hunt/z", "job-hunt/x/deeper", "job-hun", "JOB-HUNT", ROOT + "/", ""])
def test_a_path_that_is_not_a_topic_is_covered_by_nothing(build_world, path):
    w = build_world()
    w.activate(topic=ROOT, inherit=True)
    assert w.covering(path) is None


def test_an_unknown_alias_id_is_covered_by_nothing(build_world):
    w = build_world()
    w.activate(topic=ROOT, inherit=True)
    assert w.covering(ROOT, alias_id="al_ffffffffffffffff") is None


# Mutant 5: replacing the capability check with True.
def test_a_grant_covers_only_the_capabilities_it_lists(build_world):
    w = build_world()
    narrow = w.activate(topic=ROOT, capabilities=("reference.edit",))["id"]
    assert w.covering(ROOT, "reference.edit") == narrow
    assert w.covering(ROOT, "thread.organise") is None
    assert w.covering(ROOT, "topic.create_sub") is None
    assert w.covering(ROOT, "reference.delete") is None, "a name outside the three is never covered"
    assert w.covering(ROOT, "") is None


def test_a_grant_listing_all_three_covers_each(build_world):
    w = build_world()
    wide = w.activate(topic=ROOT, capabilities=ALL, inherit=True)["id"]
    assert [w.covering(path, capability) for capability in ALL for path in (ROOT, CHILD)] == [wide] * 6


# 3. Ids, not names ---------------------------------------------------------------------------------------

# Mutant 2: matching by topic_path_seen or alias name instead of topic_id / alias_id.
def test_a_grant_follows_its_topic_through_a_rename(build_world):
    w = build_world()
    made = w.activate(topic=ROOT, inherit=True)["id"]
    topics.rename(w.db, w.config, actor="orchestrator", path=ROOT, new_path="work-search",
                  acknowledge_topic_grants=[made])
    assert covered(w, made, "work-search", "work-search/x") == ["work-search", "work-search/x"]
    assert covered(w, made, ROOT, CHILD) == [], "the old paths are no topics at all now"
    assert w.state(made) == "active", "a rename does not touch the grant's own digest"


# Mutant 2: a topic created at a freed path is a stranger to the grant.
def test_a_new_topic_at_a_path_a_rename_freed_is_not_covered(build_world):
    w = build_world()
    made = w.activate(topic=ROOT, inherit=True)["id"]
    topics.rename(w.db, w.config, actor="orchestrator", path=ROOT, new_path="work-search",
                  acknowledge_topic_grants=[made])
    back_topics(w.db, ROOT, "job-hunt/fresh")
    assert w.topic_id(ROOT) != w.row(made)["topic_id"]
    assert covered(w, made, ROOT, "job-hunt/fresh") == []


# Mutant 2: the root is read by id at its current path, not by the path seen at proposal time.
def test_a_subtopic_is_judged_by_its_parents_current_path_after_a_rename(build_world):
    w = build_world()
    made = w.activate(topic=ROOT, inherit=True)["id"]
    topics.rename(w.db, w.config, actor="orchestrator", path=ROOT, new_path=LOOKALIKE + "-two",
                  acknowledge_topic_grants=[made])
    assert covered(w, made, LOOKALIKE + "-two/x") == [LOOKALIKE + "-two/x"]
    assert w.covering(LOOKALIKE_CHILD) is None


# Mutant 2: matching on the alias name or not on the alias at all.
def test_another_alias_is_not_covered(build_world):
    w = build_world()
    made = w.activate(alias="lane-a", topic=ROOT, inherit=True)["id"]
    assert w.covering(ROOT, alias_id=w.alias_b["alias_id"]) is None
    assert w.covering(CHILD, alias_id=w.alias_b["alias_id"]) is None
    other = w.activate(alias="lane-b", topic=ROOT, inherit=True)["id"]
    assert w.covering(ROOT) == made and w.covering(ROOT, alias_id=w.alias_b["alias_id"]) == other


# Mutant 2: the alias is matched by its id, so a changed name does not matter.
def test_a_grant_ignores_the_aliases_name_after_the_fact(build_world):
    w = build_world()
    made = w.activate(topic=ROOT)["id"]
    w.db.execute("UPDATE aliases SET name = 'lane-renamed', skeleton = 'lane-renamed' WHERE name = 'lane-a'")
    assert w.covering(ROOT) == made, "the grant belongs to the alias id"
    assert w.state(made) == "active"


# 4. Only active grants cover -----------------------------------------------------------------------------

# Mutant 4: allowing `pending`.
@pytest.mark.parametrize("name", ["pending", "lapsed", "expired", "invalid"])
def test_a_grant_that_is_not_active_covers_nothing(build_world, name):
    w = build_world()
    made = bring_to(w, name)
    assert w.state(made) == name
    assert w.covering(ROOT) is None


# Mutant 3: dropping the revoked term.
def test_a_revoked_grant_covers_nothing_though_it_is_otherwise_untouched(build_world):
    w = build_world()
    made = w.activate()["id"]
    assert w.covering(ROOT) == made
    w.revoke(made)
    assert w.row(made)["authorized_digest"] == w.row(made)["digest"], "only the revocation fields changed"
    assert w.row(made)["expires_at"] > w.clock.now.isoformat()
    assert w.covering(ROOT) is None


# Mutant 3: expires_at > now flipped to >= or <, at the exact boundary.
def test_a_grant_covers_up_to_and_not_including_the_instant_it_expires(build_world):
    w = build_world()
    made = w.activate(days=2)["id"]
    expiry = START + timedelta(days=2)
    w.clock.now = START + timedelta(days=1)
    assert w.covering(ROOT) == made
    w.clock.now = expiry - MICRO
    assert w.covering(ROOT) == made
    w.clock.now = expiry
    assert w.covering(ROOT) is None
    w.clock.now = expiry + MICRO
    assert w.covering(ROOT) is None


# Mutant 4: dropping the recomputed-digest check. Each edit would widen the grant if it were believed.
@pytest.mark.parametrize("column,value,topic,capability", [
    ("capabilities", json.dumps(list(CAPABILITIES)), ROOT, "thread.organise"),
    ("inherit", 1, CHILD, "reference.edit"),
    ("expires_at", (START + timedelta(days=300)).isoformat(), ROOT, "reference.edit"),
    ("topic_id", "REPLACED", ROOT, "reference.edit"),
])
def test_a_grant_edited_after_authorization_covers_nothing(build_world, column, value, topic, capability):
    w = build_world()
    made = w.activate(topic=ROOT, capabilities=("reference.edit",), inherit=False, days=7)["id"]
    if column == "topic_id":
        value = w.topic_id(CHILD)
    w.edit(made, **{column: value})
    assert w.covering(topic, capability) is None
    assert w.covering(ROOT) is None
    assert w.state(made) == "invalid"


def test_a_grant_edited_to_a_later_expiry_does_not_outlive_its_authorization(build_world):
    w = build_world()
    made = w.activate(days=1)["id"]
    w.edit(made, expires_at=(START + timedelta(days=300)).isoformat())
    w.clock.now = START + timedelta(days=2)
    assert w.covering(ROOT) is None


# 5. Which grant answers ----------------------------------------------------------------------------------

def test_the_earliest_proposal_among_the_covering_grants_answers(build_world):
    w = build_world()
    # Ids chosen so that the earliest proposal has the largest id: only the proposal time can put it first.
    first = w.reid(w.propose(topic=ROOT, inherit=True), "tgr_00000000000000ff")
    w.clock.advance(hours=1)
    second = w.reid(w.propose(topic=CHILD), "tgr_0000000000000001")
    w.clock.advance(hours=1)
    w.authorize(second)  # authorized first, proposed second
    w.clock.advance(hours=1)
    w.authorize(first)
    assert w.covering(CHILD) == first["id"], "ordered by when it was proposed, not when it was authorized"


def test_a_tie_on_the_proposal_instant_goes_to_the_smaller_id(build_world):
    w = build_world()
    # Proposed in the order 3, 2, 1 at one instant: only the id can put the last one first.
    proposals = [w.reid(w.propose(topic=ROOT), f"tgr_000000000000000{number}") for number in (3, 2, 1)]
    for proposal in proposals:
        w.authorize(proposal)
    assert w.covering(ROOT) == "tgr_0000000000000001"


def test_a_grant_that_cannot_cover_never_masks_a_later_one_that_can(build_world):
    w = build_world()
    revoked = w.activate()["id"]
    w.clock.advance(hours=1)
    pending = w.propose()["id"]
    w.clock.advance(hours=1)
    other_capability = w.activate(capabilities=("thread.organise",))["id"]
    w.clock.advance(hours=1)
    good = w.activate()["id"]
    w.revoke(revoked)
    assert w.state(pending) == "pending"
    assert w.covering(ROOT, "reference.edit") == good
    assert w.covering(ROOT, "thread.organise") == other_capability


# 6. Read-only, on the caller's connection --------------------------------------------------------------

def test_covering_grant_writes_nothing(build_world):
    w = build_world()
    w.activate(inherit=True)
    changes, events = w.db.total_changes, len(w.events())
    for path in (ROOT, CHILD, "nowhere", LOOKALIKE):
        w.covering(path)
    assert w.db.total_changes == changes and len(w.events()) == events


def test_covering_grant_runs_inside_the_callers_own_write_transaction(build_world):
    """The guard that calls it holds the write lock already: it must neither open nor end a transaction."""
    w = build_world()
    made = w.activate()["id"]
    w.db.execute("BEGIN IMMEDIATE")
    assert w.covering(ROOT) == made
    assert w.db.in_transaction, "covering_grant ended the caller's transaction"
    w.db.execute("INSERT INTO topic_grant_events (grant_id, event, actor, detail, at)"
                 " VALUES ('tgr_probe', 'probe', 'owner', NULL, 'now')")
    assert w.covering(ROOT) == made
    w.db.execute("ROLLBACK")
    assert [e["grant_id"] for e in w.events()].count("tgr_probe") == 0


def test_covering_grant_returns_the_grant_row_the_guard_needs(build_world):
    w = build_world()
    made = w.activate(capabilities=("reference.edit",), inherit=True)
    found = w.tg.covering_grant(w.db, w.alias_a["alias_id"], CHILD, "reference.edit")
    assert found["id"] == made["id"]
    assert found["topic_id"] == w.topic_id(ROOT), "the grant's own topic, not the one asked about"
    assert found["alias_id"] == w.alias_a["alias_id"]
