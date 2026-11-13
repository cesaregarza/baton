"""The graph worker's call budget: a call is reserved before it is launched, within attempt, cycle and daily caps.

A call the provider refused without doing any work is refunded and retried on a backoff instead.
"""

import pytest
from test_graph_worker import DAY, Clock, ScriptedTransport, published, run_until_idle, worker_config
from test_index_writer import converse, say

from baton.database.store import connect
from baton.graph import ledger
from baton.graph.contract import GraphError
from baton.graph.source import load_json
from baton.graph.worker import Worker, reset, status

REFUSALS = ("authentication", "rate_limited", "provider_unavailable", "provider_refused", "unverified_client")


def test_calls_are_reserved_before_launch_and_exhaustion_blocks_durably(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid])
    clock = Clock()
    transport = ScriptedTransport(["timeout", "garbage"])
    worker = Worker(config, transport=transport, clock=clock)
    first = worker.cycle()
    record = first["records"][cid]
    assert (record["phase"], record["attempts"], record["error_category"]) == ("extracting", 1, "timeout")
    assert record["not_before"] == clock.now + ledger.RETRY_DELAY
    assert worker.cycle()["stepped"] == []  # not before the retry delay
    clock.now += ledger.RETRY_DELAY
    second = worker.cycle()
    record = second["records"][cid]
    # The second call was the last one allowed for this base W; its rejection is terminal at once.
    assert (record["phase"], record["attempts"], record["error_category"]) == ("blocked", 2, "extraction_failed")
    clock.now += ledger.RETRY_DELAY
    assert worker.cycle()["stepped"] == [] and len(transport.calls) == 2
    # A timeout left no candidate, so the second request carried no correction; the saved rejection would.
    assert "correction" not in transport.calls[1]["request"]
    assert load_json(next((worker.paths(cid).root / "proposals").glob("*.json"))) == {"unexpected": True}
    with connect(config.db_path) as db:
        say(db, config, cid, "note 3")  # new posts do not clear a terminal block
    clock.now += DAY
    restarted = Worker(config, transport=ScriptedTransport(), clock=clock)
    after = restarted.cycle()
    assert after["records"][cid]["phase"] == "blocked" and after["stepped"] == []
    assert restarted.transport.calls == []
    with connect(config.db_path) as db:
        assert published(db, cid) == []
    # A reset works while a service owns the directory: it is left as a request the worker applies itself.
    requests = config.graph_worker.state_dir / "reset-requests"
    with ledger.Ledger.open(config.graph_worker.state_dir):
        assert reset(config, cid)["reset"] == "requested"
        with pytest.raises(GraphError, match="unknown worker conversation"):
            reset(config, "conv_nowhere")
    assert status(config)["conversations"][cid]["phase"] == "blocked"  # nothing applied outside the worker
    (requests / "conv_nowhere").write_text("")  # a request for no record is ignored and removed
    recovered = run_until_idle(restarted, clock)
    assert recovered[0]["records"][cid]["phase"] != "blocked" and "conv_nowhere" not in recovered[0]["records"]
    assert recovered[-1]["records"][cid]["published_W"] == 3
    assert not list(requests.iterdir())


def test_provider_refusals_are_refunded_never_block_and_back_off_to_an_hourly_ceiling(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"])
    with connect(config.db_path) as db:
        cid, _ = converse(db, config, 2)
    config = worker_config(tmp_path, conversations=[cid], daily_call_cap=2)
    clock = Clock()
    day = ledger.utc_day(clock.now)
    transport = ScriptedTransport(["rate_limited"] * 9 + ["ok"] + list(REFUSALS) + ["ok"])
    worker = Worker(config, transport=transport, clock=clock)

    def refused_until_answered(expected):
        waits = []
        for category in expected:
            record = worker.cycle()["records"][cid]
            assert (record["phase"], record["attempts"], record["error_category"]) == ("extracting", 0, category)
            waits.append(record["not_before"] - clock.now)
            clock.now = record["not_before"]
        return waits

    # Refused calls are refunded and never block; the wait doubles from one minute to an hourly ceiling.
    assert refused_until_answered(["rate_limited"] * 9) == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]
    assert (status(config)["calls"], status(config)["refused"]) == ({day: 9}, {day: 9})
    # The next answered call proceeds normally: nine refusals did not use up a daily cap of two.
    answered = worker.cycle()["records"][cid]
    assert (answered["phase"], answered["accepted_W"], answered["refusals"]) == ("compacting", 2, 0)
    with connect(config.db_path) as db:
        say(db, config, cid, "note 3")
        say(db, config, cid, "note 4")
    while worker.cycle()["records"][cid]["phase"] != "pending":  # compaction and publication make no call
        pass
    assert refused_until_answered(REFUSALS) == [60, 120, 240, 480, 960]
    results = run_until_idle(worker, clock)
    assert (results[-1]["records"][cid]["published_W"], len(transport.calls)) == (4, 16)
    assert (status(config)["calls"], status(config)["refused"]) == ({day: 16}, {day: 14})
    assert [entry["outcome"] for entry in status(config)["recent_calls"]][-6:] == [*REFUSALS, "returned"]
    # Durable causes are unchanged: an isolation failure blocks after one call. A charged failure clears the
    # consecutive-refusal count, and two charged failures block.
    worker = Worker(worker_config(tmp_path, conversations=[cid], daily_call_cap=0), transport=transport, clock=clock)
    with connect(config.db_path) as db:
        say(db, config, cid, "note 5")
    transport.script = ["isolation"]
    record = run_until_idle(worker, clock)[-1]["records"][cid]
    assert (record["phase"], record["attempts"], record["error_category"]) == ("blocked", 1, "isolation")
    reset(config, cid)
    transport.script = ["provider_refused", "request_rejected", "provider_error"]
    records = [result["records"][cid] for result in run_until_idle(worker, clock)]
    assert [(r["attempts"], r["refusals"], r["error_category"]) for r in records] == [
        (0, 1, "provider_refused"),
        (1, 0, "request_rejected"),
        (2, 0, "extraction_failed"),
    ]
    assert records[-1]["phase"] == "blocked" and len(transport.calls) == 20


