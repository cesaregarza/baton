"""Compatibility entry point for ``python -m baton.secretary_read``."""

from baton.agents.secretary.read import main

if __name__ == "__main__":
    raise SystemExit(main())
