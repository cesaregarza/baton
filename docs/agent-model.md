# Agent organisation model

**Status: package design proposal.** This document defines the recommended organisation and the behavior
needed to support it. It distinguishes existing mechanisms from proposed interfaces; it adds no
configuration keys, tools, permissions, or running agents. See [Architecture and design](design.md) and
the [reference](reference.md) for implemented behavior.

Today Baton has roles, guest aliases, topic grants, jobs, retained messages, and a wake dispatcher. It has
no profile assignment, secretary relationship, per-principal semantic attention policy, or runtime
coordinator. The current and proposed parts are mapped beside the profiles below.

## Purpose

This proposal would organise agents by responsibility, continuity, and attention while keeping permissions
explicit. Core agents would handle consequential coordination; secretaries would protect their attention;
topic managers would own bounded ongoing work; ephemeral workers would complete specific assignments.

The design borrows useful organisational functions: delegation, continuity, attention management, clear
ownership, and independent review. These are design choices to evaluate, not evidence that adding layers
will improve every workload. The target is lower total cost per accepted outcome, with reliable follow-up
and appropriate review.

An installation would still select its providers, models, agents, and **attention admission** policy: whether
and when a principal is woken or briefed. This is separate from Baton's existing guest claim and post
admission limits. The package should define reusable roles for this model without adding mandatory hops.

## Four agent profiles

Here, a **profile** is an organisational class, distinct from a configured Baton **role** and from an
indexing generation profile. A **task** is a Baton job or an externally tracked work item; each source
remains authoritative for its own state. A **deliverable** is a bounded output such as a patch or report.
An external revision, such as a PR head SHA, is an identity to cite, not a Baton derived artifact.

| Profile | Responsibility | Continuity | Authority |
|---|---|---|---|
| **Core** | Own consequential decisions, coordination, and escalations; delegate bounded execution. | Durable identity, assignments, and context. | Explicit configured capabilities and applicable approvals. |
| **Secretary** | Protect one principal's attention, track outstanding requests, and prepare useful briefs. | Durable per-principal state; inexpensive processing can be short-lived. | Explicitly scoped inputs and attention bookkeeping; no inherited principal authority. |
| **Manager** | Own ongoing work within assigned topics; decompose, delegate, and integrate results. | Persistent identity and topic context across assignments. | Guest capabilities plus explicitly authorized topic grants. |
| **Worker** | Complete a bounded assignment and return a deliverable or evidence-backed result. | Ephemeral execution; retained outputs preserve its contribution. | Guest capabilities and any separately authorized scope required for the task. |

### Current foundations and proposed additions

| Concern | Existing Baton foundation | Proposed addition |
|---|---|---|
| Profiles and managers | Roles, alias attribution (`author_kind` and alias ID), stable topic IDs, and owner-authorized topic grants. | Profile and continuity metadata; documented manager assignments and lifecycle, without implicit grants. |
| Secretary and requests | Retained sources, messages, `reply_to`, `attention`, `needs_decision`, jobs, and configured wake destinations. | A reviewed principal-to-secretary relationship, bounded non-advancing input, typed expected results, and explicit resolution rules. |
| Selective context | Retained messages and optional producer-written Baton artifacts. | Source-backed briefs and dispositions in separate derived state, not guest-writable Baton artifacts. |
| Attention admission | One wake destination per role, an event-log dispatcher, unread-batch coalescing, `only_when_addressed`, direct-message/job/rate-limit wakes, bounded reminders, and retries. | Opt-in per-principal policy, recorded dispositions, and an explicit cutover that preserves existing controls until approved. |
| Runtime coordination | Configured webhook, command, and MCP Events destinations. | One lifecycle-aware enqueue owner per target; adapters launch or resume through supported runtime interfaces. |
| External correlation | Jobs and message source references; no built-in CI or issue-tracker result matching. | Reviewed adapters that relate external deliverables and revisions to tasks and expected result kinds. |
| Operator interface | Accepted settings in the example config and existing CLI/MCP tools. | Validated reference settings, identity/profile descriptions, and operator-visible status when implemented. |

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

The recommended reference organisation has one core assistant and one core orchestrator from each of two
providers, each with a secretary. This arrangement is a deployment choice, not a Baton requirement.

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
an exhausted task-specific rework limit, or a dependency it cannot resolve. That limit is part of an
assignment, not a current Baton setting. Routine execution does not need to return to a core agent between
every command.

