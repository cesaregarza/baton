"""The background graph producer: poll heads, extract one batch, compact, publish; never a foreground call.

Each scheduling cycle polls the allowlisted conversations in a read snapshot, then round-robins the pending
ones, giving each at most one step: one graph batch (one reserved model call), one compact phase (one
reserved call unless the deterministic view already fits), or one publication attempt (never a call).
Every external call is reserved in the ledger first, so a crash consumes it; saved candidates are validated
and reused before any new call; exhaustion blocks a conversation durably until an operator resets it.
Model processes never see the authoritative database, the source directory or any Baton secret.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baton.config import Config
from baton.database.store import connection
from baton.errors import BatonError
from baton.graph import compact
from baton.graph import snapshot as snapshot_module
from baton.graph.contract import SCHEMA, GraphError
from baton.graph.export import export_source
from baton.graph.ledger import RETRY_DELAY, Budget, Ledger, WorkerBusy, next_utc_day
from baton.graph.operations import apply, prepare
from baton.graph.publication import publish_graph
from baton.graph.source import canonical, digest, load_json, load_source
from baton.graph.storage import atomic_json, load_state, state_lock
from baton.graph.transport import Limits, Transport, TransportError, for_config

log = logging.getLogger("baton.graph.worker")
EXTRACTION_CALLS = 2
COMPACT_CALLS = 2


class Blocked(Exception):
    """A durable, terminal outcome for one conversation; only an operator reset clears it."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class Deferred(Exception):
    """Try this conversation again no earlier than `until`."""

    def __init__(self, until: float, category: str | None) -> None:
        super().__init__(category or "deferred")
        self.until, self.category = until, category


