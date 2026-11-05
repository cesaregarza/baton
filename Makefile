.PHONY: check lint format format-check typecheck test

UV ?= uv

# The same fast, read-only checks run in CI.
check: lint format-check typecheck

lint:
	$(UV) run --locked ruff check .

format-check:
	$(UV) run --locked ruff format --check .

typecheck:
	$(UV) run --locked ty check

format:
	$(UV) run --locked ruff check --select I --fix .
	$(UV) run --locked ruff format .

test:
	$(UV) run --locked pytest -q
