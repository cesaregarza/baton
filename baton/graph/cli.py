"""Host-config-free graph command parser."""

import argparse
import json
import sys

from baton.graph.contract import SCHEMA, GraphError
from baton.graph.operations import apply, prepare, raw, read
from baton.graph.provider import ProviderOptions, run


def parser():
    root = argparse.ArgumentParser(description="Offline, versioned Baton conversation graphs")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("prepare", "apply", "run", "read", "raw"):
        sub = commands.add_parser(name)
        sub.add_argument("--source", required=True)
        if name != "raw":
            sub.add_argument("--state", required=True)
        if name in {"prepare", "apply", "run"}:
            sub.add_argument("--request", required=True)
        if name in {"apply", "run"}:
            sub.add_argument("--proposal", required=True)
        if name in {"prepare", "run"}:
            sub.add_argument("--batch-size", type=int, default=20)
            sub.add_argument("--max-input-bytes", type=int, default=100_000)
        if name == "read":
            sub.add_argument("--strand", action="append", default=[])
            sub.add_argument("--history", action="store_true")
            sub.add_argument("--statements", action="store_true", help="expand readable working statement clauses")
        if name == "raw":
            sub.add_argument("--seq", type=int, action="append", required=True)
        if name == "run":
            provider_arguments(sub)
    commands.add_parser("schema", help="print the canonical proposal JSON schema")
    return root


def provider_arguments(sub):
    sub.add_argument("--base-url", required=True)
    sub.add_argument("--model", required=True)
    credentials = sub.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--api-key-env")
    credentials.add_argument("--allow-no-key", action="store_true")
    sub.add_argument("--timeout", type=float, default=60)
    sub.add_argument("--max-output-tokens", type=int, default=4000)
    sub.add_argument("--response-format", choices=["json_schema", "json_object", "plain"], default="json_schema")
    sub.add_argument(
        "--token-parameter", choices=["max_completion_tokens", "max_tokens"], default="max_completion_tokens"
    )


def dispatch(args):
    match args.command:
        case "schema":
            return SCHEMA
        case "raw":
            return raw(args.source, args.seq)
        case "read":
            return read(
                args.source, args.state, strands=args.strand, history=args.history, expand_statements=args.statements
            )
        case "prepare":
            request = prepare(
                args.source, args.state, args.request, batch_size=args.batch_size, max_input_bytes=args.max_input_bytes
            )
            return {"request": args.request, "binding": request["binding"], "source_head": request["source_head"]}
        case "apply":
            return apply(args.source, args.state, args.request, args.proposal)
        case "run":
            options = ProviderOptions(**{key: getattr(args, key) for key in ProviderOptions.__dataclass_fields__})
            return run(
                args.source,
                args.state,
                args.request,
                args.proposal,
                options,
                batch_size=args.batch_size,
                max_input_bytes=args.max_input_bytes,
            )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (GraphError, OSError) as exc:
        message = str(exc) if isinstance(exc, GraphError) else "artifact filesystem operation failed"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
