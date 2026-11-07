# Delivering secretary summaries from a host adapter

`secretary_delivery.py` holds the decisions a host adapter makes when it wakes its principal with Baton's secretary
summaries: how the summaries are framed, whether they may replace the principal's usual rereads, and which of the
host's own admission ids a confirmed text may mark announced. Install it beside the adapter as the exact bytes of a
repository commit, and check its sha256 at install. It uses the standard library only and imports nothing from Baton,
so an adapter on the system interpreter can import it while the secretary itself runs under Baton's.

Everything specific to one principal stays in the adapter and is passed in: the start and end markers, the
instruction wording, helper names, state paths and thread ids.

## A historical summary

When a callback's trigger is not on the next bounded page (beyond it, or already behind it), Baton's secretary
assesses that page anyway, as historical catch-up. The result's `trigger` is null and it is stored with
`catch_up: true`. It says nothing about the callback's trigger, whose own state stays `not_reached` (or `retry`)
until a later page covers it. A historical summary is still a real assessment of its own page: an alert it wakes
for is a wake that page earned, and it is delivered and acknowledged like any other.

The host's report must carry `catch_up` with each result and pending summary. `frame` marks every summary
`historical catch-up` or `not catch-up`, prints its coverage (`since`, `through_pos`, `covered_through`) exactly as
the core reported it, and says in the header how many of the summaries carried are historical.

## When the summaries replace the rereads

A notification normally tells the principal to read the current inbox and any replay the host keeps. A text whose
summaries replace those rereads ("act from the brief") takes on the secretary's coverage in their place, so it is
allowed only when that coverage includes the wake's own trigger:

- a wake: only when the secretary decided it and the host reports `trigger_state` `assessed`;
- a scheduled turn with no trigger: only when none of the summaries is historical catch-up.

Otherwise the usual rereads stay in the text, the summaries ride along as additional context, and a line names the
unassessed trigger and its `trigger_state`, which is still handled through the host's existing fallback. A text with
no summaries is the host's usual text, unchanged. `reading` makes the choice and `lead` applies it to a text.

## Four separate facts

A host's receipts keep these apart and never present one as another:

| Fact | What it says | What it does not say |
|---|---|---|
| Queue status | whether the principal's runtime confirmed the queued text (only a confirmed queue counts) | that the principal read it or acted |
| Announced admission ids | which messages the host's own admission stops offering as new wake candidates | that those messages were delivered, read or assessed |
| Secretary coverage | the trigger's `trigger_state`, which carried summaries are historical, and each one's coverage | that the principal saw any of it |
| Principal delivery | the summary decision ids a confirmed text carried, recorded before they are acknowledged | anything about ids no confirmed text carried |

Acknowledge a summary to the secretary only after a confirmed queue, by the exact decision ids that text carried,
and record them first, so a crash between the two acknowledges late rather than delivering again. Never put an id
that was recorded, acknowledged or not, into a later text.

## Announcing admission ids

After a confirmed queue, `announceable` gives the admission ids the text took responsibility for:

- a text that kept the usual rereads: every admission id, since it asked the principal to read the inbox and the
  replay;
- a text whose summaries replaced the rereads: only the ids the assessment covered (`covered`: the trigger's own id
  where it was assessed, and the messages the carried summaries cite).

A queued summary, historical or not, is never on its own a reason to announce the current message. Nothing is
announced after a queue that was not confirmed.
