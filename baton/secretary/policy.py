"""Secretary policy values, loading, and validation.

The engine, decision gate, judge, and read interface share this policy definition.
Validation order and normalized policy digests are part of its contract."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from baton import secretary_store
from baton.config import Config, ConfigError
from baton.database import Connection

POLICY_VERSION = 1
MAX_POLICY_BYTES = 16 * 1024
DEFAULT_ANSWERS = ("PASS", "FAIL", "BLOCK", "CHANGES REQUIRED", "APPROVED")
DEFAULT_ACKS = ("claimed", "starting", "received", "recorded")
BRIEF_BYTES = (512, 2048, 16 * 1024)  # the least, default and most bytes `decide` may append to a wake
JUDGE_THRESHOLDS = {"owed_digest_below": 0.4, "wake_at": 0.8}  # their defaults; secretary_judge says how they apply


@dataclass(frozen=True)
class Policy:
    principal: str
    secretary_alias_id: str
    answer_words: tuple[str, ...]
    ack_words: tuple[str, ...]
    terminal_job_states: tuple[str, ...]
    job_events_wake: bool
    brief_bytes: int
    judge: dict[str, Any] | None
    raw: dict[str, Any]
    digest: str


def _words(value: Any, field: str, default: tuple[str, ...]) -> tuple[str, ...]:
    words = value if value is not None else list(default)
    if not isinstance(words, list) or not words or len(words) > 32 or not all(
        isinstance(word, str) and 1 <= len(word) <= 40 and word.strip() == word for word in words
    ) or len(set(words)) != len(words):
        raise ConfigError(f"policy.{field} must be 1-32 distinct short words or phrases")
    return tuple(words)


def load_policy(config: Config, baton: Connection, path: Path) -> Policy:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ConfigError(f"cannot read secretary policy: {error}") from error
    if len(data) > MAX_POLICY_BYTES:
        raise ConfigError("secretary policy exceeds 16 KiB")
    try:
        raw = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"invalid secretary policy JSON: {error}") from error
    fields = {"version", "principal", "secretary_alias_id", "answer_words", "ack_words", "terminal_job_states",
              "job_events_wake", "brief_bytes", "judge"}
    if not isinstance(raw, dict) or not {"version", "principal", "secretary_alias_id"} <= raw.keys() \
            or raw.keys() - fields:
        raise ConfigError("secretary policy fields are invalid")
    if type(raw["version"]) is not int or raw["version"] != POLICY_VERSION:
        raise ConfigError("unsupported secretary policy version")
    principal, alias_id = raw["principal"], raw["secretary_alias_id"]
    role = config.roles.get(principal) if isinstance(principal, str) else None
    if role is None or principal == "guest" or role.human or role.display_only:
        raise ConfigError("secretary principal must be an existing non-human, non-display agent role")
    if not isinstance(alias_id, str) or not alias_id or alias_id == principal:
        raise ConfigError("secretary must name an existing, different guest alias ID")
    if baton.execute("SELECT 1 FROM aliases WHERE id=?", (alias_id,)).fetchone() is None:
        raise ConfigError("secretary alias does not exist")
    if baton.execute("SELECT 1 FROM alias_roles WHERE alias_id=?", (alias_id,)).fetchone() is not None:
        raise ConfigError("secretary alias is mapped to a role")
    answers = _words(raw.get("answer_words"), "answer_words", DEFAULT_ANSWERS)
    acks = _words(raw.get("ack_words"), "ack_words", DEFAULT_ACKS)
    terminal = _words(raw.get("terminal_job_states"), "terminal_job_states",
                      tuple(name for name, state in config.jobs.states.items() if state.terminal))
    if any(name not in config.jobs.states or not config.jobs.states[name].terminal for name in terminal):
        raise ConfigError("policy.terminal_job_states must name configured terminal states")
    # Job events never wake the principal on their own unless this key says so (decide).
    job_wakes = raw.get("job_events_wake", False)
    if type(job_wakes) is not bool:
        raise ConfigError("policy.job_events_wake must be true or false")
    least, default, most = BRIEF_BYTES
    brief_bytes = raw.get("brief_bytes", default)
    if type(brief_bytes) is not int or not least <= brief_bytes <= most:
        raise ConfigError(f"policy.brief_bytes must be an integer from {least} to {most}")
    judge = _judge_block(raw.get("judge"))
    normalized = {"version": POLICY_VERSION, "principal": principal, "secretary_alias_id": alias_id,
                  "answer_words": list(answers), "ack_words": list(acks), "terminal_job_states": list(terminal),
                  "job_events_wake": job_wakes, "brief_bytes": brief_bytes, **({"judge": judge} if judge else {})}
    return Policy(principal, alias_id, answers, acks, terminal, job_wakes, brief_bytes, judge, normalized,
                  secretary_store.digest(normalized))


def _judge_block(value: Any) -> dict[str, Any] | None:
    """The optional judge: its command, its timeout, the sha256 of the question file it asks, and the two
    thresholds. All of them are in the policy digest, so a recorded decision names exactly the judge it used."""
    if value is None:
        return None
    fields = {"command", "timeout_seconds", "questions_sha256", *JUDGE_THRESHOLDS}
    if not isinstance(value, dict) or not {"command", "timeout_seconds", "questions_sha256"} <= value.keys() \
            or value.keys() - fields:
        raise ConfigError("policy.judge fields are invalid")
    block = {**JUDGE_THRESHOLDS, **value}
    command = block["command"]
    if not isinstance(command, list) or not 1 <= len(command) <= 16 or not all(
            isinstance(part, str) and 0 < len(part) <= 4096 and "\0" not in part for part in command) \
            or not command[0].startswith("/"):
        raise ConfigError("policy.judge.command must be an argv list that starts with an absolute path")
    if not _finite(block["timeout_seconds"]) or not 0 < block["timeout_seconds"] <= 10:
        raise ConfigError("policy.judge.timeout_seconds must be more than 0 and at most 10")
    if not isinstance(block["questions_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", block["questions_sha256"]):
        raise ConfigError("policy.judge.questions_sha256 must be a lowercase hex sha256")
    if not all(_finite(block[key]) for key in JUDGE_THRESHOLDS) or not (
            0 <= block["owed_digest_below"] <= block["wake_at"] <= 1):
        raise ConfigError("policy.judge needs 0 <= owed_digest_below <= wake_at <= 1")
    return block


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)
