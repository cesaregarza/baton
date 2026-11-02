# Agent organisation model

**Status: package design proposal.** This document defines the recommended organisation and the behavior
needed to support it. It distinguishes existing mechanisms from proposed interfaces; it adds no
configuration keys, tools, permissions, or running agents. See [Architecture and design](design.md) and
the [reference](reference.md) for implemented behavior.

## Purpose

Baton organises agents around responsibility, continuity, and attention. Core agents handle consequential
coordination; secretaries protect their attention; topic managers own bounded ongoing work; ephemeral
workers complete specific assignments. Permissions remain explicit at every level.

The design borrows useful organisational functions: delegation, continuity, attention management, clear
ownership, and independent review. These are design choices to evaluate, not evidence that adding layers
will improve every workload. The target is lower total cost per accepted outcome, with reliable follow-up
and appropriate review.

This model belongs in Baton's package documentation and reusable interfaces. An installation selects
providers, models, agents, and admission policies. It should not need a different private definition of
what a secretary or topic manager does.

## Four agent profiles

Here, a **profile** is an organisational class. It is distinct from a configured Baton **role**, which is
an address and a set of capabilities.

| Profile | Responsibility | Continuity | Authority |
|---|---|---|---|
| **Core** | Own consequential decisions, coordination, and escalations; delegate bounded execution. | Durable identity, assignments, and context. | Explicit configured capabilities and applicable approvals. |
| **Secretary** | Protect one principal's attention, track outstanding requests, and prepare useful briefs. | Durable per-principal state; inexpensive processing can be short-lived. | Explicitly scoped inputs and attention bookkeeping; no inherited principal authority. |
| **Manager** | Own ongoing work within assigned topics; decompose, delegate, and integrate results. | Persistent identity and topic context across assignments. | Guest capabilities plus explicitly authorised topic grants. |
| **Worker** | Complete a bounded assignment and return an artifact or evidence-backed result. | Ephemeral execution; retained outputs preserve its contribution. | Guest capabilities and any separately authorised scope required for the task. |

The recommended arrangement assigns a secretary to each core agent. Managers have no secretary by
default; the same relationship can be added when measured attention load warrants it. A worker normally
returns to its assigned owner without acquiring its own management structure.

A secretary supports its principal alongside the work hierarchy. It is not another approval stage between
every manager and core agent. Agents can communicate directly through permitted conversations; useful
information need not traverse every tier.

## Keep the dimensions separate

An implementation should represent these independently:

| Dimension | What it describes |
|---|---|
| Profile | Organisational responsibility: core, secretary, manager, or worker. |
| Execution model | How the runtime works: continuing assistant loop or task-oriented orchestrator/session. |
| Provider and model | The actual execution provider and model; record actual worker authorship for review. |
| Continuity | Identity, checkpoint, and assignment lifetime, separately from a live process or context window. |
| Capabilities | The operations the caller is currently permitted to perform. |
| Scope | The topics, principal, conversations, or other resources to which those capabilities apply. |

Persistence preserves identity and context; it does not grant permissions or guarantee a wake path. A
worker's alias and outputs may remain after its process ends without turning that worker into a manager.
Changing an agent's profile does not change its credentials, scope, or authority. Model cost is a routing
consideration, not a privilege level.

### Core agents and the two-by-two arrangement

The recommended reference organisation has an assistant and an orchestrator from each of two providers:

| Execution model | Provider A | Provider B |
|---|---|---|
| Assistant | Core assistant and its secretary | Core assistant and its secretary |
| Orchestrator | Core orchestrator and its secretary | Core orchestrator and its secretary |

An **assistant** uses a continuing loop intended to handle multiple workstreams and resume when its
runtime supports a trigger. An **orchestrator** uses a task-oriented session to coordinate delegated
execution. Record observed wake, concurrency, continuation, and tool capabilities instead of inferring
them from these names.

Intake, design, implementation, review coordination, and deployment are work assignments. They do not
follow automatically from execution model or provider. Keep one accountable owner for each bounded
outcome and identify any separately owned operation, such as a release.

The two-provider arrangement supports an independent review policy. Route review by the actual patch
authors, including manager edits, rather than the coordinating agent's provider. The recommended policy
uses a reviewer from a different provider who did not design or implement the change. Mixed-provider
authorship needs an explicitly handled review route; an exception remains a recorded operator decision.
Provider diversity alone does not establish independence or correctness.

Agent names, two specific vendors, and exactly four core agents are deployment choices. The package
should provide the reference arrangement without making a smaller installation invalid.

## Topic managers and workers

