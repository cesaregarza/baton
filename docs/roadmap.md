# Roadmap

## Principal feedback for secretary triage

**Status:** Planned, not implemented.

Let a principal correct secretary judgments: flag a needless or false wake, explain a missed urgent item, and
say what matters or what to avoid. A future feedback loop should turn explicit, attributable,
principal-authorized updates into durable principal-specific guidance applied to later triage. Ordinary inbound
or relayed messages must not be able to rewrite that guidance.

The guidance and its effects should be observable, correctable, and reversible. Evaluate candidate guidance
against example alert windows and resulting wake decisions and summaries before relying on it. Choosing a model
alone does not guarantee resistance to gaming or manipulative feedback.

Policy `instructions` already provides static operator preferences. This roadmap item concerns a future
principal feedback loop; it does not change current policy, API, or configuration behavior.
