"""Why a model call failed: one closed set of categories, each attached where its failure is raised.

No code reads a category from message text. The worker settles a call by its category alone: a refusal is a
call the provider declined without doing any work, so it is refunded and retried on a backoff, never charged
and never a block; a durable category blocks the conversation at once; every other category is charged.
"""

from __future__ import annotations

from enum import StrEnum

from baton.graph.contract import GraphError


class Category(StrEnum):
    CANCELLED = "cancelled"
    INPUT_LIMIT = "input_limit"
    ISOLATION = "isolation"
    UNSAFE_SOURCE = "unsafe_source"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    MALFORMED = "malformed"
    TRANSPORT = "transport"
    REQUEST_REJECTED = "request_rejected"
    PROVIDER_ERROR = "provider_error"
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REFUSED = "provider_refused"
    UNVERIFIED_CLIENT = "unverified_client"


REFUSALS = frozenset(
    {
        Category.AUTHENTICATION,
        Category.RATE_LIMITED,
        Category.PROVIDER_UNAVAILABLE,
        Category.PROVIDER_REFUSED,
        Category.UNVERIFIED_CLIENT,
    }
)
DURABLE = frozenset({Category.INPUT_LIMIT, Category.ISOLATION, Category.UNSAFE_SOURCE})


class TransportError(GraphError):
    """A call that produced no acceptable candidate; `category` is fixed by the raiser, never provider text."""

    def __init__(self, category: Category | str, detail: str) -> None:
        self.category = Category(category)
        super().__init__(f"{self.category}: {detail}")


def status_category(status: int) -> Category:
    """The category of a non-success HTTP status, shared by every provider that reports one."""
    if status in (401, 403):
        return Category.AUTHENTICATION
    if status == 429:
        return Category.RATE_LIMITED
    if status == 408 or 500 <= status <= 599:
        return Category.PROVIDER_UNAVAILABLE
    if 400 <= status <= 499:
        return Category.REQUEST_REJECTED
    return Category.TRANSPORT
