# Working on Baton

Read [the code map](docs/code-map.md) for feature ownership and
[the testing guide](tests/README.md) before adding or changing tests.

Prefer extending an existing behavior scenario to creating another test file or helper framework.
A test should protect an observable contract or a plausible regression. Renames, formatting, documentation
wording, and internal code moves generally need existing checks, not new tests that freeze the implementation.

Run focused checks while editing, then `make check` and the full suite once the change is integrated.
Keep test data and model/host commands synthetic. Do not use a live Baton board for validation.
