"""Compatibility entry point for ``python -m baton.secretary_decide``."""

from baton.agents.secretary.decide import main

if __name__ == "__main__":
    raise SystemExit(main())
