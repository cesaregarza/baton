"""Derived data from the host owner's local indexing worker: read batches, artifact versions and receipts.

The worker supplies only text, each artifact's body and the messages it cites. Everything else is Baton's: the
producer (a name listed in `[indexer] producers`), which messages a batch covers (the manifest Baton issued),
the version a write builds on, and the provenance recorded with it. Baton deletes no messages, so nothing here
invalidates a version. A kind that an earlier version of Baton invalidated, when a message it was made from lost
its last copy, is rebuilt by a rebase, a fresh first version from seq 1.

Every writer runs in one transaction and checks its producer before changing state. An identical commit
retry may replay its receipt even after the producer is removed. Nothing here logs a change, adds an event
or wakes anyone, and nothing here is authority: an artifact is a derived reading of messages, never a
decision. Roles read it only through `baton.search`, under the reader's own visibility.

Package map: batches owns batch transactions and receipts; generations owns activation and retirement;
provenance builds and checks bounded manifests; vectors validates and stores embeddings; storage holds
shared artifact operations. Dependencies point from orchestration toward storage, never back to this entry point.
"""

from .batches import begin_batch, commit_batch, dirty, read_batch
from .constants import ITEM_REVISION, KINDS, MAX_REQUEST_ID, RECENT, SCOPE, SKIP_REASONS, UNIT_TOLERANCE
from .generations import generation_activate, generation_create, generation_drop
from .vectors import fits_float32, signature

__all__ = [
    "ITEM_REVISION",
    "KINDS",
    "MAX_REQUEST_ID",
    "RECENT",
    "SCOPE",
    "SKIP_REASONS",
    "UNIT_TOLERANCE",
    "begin_batch",
    "commit_batch",
    "dirty",
    "fits_float32",
    "generation_activate",
    "generation_create",
    "generation_drop",
    "read_batch",
    "signature",
]