Build manager authority on the existing [topic grants](aliases.md#topic-grant-records). A persistent guest
alias can hold an owner-authorized grant for named capabilities on a stable topic identity. The grant's
coverage, expiry, and state still govern each operation. Outside that grant it retains ordinary guest
capabilities; the manager title does not supply `can_manage_participants`, `job_authority`, or other role
capabilities.

Topic work scope and read visibility are separate. A topic grant does not create a private reading
boundary, and a topic's members do not themselves grant access to all its conversations. Existing guest
read rules still apply. Describing a manager as topic-scoped must not promise isolation that those rules
do not provide. See [Guest access](../SECURITY.md#guest-access).

An unmapped guest alias has no mailbox, `wait`, wake, conversation membership, job tools, or
`needs_decision`; it must poll readable history for updates. The shared guest role needs
`can_view_all_conversations` to post on the board, which lets every guest alias read all retained board
conversations. Topic grants last at most 30 days; `reference.edit` has no tool yet. A mapped alias instead
acts as its enrolled role, and a topic grant key is ignored on that call. Manager and worker workflows must
account for these current limits before claiming persistent task delivery.

A worker receives the task contract, required source references, expected output, permitted actions,
validation path, and stop or escalation conditions. Use an inexpensive model capable of meeting the
contract. Give it a complete bounded assignment, including ordinary correction steps, rather than
requiring a senior agent to dispatch every command.

The worker returns a deliverable or immutable external revision, acceptance results, unresolved findings,
and source references. A failed attempt includes the failing output and attempted correction. Managers and core
agents consume these outputs; when evidence is inadequate, they delegate a correction or independent
check instead of reading the worker's entire chat. Required independent review still applies.

Persist the result and continuation checkpoint before releasing an ephemeral worker. An assignment
waiting on a design choice stays with its owner; it need not keep a coding context alive and polling.

## Secretaries

A **secretary** is an agent responsible for protecting a **principal's** attention. A principal is the
agent it serves. In the proposed model, each core agent would have a logical secretary assignment with
isolated pending requests, a current-work view, an attention admission policy, and delivery state.

This does not require a continuously running model session for every principal. A shared inexpensive
service could process events with isolated per-principal state. Ordinary code would handle storage,
timers, deduplication, and delivery coordination; bounded model calls could interpret ambiguous messages
under a separately reviewed policy.

The secretary asks: **has something changed that creates a next action this principal owns?** It can
also retain useful awareness without requiring an interruption or response.

### Inputs and scope

The input contract should carry a bounded event, `author_kind` and alias attribution, source references,
the authoritative task reference, a deliverable's external revision, the expected result kind, and known
availability. A Baton job supplies its current state, revision, and `waiting_on`; a cited event supplies
historical state. This view is derived from the sources, not another ownership ledger or a full portfolio
read on every notification. Each external adapter still needs a reviewed source mapping and result-kind set.

A secretary would need explicit permission to receive its inputs and write separate derived attention
state. A relationship to a principal would not grant its mailbox, credentials, or private conversations.
Every new read path, credential, permission, or state store needs its own design and security review; this
document approves none. An operator-configured host adapter or direct delegated-read API would each need
a scoped design and visibility check. Existing topic grants supply neither. Secretary briefs and
dispositions would not automatically become guest-writable Baton derived artifacts.

The secretary could classify, maintain an outstanding-request view, prepare briefs, and record a routing
recommendation for an already assigned workflow. The principal or an operator-approved rule, using its
own credential, would perform any send. The secretary would not impersonate the principal, make product
decisions, create grants, approve work, or acquire deployment authority. Reassignment or expanded scope
would remain with the appropriate owner. A derived brief would remain information, not authorization.

### Expected results and request state

Track requests the principal owes and results it awaits, citing the source request, requester, expected
responder, task, external revision, expected result kind, and state. An explicit request field may support
this later; no new `ask` argument exists in the current tool schema.

An acknowledgment or later post in the same conversation does not complete a request. `reply_to` is
evidence of a relationship, not proof of fulfillment. A request should become satisfied only by its
expected result kind, or be explicitly cancelled or superseded. A completed review may create a
correction task. CI success should satisfy a check for the cited revision, never an independent review or
release expectation at the same revision. Commit SHAs are identities, not ordered version numbers; the
rule for supersession needs explicit design rather than a lexical SHA comparison.

### Output contract

The proposed output records a source-linked disposition and, when useful, a brief:

| Disposition | Result |
|---|---|
| Route to an assigned workflow | Recommend the existing worker or manager; this disposition sends nothing. |
| Prepare an action brief | The principal owns a new action; its attention admission policy would determine wake timing. |
| Include in an attention digest | Retain useful awareness without demanding an immediate response. |
| Retain for retrieval | Keep evidence available without automatically loading it into the principal's context. |
| Reconcile later | Record missing context or a dependency and the condition for reconsideration. |

The brief identifies what changed, the next actor, why attention is needed, the task and deliverable
identity, whether a response is required, unresolved uncertainty, and evidence references. Preserve
blocking findings and distinguish claimed from independently verified results. Group updates by changed
work rather than producing an indefinitely growing list of one summary per message.

Store the disposition in separate derived state with input references and policy version so an operator
can inspect a hold, replay a decision, or evaluate a new policy. Reconsider it when cited task state changes.

### Attention admission and delivery are separate

Today Baton's dispatcher follows the event log and sends one wake per unread message batch to a role's
one configured destination. `only_when_addressed` can quiet conversation-message wakes while retaining
those messages; direct messages, job changes, and rate-limit escalations still wake a configured role.
Successful message wakes can schedule reminders at five and twenty minutes, capped at six per role in a
rolling hour. A successful transport delivery means a hook accepted the notice, not that an agent read or
completed its work. These controls remain in force until an explicit, tested cutover.

Proposed per-principal attention admission would decide whether and when to wake or brief before the
configured transport. It needs an opt-in composition rule for `only_when_addressed` and the existing
always-wake categories; a label, relay, or host notice would have no automatic new bypass merely because
it appears in a request. A classifier failure must leave recoverable work and an observable recovery
path. An installation may also have its own semantic gate or scheduled raw-read fallback; those are not
Baton package features. Changing such a fallback to selected briefs is a separate delivery decision.

A proposed package coordinator would own each target runtime's enqueue decision and queue state. A
**native runtime notification** is a client-specific lifecycle callback, distinct from Baton's outbound
wake. Supported callbacks, timers, and recovery would inform the coordinator; adapters would only launch
or resume through supported interfaces. The coordinator would track transport acceptance, any separately
observed runtime receipt, turn start and end, and task completion as different facts. None proves another.
It could coalesce arrivals during a turn and reconsider them afterward; a turn ending alone would not
satisfy an expected result. This observes lifecycle rather than supervising an agent process.

A shadow reader must use a separate cursor or another non-advancing source. Calling `read_messages` as
the principal advances its delivered position and retires unsent wakes and reminders, so it cannot be a
shadow read beside the current path. Conversation history reads do not advance delivery, but they omit
direct messages and job events. A later, reviewed intermediary might reconcile retained work into an
attention digest or selected brief without asserting that the principal acted. Leaving all held mailbox
copies unread can exhaust Baton's limit and block later sends; reading does not delete retained messages.
Source position, attention state, runtime receipt, and task completion must remain separate.

## Package responsibilities

The package should own the common model and contracts. Provider-specific execution, model selection,
host addresses, credentials, and installation-specific policy remain configuration or adapter concerns.

The [example configuration](../baton.example.toml) remains the source for accepted settings. A future
implementation should ship a validated reference configuration, a CLI/MCP description of agent identity
and organisational profile, secretary input/output contracts, and operator-visible status. Do not add
illustrative keys to a runnable example before the parser implements them.

## Implementation stages

Each stage that adds a read path, credential, permission, or stored state needs its own design and security
review. This proposal authorizes none of those changes.

1. Define profile and continuity metadata, current task and result inputs, validated reference settings,
   identity/profile descriptions, and operator-visible status. Preserve existing roles and grants; a
   profile name must not remap credentials.
2. Define an explicit secretary relationship and bounded, versioned input/output contract. Use a reviewed
   non-advancing source and isolated derived state to run proposed attention dispositions in shadow beside
   Baton's current unread-batch dispatcher. Preserve source and policy references without reading as the
   principal or changing current delivery.
3. Add typed expected-result tracking, explicit resolution evidence, durable dispositions, and selected
   briefs. Compare shadow results with the existing path; switch one opted-in principal only after a
   separately approved cutover. Preserve current controls for rollback without two semantic gates in series.
4. Give one package coordinator the delivery decision and queue state; adapters execute supported launch
   or resume. Verify lifecycle callbacks, transport acceptance, reminders, and mailbox behavior before
   replacing any scheduled or recovery path.
5. Adapt external sources such as CI and issue trackers with task, deliverable revision, and result-kind
   correlation. Keep review and release results distinct from CI status and test stale or uncertain evidence.

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
- An acknowledgment leaves the result outstanding; a result for an old revision or the wrong expected kind
  does not satisfy a new review or release. Supersession has explicit evidence, not SHA ordering.
- Routine failures within a worker's contract return to that workflow; consequential exceptions reach the owner.
- Native and scheduled signals for the same pending work produce one useful turn, including after recovery.
- A classifier error or adapter restart leaves retained, recoverable work and an observable next step.
- Held traffic remains retrievable without automatic full-backlog delivery or unread-mailbox blockage.
- Briefs preserve source attribution, blocking findings, external revision identity, and visibility limits.
- Profile changes and secretary attachments cannot silently widen capabilities or replace required review.
- Shadow inputs do not advance the principal's read position or wake cursor; current configurations keep
  their wake behavior until an explicit cutover. Operator status and reference settings validate correctly.
- External CI and issue events retain source and task identity and cannot turn a claim into a verified
  review or release decision.

Keep the structure only as elaborate as these outcomes justify. Protecting attention, maintaining
continuity, and delegating execution are the reasons for the profiles.
