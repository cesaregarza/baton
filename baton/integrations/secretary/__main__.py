"""Install artifacts, supervise a worker, or handle one product callback."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from baton.errors import BatonError

from .hooks import handle, receive
from .install import install, merge_hooks
from .instance import Instance
from .worker import Worker


def apply_hooks(instance: Instance) -> dict:
    """Merge only this instance's project hooks, preserving the original bytes in a backup."""
    relative = ".codex/hooks.json" if instance.product == "codex" else ".claude/settings.local.json"
    target = instance.project / relative
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("refusing to replace a symlinked project hook configuration")
    raw = target.read_bytes() if target.exists() else b"{}"
    existing = json.loads(raw)
    fragment = json.loads(instance.path.with_name("hooks.json").read_text())
    if not isinstance(existing, dict):
        raise ValueError("project hook configuration must be a JSON object")
    merged = merge_hooks(existing, fragment)
    if merged == existing:
        return {"hooks": str(target), "changed": False}
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = target.with_name(f"{target.name}.baton-secretary-backup-{time.time_ns()}")
        with backup.open("xb") as output:
            output.write(raw)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".secretary-hooks-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(merged, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"hooks": str(target), "changed": True, "backup": str(backup) if backup else None}


async def dispatch(instance: Instance, args) -> int:
    match args.action:
        case "run":
            return await _run_worker(instance)
        case "hook":
            return await _handle_hook(instance)
        case "receive":
            result = await receive(instance)
        case "acknowledge":
            result = {"since": await instance.secretary.acknowledge(args.decision_id)}
        case "bind":
            instance.receipts.bind(args.session, replace=True)
            result = {"session": args.session}
        case "apply-hooks":
            result = apply_hooks(instance)
        case "status":
            result = {key: instance.receipts.get(key) for key in ("session", "pending", "queued", "health")}
            health = result["health"]
            result["heartbeat_stale"] = not health or time.time() - health["checked_at"] > max(
                30, 2 * instance.budget_seconds + instance.poll_seconds
            )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


async def _run_worker(instance: Instance) -> int:
    """Keep the worker alive until the supervisor asks it to stop."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)
    try:
        await Worker(instance).run()
    except asyncio.CancelledError:
        pass
    return 0


async def _handle_hook(instance: Instance) -> int:
    """Read one bounded host event and emit the hook's context and exit status."""
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError("hook input exceeds 64 KiB")
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("hook input must be a JSON object")
    output, stderr, code = await handle(instance, event)
    if stderr:
        print(stderr, file=sys.stderr, flush=True)
    if output:
        print(json.dumps(output, ensure_ascii=False), flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path, help="private integration.json")
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("install", help="generate instance artifacts; does not start a service")
    setup.add_argument("--product", choices=("codex", "claude-code"), required=True)
    for name in ("destination", "project", "config", "policy"):
        setup.add_argument(f"--{name}", type=Path, required=True)
    setup.add_argument("--codex", type=Path)
    setup.add_argument("--apply-hooks", action="store_true")
    for name in ("run", "hook", "status", "receive", "apply-hooks"):
        actions.add_parser(name)
    actions.add_parser("acknowledge").add_argument("decision_id")
    actions.add_parser("bind").add_argument("session")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        if args.action == "install":
            result = install(
                destination=args.destination,
                project=args.project,
                config=args.config,
                policy=args.policy,
                product=args.product,
                python=Path(sys.executable),
                codex=args.codex,
            )
            instance = Instance.load(result["instance"])
            if args.apply_hooks:
                result["applied"] = apply_hooks(instance)
            print(json.dumps(result, default=str), flush=True)
            return 0
        if args.instance is None:
            parser.error("--instance is required for runtime commands")
        return asyncio.run(dispatch(Instance.load(args.instance), args))
    except (BatonError, ValueError, OSError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