A topic manager keeps the local work context, prepares bounded assignments, collects results, and handles
ordinary rework within the accepted scope. It resolves routine reversible choices within the accepted
intent and escalates consequential product choices beyond that intent, changes to authority or scope,
an exhausted correction budget, or a dependency it cannot resolve. Routine execution does not need to
return to a core agent between every command.

Build manager authority on the existing [topic grants](aliases.md#topic-grant-records). A persistent guest
alias can hold an owner-authorised grant for named capabilities on a stable topic identity. The grant's
coverage, expiry, and state still govern each operation. Outside that grant it retains ordinary guest
capabilities; the manager title does not supply core flags, membership management, or global job authority.

Topic work scope and read visibility are separate. A topic grant does not create a private reading
boundary, and a topic's members do not themselves grant access to all its conversations. Existing guest
read rules still apply. Describing a manager as topic-scoped must not promise isolation that those rules
do not provide. See [Guest access](../SECURITY.md#guest-access).

A worker receives the task contract, required source references, expected output, permitted actions,
validation path, and stop or escalation conditions. Use an inexpensive model capable of meeting the
contract. Give it a complete bounded assignment, including ordinary correction steps, rather than
requiring a senior agent to dispatch every command.

The worker returns an artifact or immutable revision, acceptance results, unresolved findings, and source
references. A failed attempt includes the failing output and attempted correction. Managers and core
agents consume these outputs; when evidence is inadequate, they delegate a correction or independent
check instead of reading the worker's entire chat. Required independent review still applies.

Persist the result and continuation checkpoint before releasing an ephemeral worker. An assignment
waiting on a design choice stays with its owner; it need not keep a coding context alive and polling.

## Secretaries

A **secretary** is an agent responsible for protecting a **principal's** attention. A principal is the
agent it serves. Each core agent has a logical secretary assignment with its own pending requests,
current-work view, admission policy, and delivery state.

This does not require a continuously running model session for every principal. A shared inexpensive
service can process events with isolated per-principal state. Ordinary code handles storage, timers,
deduplication, and delivery coordination; bounded model calls interpret ambiguous messages.

The secretary asks: **has something changed that creates a next action this principal owns?** It can
also retain useful awareness without requiring an interruption or response.

### Inputs and scope

Supply the incoming event, relevant source attribution, current task and artifact identity, expected
results, existing next actor, and the principal's known availability. Build this compact view from the
existing records with source references; do not maintain another competing ownership ledger or reread
the whole portfolio on every notification.

A secretary requires explicit permission to receive the information it processes and to write its own
attention records. A relationship to a principal does not grant access to that principal's mailbox,
credentials, or private conversations. An operator-configured host adapter can supply a bounded input
under the applicable visibility rules. A direct delegated-read API would require its own scoped design.
Existing topic grants do not supply either interface.

The secretary may classify, maintain its outstanding-request view, prepare briefs, and suggest routing
through already assigned workflows. It does not impersonate the principal, make product decisions for
it, create grants, approve work, or acquire deployment authority. Reassignment or expanded execution
scope remains with the appropriate owner. A derived brief remains information, not authorisation.

### Expected results and request state

Track requests the principal owes and results it is waiting to receive. Identify the source request,
requester, expected responder, task or artifact revision, expected result, and current state. An explicit
request field may support this later; no new `ask` argument exists in the current tool schema.

An acknowledgment or later post in the same conversation does not complete a request. `reply_to` is
evidence of a relationship, not proof of fulfillment. A request becomes satisfied when its expected
result is established, or is explicitly cancelled or superseded. A completed review request may create
a correction task. CI passing satisfies the corresponding check expectation for that revision, not an
unrelated review or release decision.

### Output contract

Each decision records a source-linked disposition and brief:

| Disposition | Result |
|---|---|
| Route to an assigned workflow | The existing worker or manager receives work already within its contract. |
| Prepare an action brief | The principal owns a new action; its admission policy determines wake timing. |
| Include in a digest | Retain useful awareness without demanding an immediate response. |
| Retain for retrieval | Keep evidence available without automatically loading it into the principal's context. |
| Reconcile later | Record missing context or a dependency and the condition for reconsideration. |

The brief identifies what changed, the next actor, why attention is needed, relevant task and artifact
identity, whether a response is required, unresolved uncertainty, and evidence references. Preserve
blocking findings and distinguish claimed from independently verified results. Group updates by changed
work rather than producing an indefinitely growing list of one summary per message.

Store the disposition with its input references and policy version so an operator can inspect a hold,
replay a decision, or evaluate a new policy. Reconsider an old decision when relevant task state changes.

### Admission and delivery are separate

Shared admission policy belongs before Baton's configured wake transports. The policy is per principal;
request labels, human relays, or host notices do not imply a universal bypass. Preserve an installation's
documented threshold, exception, fallback, and cadence behavior during migration. Classifier failures
must retain recoverable work and use the configured recovery path.

Each target runtime has one enqueue owner. Native wakes, timers, recovery, and supported worker callbacks
feed that coordinator rather than independently queuing turns. It distinguishes queued work, an active
turn, transport acceptance, packet receipt, and task completion. New arrivals during a turn are coalesced
and reevaluated afterward. A turn ending does not itself satisfy an outstanding request.

Held messages remain available, but are not automatically replayed in full at the next useful wake.
Reconciliation can prepare a digest or selected brief using inexpensive work. Changing an existing
hourly raw-read fallback to this behavior is an explicit delivery change, not merely a classifier tweak.

Keep source delivery cursors, triage state, packet receipt, and task completion distinct. A bounded
intermediary may persist and drain mailbox copies without claiming the principal acted on them. Leaving
all held copies unread indefinitely can exhaust the current mailbox limit and block other participants'
conversation sends. Reading does not delete the retained messages.

Baton transports cannot intercept every native runtime notification. Adapt supported sources to the
common contract and record coverage gaps. Baton's host retains the coordination state; runtime adapters
remain responsible for launching or resuming execution through supported interfaces.

## Package responsibilities and current support

The package should own the common model and contracts. Provider-specific execution, model selection,
host addresses, credentials, and installation-specific policy remain configuration or adapter concerns.

| Concern | Existing foundation | Required addition |
|---|---|---|
| Agent profiles | Configured roles, aliases, and provenance. | Profile and continuity metadata without implicit grants. |
| Persistent topic managers | Guest aliases, stable topic IDs, owner-authorised topic grants. | Documented manager lifecycle and assignments using those mechanisms. |
| Secretary attachment | Configured wake destinations and integration adapters. | Principal-to-secretary relationship, scoped inputs, and durable attention state. |
| Outstanding requests | Messages, `reply_to`, `attention`, decision signals, jobs. | Expected-result tracking and explicit resolution semantics. |
| Selective context | Retained sources and optional producer-supplied artifacts. | Source-backed action briefs and digest generation with appropriate visibility. |
| Shared admission | Event log, dispatcher, retry and coalescing records. | Pluggable per-principal policy and recorded dispositions across supported transports. |
| Runtime coordination | External command, webhook, and MCP Events hooks. | One lifecycle-aware enqueue owner per target and supported callback adapters. |

The [example configuration](../baton.example.toml) remains the source for accepted settings. A future
implementation should ship a validated reference configuration, a CLI/MCP description of agent identity
and profile, secretary input/output contracts, and operator-visible status. Do not add illustrative
configuration keys to a runnable example before the parser implements them.

## Implementation stages

1. Establish the documented profiles and identify current task and expected-result inputs. Preserve
   existing roles and grants; do not remap credentials because a profile name changes.
2. Implement an explicit secretary relationship and bounded input/output contract. Run proposed admission
   decisions in shadow beside the current authoritative path, preserving source and policy provenance.
3. Add durable dispositions and selected briefs, then switch one principal to one authoritative policy.
   Keep the previous path available for rollback without running two independent semantic gates in series.
4. Consolidate runtime enqueue ownership and reconcile held work. Verify lifecycle signals and mailbox
   behavior before replacing existing scheduled or recovery delivery paths.
5. Adapt external event sources, such as CI and issue trackers, with task and artifact correlation.

Existing installations remain usable through these stages. New configurations can adopt the recommended
organisation without forcing every participant through another manager or secretary. A secretary is added
to a topic manager when its attention savings exceed the extra coordination cost.

## Acceptance and measurement

Use total cost per accepted outcome, including secretaries, coordination, execution, review, and rework.
Also measure principal turns and input tokens, missed actionable results, duplicate or stale turns,
unnecessary responses, and time from an actionable result to the responsible actor starting. A small brief
reduces new input; it does not erase an existing large session context.

Before enabling the new delivery path, verify that:

- An untagged answer to an expected result is recognised without requiring every contributor to use a new field.
- An acknowledgment leaves the result outstanding, and a result for an old revision does not satisfy a new one.
- Routine failures within a worker's contract return to that workflow; consequential exceptions reach the owner.
- Native and scheduled signals for the same pending work produce one useful turn, including after recovery.
- A classifier error or adapter restart leaves retained, recoverable work and an observable next step.
- Held traffic remains retrievable without automatic full-backlog delivery or unread-mailbox blockage.
- Briefs preserve source attribution, blocking findings, artifact identity, and the recipient's visibility limits.
- Profile changes and secretary attachments cannot silently widen capabilities or replace required review.

Keep the structure only as elaborate as these outcomes justify. Protecting attention, maintaining
continuity, and delegating execution are the reasons for the profiles; titles are descriptions of those
responsibilities.
