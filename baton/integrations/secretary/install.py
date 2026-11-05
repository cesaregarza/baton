"""Create private, opt-in artifacts for a persistent secretary instance."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from baton import datadir
from baton.agents.secretary.policy import load_policy
from baton.config import load


def install(
    *,
    destination: Path,
    project: Path,
    config: Path,
    policy: Path,
    product: str,
    python: Path,
    codex: Path | None = None,
) -> dict[str, Any]:
    """Write instance, hook-fragment, and user-unit artifacts to a new private directory."""
    if product not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported secretary product: {product}")
    project = _existing(project, "project", directory=True)
    config = _existing(config, "config", directory=False)
    policy = _existing(policy, "policy", directory=False)
    _check_summary_limit(policy)
    load_policy(load(config), policy)
    python = _executable(python, "python")
    codex_path = (_executable(codex, "codex") if codex is not None else _find_codex()) if product == "codex" else None
    destination = datadir.canonical(destination)
    datadir.check(destination)
    if any(destination.iterdir()):
        raise ValueError(f"destination is not empty: {destination}")

    instance_path = destination / "integration.json"
    command = shlex.join((str(python), "-m", "baton.integrations.secretary", "--instance",
                          str(instance_path), "hook"))
    command_hook = {"type": "command", "command": command, "timeout": 10}
    hooks = {"hooks": {event: [{"hooks": [command_hook]}]
                       for event in ("SessionStart", "Stop")}}
    if product == "claude-code":
        hooks["hooks"]["FileChanged"] = [{"hooks": [{**command_hook, "asyncRewake": True}]}]
    digest = hashlib.sha256(str(destination).encode()).hexdigest()[:8]
    unit_name = f"secretary-{digest}.service"
    unit_path = destination / unit_name
    run_args = (str(python), "-m", "baton.integrations.secretary", "--instance", str(instance_path), "run")
    unit = "\n".join((
        "[Unit]", "Description=Baton secretary integration", "", "[Service]",
        f"ExecStart={_systemd_command(run_args)}", f"WorkingDirectory={_systemd_arg(str(project), quote=False)}",
        f"EnvironmentFile=-{_systemd_arg(str(destination / 'environment'), quote=False)}",
        "Restart=on-failure", "RestartSec=5", "UMask=0077", "", "[Install]", "WantedBy=default.target", "",
    ))
    instance = {
        "version": 1,
        "product": product,
        "project": str(project),
        "config": str(config),
        "policy": str(policy),
        "state": str(destination / "attention.sqlite"),
        "poll_seconds": 2,
        "budget_seconds": 30,
    }
    if codex_path is not None:
        instance["codex"] = str(codex_path)
    _write_private(instance_path, json.dumps(instance, indent=2, sort_keys=True) + "\n")
    _write_private(destination / "hooks.json", json.dumps(hooks, indent=2, sort_keys=True) + "\n")
    if product == "claude-code":
        _write_private(destination / "pending.json", "{}\n")
    _write_private(unit_path, unit)
    return {"destination": destination, "instance": instance_path, "hooks": destination / "hooks.json",
            "unit": unit_path, "unit_name": unit_name, "hook_events": tuple(hooks["hooks"]),
            "pending": destination / "pending.json" if product == "claude-code" else None}


def merge_hooks(existing: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    """Return a copied settings object with fragment hooks appended once per event."""
    result = deepcopy(existing)
    additions = fragment.get("hooks")
    if not isinstance(additions, dict):
        raise ValueError("hook fragment must contain a hooks object")
    target = result.setdefault("hooks", {})
    if not isinstance(target, dict):
        raise ValueError("existing hooks setting must be an object")
    for event, entries in additions.items():
        if not isinstance(entries, list):
            raise ValueError(f"hook fragment {event} must be a list")
        current = target.setdefault(event, [])
        if not isinstance(current, list):
            raise ValueError(f"existing hooks.{event} must be a list")
        for entry in entries:
            if entry not in current:
                current.append(deepcopy(entry))
    return result


def _existing(path: Path, label: str, *, directory: bool) -> Path:
    resolved = Path(path).resolve(strict=True)
    if directory and not resolved.is_dir():
        raise ValueError(f"{label} path must be a directory: {path}")
    if not directory and not resolved.is_file():
        raise ValueError(f"{label} path must be a file: {path}")
    return resolved


def _check_summary_limit(path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("secretary policy must be readable JSON") from error
    limit = value.get("summary_bytes") if isinstance(value, dict) else None
    if type(limit) is not int or not 256 <= limit <= 6000:
        raise ValueError("secretary policy summary_bytes must be an integer from 256 through 6000")


def _executable(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    normalized = Path(os.path.abspath(path))
    if not normalized.is_file() or not os.access(normalized, os.X_OK):
        raise ValueError(f"{label} path must name an executable file: {normalized}")
    return normalized


def _find_codex() -> Path:
    found = shutil.which("codex")
    if found is None:
        raise ValueError("codex executable was not found; pass its absolute path")
    return _executable(Path(found), "codex")


def _write_private(path: Path, contents: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(contents)
        output.flush()
        os.fchmod(output.fileno(), 0o600)


def _systemd_arg(argument: str, *, quote: bool = True) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in argument):
        raise ValueError("systemd arguments cannot contain control characters")
    if not quote:
        # Path directives consume the whole value, including literal spaces and quotes.
        if argument != argument.strip():
            raise ValueError("systemd directory paths cannot have leading or trailing whitespace")
        return argument.replace("%", "%%")
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_command(arguments: tuple[str, ...]) -> str:
    return " ".join(_systemd_arg(argument).replace("$", "$$") for argument in arguments)
