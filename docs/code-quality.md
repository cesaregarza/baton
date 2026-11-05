# Code quality

Run `uv sync --locked` once, then `make check` before pushing. These checks do not run the application,
connect to a database, or call a model. `make test` runs the separate test suite.

## Ruff: lint and formatting

Ruff owns linting, import order, and formatting. Settings live in `pyproject.toml`; its version is pinned
so CI and editors use the same rules and formatter. Use `make format` to sort imports and format files,
or `uv run --locked ruff check --fix .` for safe lint fixes. Unsafe fixes are never applied automatically.

The code targets Python 3.12, uses four-space indentation, double quotes, LF line endings, and a
120-character line length. Existing correctness, import, bugbear, and modern-Python checks remain enabled.
Unused suppression comments are errors.

Production functions also have these limits:

| Check | Limit | What to do when it fails |
| --- | --- | --- |
| `C901` | McCabe complexity 12 | Split independent decisions into named operations. |
| `PLR0912` | 12 branches | Separate validation categories or workflow stages. |
| `PLR0915` | 50 statements | Give a coherent part of the function its own owner or helper. |

Function size is measured in statements, not physical lines: wrapping an expression or adding an explanation
should not force a refactor. Test scenarios are exempt from these three maintainability limits so setup,
actions, and assertions can remain together; all other lint and format rules still apply to tests.
Early returns are allowed. Do not trade clear guard clauses for nested control flow just to reduce return count.

These checks pass for the existing production code. New helpers should name a responsibility, preserve
validation order and transaction boundaries, and make the caller easier to follow. Do not raise limits or
add broad ignores to accommodate a new violation.

## ty: fast static type checking

`make typecheck` runs Astral's ty against `baton/`, targeting Python 3.12. It uses the project's installed
dependencies and does not require every function to be annotated. Tests remain checked by Ruff and pytest.

All production code is checked without a diagnostic baseline. Fix reported errors at their source: validate
external values, narrow optional values, and describe the actual contracts between components. Database
queries that require exactly one row use `one()`; queries where absence is valid use `fetchone()` and handle
`None`. Do not replace useful types with `Any` or add blanket exclusions to make a check pass.

ty's exact version is pinned so developers and CI use the same diagnostics. Review newly reported findings
when upgrading it.

References: [Ruff complexity](https://docs.astral.sh/ruff/rules/complex-structure/),
[statement limits](https://docs.astral.sh/ruff/rules/too-many-statements/),
[formatter](https://docs.astral.sh/ruff/formatter/), and
[ty](https://docs.astral.sh/ty/).