class CallFailed(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class Cancelled(Exception):
    """The service is stopping: the reserved call is spent, and nothing it returned is applied or published."""


@dataclass(frozen=True)
class Paths:
    """Where one conversation's artifacts live inside the host-owned private state directory."""

    root: Path

    @property
    def source(self) -> Path:
        return self.root / "source.json"

    @property
    def state(self) -> Path:
        return self.root / "graph.json"

    def request(self, watermark: int, revision: int) -> Path:
        return self.root / "requests" / f"w{watermark}-r{revision}.json"

    def proposal(self, request_id: str, attempt: int) -> Path:
        return self.root / "proposals" / f"{request_id}.{attempt}.json"

    def proposals_for(self, request_id: str) -> list[Path]:
        folder = self.root / "proposals"
        return sorted(folder.glob(f"{request_id}.*.json")) if folder.exists() else []

    def compact_view(self, graph_digest: str) -> Path:
        return self.root / "compact" / f"{graph_digest}.json"

    def compact_candidate(self, graph_digest: str, attempt: int) -> Path:
        return self.root / "compact" / f"{graph_digest}.candidate.{attempt}.json"

    def compact_candidates(self, graph_digest: str) -> list[Path]:
        folder = self.root / "compact"
        return sorted(folder.glob(f"{graph_digest}.candidate.*.json")) if folder.exists() else []


class Worker:
    """One host's graph worker; construct it from a config whose `[graph_worker]` is enabled."""

    def __init__(self, config: Config, *, transport: Transport | None = None, clock: Any = time.time) -> None:
        if not config.graph_worker.enabled:
            raise GraphError("the graph worker is disabled ([graph_worker] enabled)")
        self.config, self.settings, self.clock = config, config.graph_worker, clock
        self.transport = transport if transport is not None else for_config(self.settings)
        self.limits = Limits(
            timeout=self.settings.call_timeout_seconds,
            max_input_bytes=self.settings.max_input_bytes,
            max_response_bytes=self.settings.max_response_bytes,
            max_output_tokens=self.settings.max_output_tokens,
        )
        self.cycle_calls = 0
        # Set by `serve`: a stopping service cancels the in-flight call and takes no further step.
        self.stop: threading.Event | None = None

    @property
    def state_dir(self) -> Path:
        assert self.settings.state_dir is not None  # an enabled section always names one
        return self.settings.state_dir

    def paths(self, conversation_id: str) -> Paths:
        return Paths(self.state_dir / conversation_id)

    # Scheduling ---------------------------------------------------------------------------------------------

    def cycle(self, ledger: Ledger | None = None) -> dict[str, Any]:
        """One scheduling cycle under the ledger lock: poll, then at most one step per pending conversation.

        `serve` passes the ledger it owns for its whole lifetime; a direct call opens and releases one.
        """
        if ledger is None:
            with Ledger.open(self.state_dir) as owned:
                return self.cycle(owned)
        interrupted = ledger.consume_interrupted()
        self.poll(ledger)
        self.cycle_calls = 0
        now = self.clock()
        stepped = []
        # Records outlive the allowlist (their counters are durable), but only listed conversations are served.
        listed = sorted(set(ledger.records()) & set(self.settings.conversations))
        for conversation_id in ledger.rotate(listed):
            if self.stop is not None and self.stop.is_set():
                break
            record = ledger.record(conversation_id)
            if record["phase"] in ("blocked", "idle") or record["not_before"] > now:
                continue
            self.step(ledger, conversation_id)
            stepped.append(conversation_id)
        return {
            "interrupted": interrupted,
            "stepped": stepped,
            "model_calls": self.cycle_calls,
            "records": {cid: dict(record) for cid, record in ledger.records().items()},
        }

    def poll(self, ledger: Ledger) -> None:
        """Compare canonical heads with graph W in one read snapshot; coalesce to one pending target each."""
        with connection(self.config.db_path) as conn, conn.snapshot():
            for conversation_id in self.settings.conversations:
                row = conn.execute("SELECT last_seq FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                if row is None:
                    continue
                record = ledger.record(conversation_id, str(self.paths(conversation_id).state))
                head = row["last_seq"]
                if record["phase"] == "blocked":
                    continue
                if record["phase"] == "idle" and head > record["accepted_W"]:
                    ledger.update(conversation_id, phase="pending", target_head=head)
                elif head != record["target_head"]:
                    ledger.update(conversation_id, target_head=head)

    def step(self, ledger: Ledger, conversation_id: str) -> None:
        phase = ledger.record(conversation_id)["phase"]
        try:
            if phase in ("pending", "extracting"):
                self._extract(ledger, conversation_id)
            elif phase == "compacting":
                self._compact(ledger, conversation_id)
            elif phase == "publishing":
                self._publish(ledger, conversation_id)
        except Blocked as blocked:
            log.warning("graph worker blocked %s: %s", conversation_id, blocked.category)
            ledger.block(conversation_id, blocked.category)
        except Deferred as deferred:
            ledger.defer(conversation_id, deferred.until, deferred.category)
        except Cancelled:
            log.info("graph worker stopped during a call for %s; its attempt is spent", conversation_id)

    # Shared helpers -----------------------------------------------------------------------------------------

    def _load(self, paths: Paths) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            source = load_source(paths.source)
            with state_lock(paths.state):
                return source, load_state(paths.state, source)
        except GraphError:
            raise Blocked("unsafe_source") from None

    def _call(self, ledger: Ledger, conversation_id: str, phase: str, request: dict, schema: dict, cap: int) -> dict:
        """Reserve, then make exactly one bounded call; every outcome settles the reservation."""
        now = self.clock()
        try:
            ledger.reserve(
                conversation_id,
                phase,
                digest(request),
                now=now,
                daily_cap=self.settings.daily_call_cap,
                cycle_calls=self.cycle_calls,
                cycle_cap=self.settings.calls_per_cycle,
                attempt_cap=cap,
            )
        except Budget as budget:
            if budget.category == "daily_cap":
                raise Deferred(next_utc_day(now), "daily_cap") from None
            if budget.category == "cycle_cap":
                raise Deferred(now, None) from None
            raise Blocked(f"{phase}_failed") from None
        self.cycle_calls += 1
        started = self.clock()
        try:
            produced = self.transport.produce(request, schema, self.limits, stop=self.stop)
        except TransportError as error:
            ledger.settle({"outcome": error.category, "elapsed": self.clock() - started})
            if error.category == "cancelled":
                raise Cancelled() from None
            if error.category in ("authentication", "isolation", "input_limit"):
                raise Blocked(error.category) from None
            raise CallFailed(error.category) from None
        except BaseException:
            ledger.settle({"outcome": "crash", "elapsed": self.clock() - started})
            raise
        if self.stop is not None and self.stop.is_set():  # returned during shutdown: spent, never applied
            ledger.settle({"outcome": "cancelled", "elapsed": self.clock() - started})
            raise Cancelled()
        ledger.settle(
            {
                "outcome": "returned",
                "elapsed": produced.get("elapsed", self.clock() - started),
                "input_bytes": produced.get("input_bytes"),
                "output_bytes": produced.get("output_bytes"),
                "usage": produced.get("usage", "unknown"),
                "turns": produced.get("turns"),
            }
        )
        return produced["candidate"]

    def _retry_or_block(self, ledger: Ledger, conversation_id: str, cap: int, block: str, failure: str) -> Exception:
        if ledger.record(conversation_id)["attempts"] < cap:
            return Deferred(self.clock() + RETRY_DELAY, failure)
        return Blocked(block)

    def _correction(self, request: dict, rejected: tuple[str, Any] | None) -> dict:
        model_request = request
        if rejected is not None:
            model_request = {**request, "correction": {"validation_error": rejected[0], "rejected_output": rejected[1]}}
        if len(canonical(model_request).encode("utf-8")) > self.settings.max_input_bytes:
            raise Blocked("input_limit")
        return model_request

    # Extraction ---------------------------------------------------------------------------------------------

    def _extract(self, ledger: Ledger, conversation_id: str) -> None:
        paths = self.paths(conversation_id)
        paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with connection(self.config.db_path) as conn:
                export_source(conn, self.config, conversation_id, paths.source)
        except GraphError:
            raise Blocked("unsafe_source") from None
        source, state = self._load(paths)
        record = ledger.record(conversation_id)
        if state["watermark"] > record["accepted_W"] or state["watermark"] >= len(source["messages"]):
            # The checkpoint already advanced (an apply the ledger never saw): compact and publish it first.
            self._after_apply(ledger, conversation_id, state)
            return
        ledger.update(conversation_id, phase="extracting")
        request, request_path = self._request(paths, source, state)
        request_id = request["binding"]["request_id"]
        ledger.begin(conversation_id, request_id)
        if request_id in state["receipts"]:  # applied before a crash: the receipt is the proof
            self._after_apply(ledger, conversation_id, state)
            return
        rejected = None
        for saved in paths.proposals_for(request_id):  # a saved candidate is reused before any new call
            try:
                apply(paths.source, paths.state, request_path, saved)
            except GraphError as error:
                rejected = (str(error), load_json(saved))
                continue
            self._after_apply(ledger, conversation_id, self._load(paths)[1])
            return
        if ledger.record(conversation_id)["attempts"] >= EXTRACTION_CALLS:
            raise Blocked("extraction_failed")
        model_request = self._correction(request, rejected)
        try:
            candidate = self._call(ledger, conversation_id, "extracting", model_request, SCHEMA, EXTRACTION_CALLS)
        except CallFailed as failed:
            raise self._retry_or_block(
                ledger, conversation_id, EXTRACTION_CALLS, "extraction_failed", failed.category
            ) from None
        attempt = ledger.record(conversation_id)["attempts"]
        atomic_json(paths.proposal(request_id, attempt), candidate, replace=False)
        try:
            apply(paths.source, paths.state, request_path, paths.proposal(request_id, attempt))
        except GraphError:
            raise self._retry_or_block(
                ledger, conversation_id, EXTRACTION_CALLS, "extraction_failed", "rejected"
            ) from None
        self._after_apply(ledger, conversation_id, self._load(paths)[1])

    def _request(self, paths: Paths, source: dict, state: dict) -> tuple[dict, Path]:
        request_path = paths.request(state["watermark"], state["revision"])
        if request_path.exists():
            request = load_json(request_path)
            binding = request.get("binding", {}) if isinstance(request, dict) else {}
            if binding.get("base_revision") != state["revision"] or binding.get("base_state_digest") != digest(state):
                raise Blocked("unsafe_source")
            return request, request_path
        try:
            request = prepare(
                paths.source,
                paths.state,
                request_path,
                batch_size=self.settings.batch_size,
                max_input_bytes=self.settings.max_input_bytes,
            )
        except GraphError as error:
            detail = str(error)
            if "recall" in detail:
                raise Blocked("missing_recall") from None
            raise Blocked("input_limit" if "exceeds" in detail else "unsafe_source") from None
        return request, request_path

    def _after_apply(self, ledger: Ledger, conversation_id: str, state: dict) -> None:
        record = ledger.record(conversation_id)
        accepted = state["watermark"]
        if accepted > record["published_W"]:
            phase = "compacting"
        else:
            phase = "pending" if record["target_head"] > accepted else "idle"
        ledger.update(
            conversation_id, accepted_W=accepted, phase=phase, request_id=None, attempts=0, error_category=None
        )

    # Compaction ---------------------------------------------------------------------------------------------

    def _bounds(self) -> dict[str, int]:
        return {
            "max_body_bytes": compact.body_limit(self.config.indexer.max_artifact_bytes),
            "max_citations": self.config.indexer.max_citations,
        }

    def _compact(self, ledger: Ledger, conversation_id: str) -> None:
        paths = self.paths(conversation_id)
        source, state = self._load(paths)
        snapshot = snapshot_module.project(state)
        graph_digest = snapshot["graph_digest"]
        view_path = paths.compact_view(graph_digest)
        if view_path.exists():
            self._to_publishing(ledger, conversation_id, graph_digest)
            return
        rendered = compact.render_view(snapshot, source)
        if compact.fits(rendered, snapshot, source, **self._bounds()):  # no model pass, so no model input bound
            atomic_json(view_path, rendered, replace=False)
            self._to_publishing(ledger, conversation_id, graph_digest)
            return
        request = compact.compact_request(snapshot, source)
        if len(canonical(request).encode("utf-8")) > self.settings.max_input_bytes:
            raise Blocked("input_limit")
        ledger.begin(conversation_id, "compact:" + graph_digest)
        rejected = None
        for saved in paths.compact_candidates(graph_digest):
            candidate = load_json(saved)
            try:
                compact.validate_compact(candidate, snapshot, source, **self._bounds())
            except GraphError as error:
                rejected = (str(error), candidate)
                continue
            atomic_json(view_path, candidate, replace=False)
            self._to_publishing(ledger, conversation_id, graph_digest)
            return
        if ledger.record(conversation_id)["attempts"] >= COMPACT_CALLS:
            raise Blocked("compact_failed")
        model_request = self._correction(request, rejected)
        try:
            candidate = self._call(ledger, conversation_id, "compacting", model_request, compact.SCHEMA, COMPACT_CALLS)
        except CallFailed as failed:
            raise self._retry_or_block(
                ledger, conversation_id, COMPACT_CALLS, "compact_failed", failed.category
            ) from None
        attempt = ledger.record(conversation_id)["attempts"]
        atomic_json(paths.compact_candidate(graph_digest, attempt), candidate, replace=False)
        try:
            compact.validate_compact(candidate, snapshot, source, **self._bounds())
        except GraphError:
            raise self._retry_or_block(ledger, conversation_id, COMPACT_CALLS, "compact_failed", "rejected") from None
        atomic_json(view_path, candidate, replace=False)
        self._to_publishing(ledger, conversation_id, graph_digest)

    def _to_publishing(self, ledger: Ledger, conversation_id: str, graph_digest: str) -> None:
        ledger.update(conversation_id, phase="publishing", error_category=None)
        ledger.begin(conversation_id, "publish:" + graph_digest)

    # Publication --------------------------------------------------------------------------------------------

    def _publish(self, ledger: Ledger, conversation_id: str) -> None:
        paths = self.paths(conversation_id)
        source, state = self._load(paths)
        snapshot = snapshot_module.project(state)
        graph_digest = snapshot["graph_digest"]
        view_path = paths.compact_view(graph_digest)
        if not view_path.exists():  # the compact view belongs to another digest: compact this graph first
            ledger.update(conversation_id, phase="compacting", request_id=None, attempts=0)
            return
        record = ledger.begin(conversation_id, "publish:" + graph_digest)
        if record["attempts"] >= self.settings.publication_attempts:
            raise Blocked("publication_failed")
        ledger.update(conversation_id, attempts=record["attempts"] + 1)
        try:
            with connection(self.config.db_path) as conn:
                publish_graph(
                    conn,
                    self.config,
                    self.settings.producer,
                    paths.source,
                    paths.state,
                    generation=self.settings.generation,
                    view=load_json(view_path),
                    attach_graph=True,
                )
        except (GraphError, BatonError, OSError) as error:
            log.warning("graph publication failed for %s: %s", conversation_id, type(error).__name__)
            raise self._retry_or_block(
                ledger, conversation_id, self.settings.publication_attempts, "publication_failed", "publication_failed"
            ) from None
        record = ledger.record(conversation_id)
        ledger.update(
            conversation_id,
            published_W=state["watermark"],
            accepted_W=state["watermark"],
            phase="pending" if record["target_head"] > state["watermark"] else "idle",
            request_id=None,
            attempts=0,
            error_category=None,
        )


def serve(config: Config, stop: threading.Event, *, worker: Worker | None = None) -> None:
    """Own the state directory for the whole service lifetime and run cycles until `stop`.

    The exclusive ledger lock is taken before verification and held through every idle wait, so a second
    service against the same directory ends at once without inference, and an administrative reset is refused
    while the service runs. A stop during a call cancels it and returns only after its process is cleaned up.
    """
    try:
        worker = worker if worker is not None else Worker(config)
        worker.stop = stop
        with Ledger.open(worker.state_dir) as ledger:
            verified = worker.transport.verify()
            log.info("graph worker verified %s", verified.get("mode"))
            while not stop.is_set():
                try:
                    worker.cycle(ledger)
                except Exception as error:  # the loop must survive one bad cycle; nothing it read reaches the log
                    log.warning("graph worker cycle failed: %s", type(error).__name__)
                stop.wait(config.graph_worker.poll_seconds)
    except WorkerBusy:
        log.error("graph worker is off for this run: another worker owns its state directory")
    except (GraphError, OSError) as error:
        log.error("graph worker is off for this run: %s", error)


def status(config: Config) -> dict[str, Any]:
    """The ledger as stored: records, budget and recent sanitized call outcomes. Read-only, no lock needed."""
    settings = config.graph_worker
    if not settings.enabled or settings.state_dir is None:
        return {"enabled": False}
    path = settings.state_dir / "ledger.json"
    ledger = load_json(path) if path.exists() else {"conversations": {}, "calls": {}, "in_flight": None}
    return {"enabled": True, "producer": settings.producer, "provider": settings.provider, **ledger}


def reset(config: Config, conversation_id: str) -> dict[str, Any]:
    """Explicit host administration: clear one blocked record. Stop the service first; the ledger lock is held."""
    settings = config.graph_worker
    if not settings.enabled or settings.state_dir is None:
        raise GraphError("the graph worker is disabled ([graph_worker] enabled)")
    with Ledger.open(settings.state_dir) as ledger:
        if conversation_id not in ledger.records():
            raise GraphError("unknown worker conversation record")
        return dict(ledger.reset(conversation_id))
