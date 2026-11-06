"""Small helpers shared by local CLI command families."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from baton.config import Config
from baton.database.store import connection


def with_role(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--as", dest="role", help="the role to act as (default: $BATON_ROLE); local mode only")
    return parser


def db(config: Config, operation: Any) -> Any:
    with connection(config.db_path) as conn:
        return operation(conn)


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2)
    sys.stdout.write("\n")