def test_daily_cap_defers_without_clearing_attempts_and_cycle_cap_spreads_calls(tmp_path) -> None:
    config = worker_config(tmp_path, conversations=["placeholder"], daily_call_cap=1)
    with connect(config.db_path) as db:
        first_cid, _ = converse(db, config, 2)
        second_cid, _ = converse(db, config, 2)
    pair = [first_cid, second_cid]
    config = worker_config(tmp_path, conversations=pair, daily_call_cap=1)
    clock = Clock()
    transport = ScriptedTransport()
    worker = Worker(config, transport=transport, clock=clock)
    cycle = worker.cycle()
    assert cycle["model_calls"] == 1 and len(transport.calls) == 1
    deferred = [cid for cid, record in cycle["records"].items() if record["not_before"] > clock.now]
    served = [cid for cid, record in cycle["records"].items() if record["phase"] == "compacting"]
    assert len(deferred) == 1 and len(served) == 1
    assert cycle["records"][deferred[0]]["error_category"] == "daily_cap"
    assert cycle["records"][deferred[0]]["not_before"] == ledger.next_utc_day(clock.now)
    again = worker.cycle()  # the day's budget is spent: no launch, the other conversation keeps moving
    assert len(transport.calls) == 1 and again["records"][served[0]]["phase"] == "publishing"
    clock.now = ledger.next_utc_day(clock.now) + 1
    results = run_until_idle(worker, clock, cycles=20)
    assert all(r["published_W"] == 2 for r in results[-1]["records"].values())
    assert len(transport.calls) == 2
    # The per-cycle ceiling spreads launches across cycles and round-robin serves the other conversation first.
    with connect(config.db_path) as db:
        for cid in pair:
            say(db, config, cid, "note 3")
    spread = Worker(worker_config(tmp_path, conversations=pair, calls_per_cycle=1), transport=transport, clock=clock)
    one = spread.cycle()
    assert one["model_calls"] == 1 and len(transport.calls) == 3
    waiting = [cid for cid, record in one["records"].items() if record["phase"] == "extracting"]
    moved = [cid for cid, record in one["records"].items() if record["phase"] == "compacting"]
    assert len(waiting) == 1 and len(moved) == 1 and one["records"][waiting[0]]["attempts"] == 0
    two = spread.cycle()
    assert two["model_calls"] == 1 and two["records"][waiting[0]]["phase"] == "compacting"
    assert two["stepped"][0] == waiting[0]  # the position advanced, so the waiting conversation went first
    final = run_until_idle(spread, clock, cycles=20)[-1]
    assert all(r["published_W"] == 3 for r in final["records"].values())


def test_unlimited_daily_cap_still_counts_calls_and_keeps_cycle_and_attempt_bounds(tmp_path) -> None:
    settings = {"daily_call_cap": 0, "calls_per_cycle": 1, "batch_size": 1}
    config = worker_config(tmp_path, conversations=["placeholder"], **settings)
    with connect(config.db_path) as db:
        first_cid, _ = converse(db, config, 5)
        second_cid, _ = converse(db, config, 5)
    pair = [first_cid, second_cid]
    config = worker_config(tmp_path, conversations=pair, **settings)
    clock = Clock()
    day = ledger.utc_day(clock.now)
    transport = ScriptedTransport()
    worker = Worker(config, transport=transport, clock=clock)
    results = []
    while len(transport.calls) < 5 and len(results) < 40:
        results.append(worker.cycle())
    assert status(config)["calls"] == {day: 5}  # launches are still reserved and counted
    # A restart keeps the day's counter, and neither it nor the default ceiling of 8 stops the catch-up.
    restarted = Worker(config, transport=transport, clock=clock)
    while len(transport.calls) < 10 and len(results) < 80:
        results.append(restarted.cycle())
    results += run_until_idle(restarted, clock)
    assert ledger.utc_day(clock.now) == day and status(config)["calls"] == {day: 10}
    assert all(r["published_W"] == 5 for r in results[-1]["records"].values())
    assert max(result["model_calls"] for result in results) == 1  # the per-cycle ceiling still spreads launches
    assert not any(r["error_category"] == "daily_cap" for result in results for r in result["records"].values())
    # The attempt bound is untouched: two failed extraction calls still block durably.
    with connect(config.db_path) as db:
        say(db, config, first_cid, "note 6")
    transport.script = ["timeout", "garbage"]
    blocked = run_until_idle(restarted, clock)[-1]["records"][first_cid]
    assert (blocked["phase"], blocked["attempts"], blocked["error_category"]) == ("blocked", 2, "extraction_failed")
    assert len(transport.calls) == 12 and status(config)["calls"] == {day: 12}
    clock.now = ledger.next_utc_day(clock.now) + 1
    assert restarted.cycle()["stepped"] == [] and len(transport.calls) == 12  # a new day clears nothing
    with connect(config.db_path) as db:
        say(db, config, second_cid, "note 6")
    run_until_idle(restarted, clock)
    assert status(config)["calls"] == {ledger.utc_day(clock.now): 1}  # the counter still rolls over by UTC day
