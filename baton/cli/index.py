"""Local indexing commands: arguments, JSON input, and database connection ownership.

Read commands open the database read-only. Writer commands open it before evaluating their
operation, including JSON input; the derived-data functions own their transactions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from baton import changes, derived
from baton.config import Config, config_path
from baton.database.store import connection
from baton.errors import InvalidRequest


def add_commands(commands: Any) -> None:
    """Local commands for the host owner's indexing worker: no role, no token, no MCP."""
    index = commands.add_parser(
        "index", help="change feed and derived-data writer for a local indexing worker"
    ).add_subparsers(dest="index_command", required=True)
    changes_parser = index.add_parser("changes", help="changes after a cursor, oldest first, metadata only")
    changes_parser.add_argument("--after", help="the next_cursor of the last call; omit to start at the oldest kept")
    changes_parser.add_argument("--limit", type=int, default=100)
    changes_parser.set_defaults(
        handler=lambda config, args: _index(
            config,
            lambda conn: changes.read(conn, changes.Scope.ALL, cursor=args.after, limit=args.limit),
            read_only=True,
        )
    )
    wait_parser = index.add_parser("wait", help="block until something changes after a cursor; prints only its seq")
    wait_parser.add_argument("--after", required=True)
    wait_parser.add_argument("--timeout", type=float, default=30.0, help="seconds")
    wait_parser.set_defaults(
        handler=lambda config, args: _index(
            config,
            lambda conn: changes.wait(conn, changes.Scope.ALL, cursor=args.after, timeout_seconds=args.timeout),
            read_only=True,
        )
    )
    dirty_parser = index.add_parser("dirty", help="what a producer has no current version of; read-only")
    dirty_parser.add_argument("--producer", required=True, help="whose set to list; not checked against the config")
    dirty_parser.add_argument("--generation", help="which of its generations; default: the active one")
    dirty_parser.set_defaults(
        handler=lambda config, args: _index(
            config, lambda conn: derived.dirty(conn, config, args.producer, generation=args.generation), read_only=True
        )
    )
    begin = index.add_parser("begin-batch", help="issue a manifest of the messages a batch reads")
    begin.add_argument("--producer", required=True)
    begin.add_argument("--conversation", required=True)
    begin.add_argument(
        "--kinds",
        required=True,
        help=f"comma-separated, from {', '.join(derived.KINDS)}; or "
        f"{derived.RECENT} alone, when [search] rolling_window_messages is set",
    )
    begin.add_argument("--generation", help="the generation to fill; default: the active one")
    begin.add_argument("--through-seq", type=int, help="cumulative summary ceiling; newer posts stay live")
    begin.set_defaults(
        handler=lambda config, args: _index(
            config,
            lambda conn: derived.begin_batch(
                conn,
                config,
                args.producer,
                args.conversation,
                args.kinds.split(","),
                generation=args.generation,
                through_seq=args.through_seq,
            ),
        )
    )
    read = index.add_parser("read-batch", help="one page of a batch's messages, recorded as delivered")
    read.add_argument("batch")
    read.add_argument("--page", type=int, required=True)
    read.add_argument("--producer", required=True)
    read.set_defaults(
        handler=lambda config, args: _index(
            config, lambda conn: derived.read_batch(conn, config, args.producer, args.batch, args.page)
        )
    )
    commit = index.add_parser("commit-batch", help="write a version of each kind from a delivered batch")
    commit.add_argument("batch")
    commit.add_argument("--file", required=True, help="the commit as JSON: request_id and artifacts; - for stdin")
    commit.add_argument("--producer", required=True)
    commit.set_defaults(
        handler=lambda config, args: _index(
            config, lambda conn: derived.commit_batch(conn, config, args.producer, args.batch, _json_file(args.file))
        )
    )
    _add_graph_commands(index)
    generation = index.add_parser(
        "generation", help="make, switch to, or drop a producer's generations"
    ).add_subparsers(dest="generation_command", required=True)
    create = generation.add_parser("create", help="a new, inactive generation to fill")
    create.add_argument("--producer", required=True)
    create.add_argument("--profile", help="its profile as JSON (summarizer, embedding); - for stdin")
    create.set_defaults(
        handler=lambda config, args: _index(
            config,
            lambda conn: derived.generation_create(
                conn, config, args.producer, _json_file(args.profile, "--profile") if args.profile else None
            ),
        )
    )
    activate = generation.add_parser("activate", help="switch readers to a generation, if it is complete")
    activate.add_argument("generation")
    activate.add_argument("--producer", required=True)
    activate.set_defaults(
        handler=lambda config, args: _index(
            config, lambda conn: derived.generation_activate(conn, config, args.producer, args.generation)
        )
    )
    drop = generation.add_parser("drop", help="drop an inactive generation and free its slot")
    drop.add_argument("generation")
    drop.add_argument("--producer", required=True)
    drop.set_defaults(
        handler=lambda config, args: _index(
            config, lambda conn: derived.generation_drop(conn, config, args.producer, args.generation)
        )
    )


def _index(config: Config, operation: Any, *, read_only: bool = False) -> Any:
    with connection(config.db_path, read_only=read_only) as conn:
        return operation(conn)


def _json_file(path: str, flag: str = "--file") -> Any:
    try:
        return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    except (OSError, ValueError) as error:
        raise InvalidRequest(f"{flag} must name readable JSON: {type(error).__name__}") from None


def _graph_export(config: Config, args: Any) -> Any:
    from baton.graph.export import export_source, invalid_graph

    return invalid_graph(
        lambda: _index(
            config,
            lambda conn: export_source(
                conn,
                config,
                args.conversation,
                args.file,
                through_seq=args.through_seq,
                max_posts=args.max_posts,
                max_bytes=args.max_bytes,
                protected_paths=(config_path(args.config),),
            ),
            read_only=True,
        )
    )


def _graph_publish(config: Config, args: Any) -> Any:
    from baton.graph.export import invalid_graph
    from baton.graph.publication import publish_graph

    return invalid_graph(
        lambda: _index(
            config,
            lambda conn: publish_graph(
                conn,
                config,
                args.producer,
                args.source,
                args.state,
                receipt_path=args.receipt,
                generation=args.generation,
                protected_paths=(config_path(args.config),),
            ),
        )
    )


def _add_graph_commands(index: Any) -> None:
    export = index.add_parser("graph-export", help="save a complete native prefix for graph generation")
    export.add_argument("--conversation", required=True)
    export.add_argument("--file", required=True)
    export.add_argument("--through-seq", type=int)
    export.add_argument("--max-posts", type=int, default=10_000)
    export.add_argument("--max-bytes", type=int, default=20_000_000)
    export.set_defaults(handler=_graph_export)
    publish = index.add_parser("graph-publish", help="publish a completed graph as a cumulative summary")
    publish.add_argument("--producer", required=True)
    publish.add_argument("--source", required=True)
    publish.add_argument("--state", required=True)
    publish.add_argument("--receipt", help="private attempt file; default is a per-publication sibling file")
    publish.add_argument("--generation")
    publish.set_defaults(handler=_graph_publish)
