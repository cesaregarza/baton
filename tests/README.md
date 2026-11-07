# Testing Baton

The suite protects message delivery, permissions, persistent state, and the boundaries used by agent hosts.
Optimize for useful failure detection and quick feedback. Test count and coverage percentages are not goals.

## Choose the behavior first

Start with the failure a user would observe. Extend the existing scenario for that feature when possible.
Before adding a test, identify the failure it catches that the existing suite does not. If two tests protect
the same rule, keep one owning scenario and move any distinct assertions into it. A new helper, file, or
review round is not a reason to add another test.
For a bug fix, reproduce the failure before changing the implementation. For new behavior, decide its
guarantees before writing the code or tests.

Keep focused checks for parsing and other pure rules when they are fast and meaningful. Use real SQLite
and the domain or MCP entry point when the guarantee involves persisted state, permissions, SDK validation,
or response shape. Keep real processes, sockets, and TLS where process death, cancellation, file locking,
command serialization, or network behavior is the point of the test.

Do not add tests just to prove a helper exists, an internal module owns a function, prose has not changed,
or implementation constants equal themselves. Tool contracts should name the important fields and permissions;
opaque hashes of entire tool descriptions turn harmless edits into test maintenance.

## Consolidate repeated work

- Check historical upgrades once per fixture version, including preserved data and all added tables.
- For rejected inputs with the same initial state, reuse one app/database scenario. Keep every meaningful
  input, assert its result and lack of side effects immediately, and identify the failing input.
- Keep separate scenarios when setup, state transitions, or failure boundaries differ. Do not create one
  giant workflow merely to reduce the number of collected cases.
- Build only the state a scenario needs. Do not migrate an old database or create unrelated topics and
  aliases for tests that need a fresh database. Keep mutable state isolated between scenarios.
- For pagination and capacity rules, use a small configured limit and enough items to cross it. Assert that
  the bound is actually applied, as well as traversal, refusal, or recovery. Keep published default-value
  checks separate from the repeated boundary exercise; do not silently weaken a fixed public limit.
- Wait for the durable condition being tested. A request log, subprocess startup delay, or arbitrary sleep
  does not prove that an assessment, receipt, or delivery has finished. Poll state directly; exercise the
  CLI explicitly at the points where its behavior matters.

## Exercise interactions and failures

Secretary bugs often depend on ordering: receive, hold, retry, acknowledge, revoke access, restart, or
complete overlapping callbacks. Prefer scenarios that check guarantees across these transitions over
separate assertions about each helper. Use controlled clocks and explicit barriers for ordering, with
scripted provider replies at the I/O boundary. Preserve real crash and cancellation checks separately.

When consolidating critical regression coverage, a targeted defect reintroduction in a disposable copy can
show that the retained scenario still detects the failure. This is useful evidence, not a mandatory extra
suite for every edit. Do not add a test framework or broad parameter matrix without a concrete coverage gap.

Fake model replies verify harness behavior; they do not evaluate triage quality or summary usefulness.
Likewise, checking CSS strings does not establish layout. For layout changes, check long content and paging
in a browser at narrow and wide viewports; keep HTML tests honest about what they actually establish.

## Run the checks

Install locked dependencies with `uv sync --locked`. During an edit, run the affected scenarios, for example:

```sh
uv run --locked pytest -q tests/test_reference_boundary.py
uv run --locked pytest -q tests/test_secretary_integration.py -k pending_result
```

After the changes are integrated, run `make check` and `make test`. Repeat focused checks after fixes;
do not rerun the entire suite after every helper or documentation edit. CI keeps both Python 3.12 and 3.13
and reports its ten slowest test phases. Use `--durations=10` locally when investigating runtime.

All scenarios use temporary state and synthetic model/host commands. No paid inference or deployed hooks
are needed. See [code quality](../docs/code-quality.md) for Ruff, formatting, and static type checks.
