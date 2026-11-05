"""Synthetic Baton records for the offline attention tests; the alias comes from an existing schema fixture."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from baton import config as config_module
from baton import conversations
from baton.agents.secretary import store as secretary_store
from baton.config import Config
from baton.database import store

ALIAS_ID = "al_0000000000000001"


@dataclass
class World:
    config: Config
    config_path: Path
    policy_path: Path
    state_path: Path
    baton: sqlite3.Connection

    def __enter__(self) -> World:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.baton.close()


def make_world(tmp_path: Path, *, principal: str = "planner", initial_state: str = "open") -> World:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "baton.toml"
    config_path.write_text(f"""
[host]
public_url = "http://127.0.0.1:8765"
data_dir = "{data_dir}"
[roles.planner]
can_create_conversations = true
can_manage_participants = true
[roles.worker]
[roles.reviewer]
[roles.outsider]
[roles.guest]
can_view_all_conversations = true
[conversations]
enabled = true
escalation_role = "planner"
[jobs]
initial = "{initial_state}"
[jobs.states.open]
waiting_on = "planner"
next = ["working", "done"]
[jobs.states.working]
waiting_on = "worker"
next = ["review", "handoff", "done"]
[jobs.states.handoff]
waiting_on = "reviewer"
next = ["review", "done"]
[jobs.states.review]
waiting_on = "planner"
next = ["done", "working"]
[jobs.states.done]
""")
    config = config_module.load(config_path)
    data_dir.mkdir(mode=0o700)
    raw = sqlite3.connect(config.db_path)
    raw.executescript((Path(__file__).parent / "fixtures" / "schema_v16.sql").read_text())
    raw.close()
    config.db_path.chmod(0o600)
    baton = store.connect(config.db_path)  # migrate only this synthetic test fixture, before any secretary call
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"version": 1, "principal": principal, "secretary_alias_id": ALIAS_ID}))
    return World(config, config_path, policy_path, data_dir / "secretary.db", baton)


def policy(world: World, **changes: object) -> Path:
    value = json.loads(world.policy_path.read_text())
    value.update(changes)
    world.policy_path.write_text(secretary_store.canonical(value))
    return world.policy_path


def conversation(world: World, participants: list[str] | None = None) -> str:
    made = conversations.create(world.baton, world.config, actor="planner", title="Synthetic work",
                                participants=participants or ["planner", "worker", "reviewer"])
    return made["conversation_id"]


def say(world: World, room: str, sender: str, text: str, **kwargs: object) -> dict:
    return conversations.send(world.baton, world.config, sender=sender, conversation_id=room,
                              text=text, **kwargs)


def table_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    """All Baton tables, including delivery positions, with stable order and no sidecar rows."""
    names = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: hashlib.sha256("\n".join(sorted(repr(tuple(row)) for row in conn.execute(
        f'SELECT * FROM "{name}"'))).encode()).hexdigest()
            for name in names}
