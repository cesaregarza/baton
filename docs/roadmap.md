# Roadmap

## PostgreSQL backend

**Status:** Planned; SQLite is the supported backend today.

The shared connection API uses SQLAlchemy, but the stores still depend on SQLite SQL, FTS5 search,
schema migration details, and writer serialization. Supporting PostgreSQL requires porting those operations
behind the connection and store APIs, defining equivalent search behavior, and running the same message,
retry, permission, and concurrency scenarios against both backends. A SQLAlchemy dependency alone does not
establish backend support.

## Principal feedback for secretary triage

**Status:** Planned, not implemented.

Let a principal correct secretary judgments: flag a needless or false wake, explain a missed urgent item, and
say what matters or what to avoid. A future feedback loop should turn explicit, attributable,
principal-authorized updates into durable principal-specific guidance applied to later triage. Ordinary inbound
or relayed messages must not be able to rewrite that guidance.

The guidance and its effects should be observable, correctable, and reversible. Evaluate candidate guidance
against example alert windows and resulting wake decisions and summaries before relying on it. Choosing a model
alone does not guarantee resistance to gaming or manipulative feedback.

Policy `instructions` provides static operator preferences. This roadmap item concerns a future principal
feedback loop; it is not supported by the current policy or API. The current secretary policy format is version 3.
