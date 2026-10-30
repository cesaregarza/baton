"""Helpers for the browser seat's tests: a host with a human seat role, a display-only viewer, and a login.

The seat is driven through `baton.seat.respond(config, method, target, headers, body)`, a pure function like
the dashboard's `respond`. `headers` is a list of (name, value) pairs, so a test can send a header twice.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from baton import config as config_module
from baton.auth import new_token
from baton.config import Config

SEAT_HOST = "seat.example.test"
SEAT_ORIGIN = f"https://{SEAT_HOST}"
SESSION_COOKIE = "__Host-baton_seat"
PRE_COOKIE = "__Host-baton_seat_pre"
FORM = "application/x-www-form-urlencoded"


@dataclass
class SeatHost:
    path: Path
    config: Config
    seat_token: str
    seat_digest: str
    agent_token: str

    def reload(self, text: str | None = None) -> Config:
        """Rewrite the config (or keep it) and load it again, as a restart would."""
        if text is not None:
            self.path.write_text(text)
        self.config = config_module.load(self.path)
        return self.config


def seat_config_text(tmp_path: Path, seat_digest: str, agent_digest: str, *, seat_token_name: str = "operator-phone",
                     seat_token_role: str = "operator", operator_extra: str = "human = true\n",
                     viewer_extra: str = "display_only = true\n", seat_extra: str = "",
                     seat_role: str = "operator", include_seat_token: bool = True,
                     seat_origin: str = SEAT_ORIGIN, max_per_hour: int = 1000) -> str:
    return textwrap.dedent(f"""
        [host]
        public_url = "http://127.0.0.1:8765"
        data_dir = "{tmp_path / 'data'}"

        [roles.orchestrator]
        can_create_conversations = true
        can_manage_participants = true
        [roles.reviewer]
        [roles.operator]
        display_name = "Alex Rivera"
    """) + operator_extra + textwrap.dedent("""
        [roles.viewer]
        can_view_all_conversations = true
    """) + viewer_extra + textwrap.dedent(f"""
        [conversations]
        enabled = true
        max_messages_per_hour = {max_per_hour}

        [dashboard]
        enabled = true
        viewer_role = "viewer"

        [seat]
        enabled = true
        role = "{seat_role}"
        origin = "{seat_origin}"
    """) + seat_extra + (textwrap.dedent(f"""
        [[auth.tokens]]
        name = "{seat_token_name}"
        role = "{seat_token_role}"
        sha256 = "{seat_digest}"
    """) if include_seat_token else "") + textwrap.dedent(f"""
        [[auth.tokens]]
        name = "orchestrator-token"
        role = "orchestrator"
        sha256 = "{agent_digest}"
    """)


def make_seat_host(tmp_path: Path, **overrides) -> SeatHost:
    seat_token, seat_digest = new_token()
    agent_token, agent_digest = new_token()
    path = tmp_path / "baton.toml"
    path.write_text(seat_config_text(tmp_path, seat_digest, agent_digest, **overrides))
    return SeatHost(path, config_module.load(path), seat_token, seat_digest, agent_token)


def cookies_set(response) -> dict[str, str]:
    """The cookies a response sets, by name, with their attribute strings."""
    found = {}
    for value in response.cookies:
        key, _, rest = value.partition("=")
        found[key] = rest
    return found


def cookie_value(response, name: str) -> str | None:
    raw = cookies_set(response).get(name)
    return None if raw is None else raw.split(";", 1)[0]


def hidden(body: bytes, field: str) -> str:
    match = re.search(rf'name="{field}" value="([^"]*)"', body.decode())
    assert match, f"no hidden {field} field"
    return match.group(1)


def post(config: Config, target: str, fields: list[tuple[str, str]] | dict[str, str], *,
         cookies: dict[str, str] | None = None, origin: str | None = SEAT_ORIGIN,
         extra: list[tuple[str, str]] = (), content_type: str = FORM):
    from baton import seat

    body = urlencode(list(fields.items()) if isinstance(fields, dict) else fields).encode()
    headers = [("Host", SEAT_HOST), ("Content-Type", content_type), ("Content-Length", str(len(body)))]
    if origin is not None:
        headers.append(("Origin", origin))
    if cookies:
        headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
    return seat.respond(config, "POST", target, [*headers, *extra], body)


def get(config: Config, target: str, *, cookies: dict[str, str] | None = None):
    from baton import seat

    headers = [("Host", SEAT_HOST)]
    if cookies:
        headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
    return seat.respond(config, "GET", target, headers, b"")


def log_in(config: Config, token: str):
    """GET the login form, then POST the token with its pre-login value. Returns the POST's response and cookies."""
    form = get(config, "/seat/login")
    pre = cookie_value(form, PRE_COOKIE)
    response = post(config, "/seat/login", {"token": token, "pre": hidden(form.body, "pre")},
                    cookies={PRE_COOKIE: pre} if pre else None)
    session = cookie_value(response, SESSION_COOKIE)
    return response, ({SESSION_COOKIE: session} if session else {})



def conversation(host: SeatHost, participants: tuple[str, ...] = ("orchestrator", "operator"), title: str = "Example"):
    from baton import conversations
    from baton.store import connect

    conn = connect(host.config.db_path)
    try:
        return conversations.create(conn, host.config, actor="orchestrator", title=title,
                                    participants=list(participants))["conversation_id"]
    finally:
        conn.close()


def agent_says(host: SeatHost, conversation_id: str, text: str, sender: str = "orchestrator", **kw):
    from baton import conversations
    from baton.store import connect

    conn = connect(host.config.db_path)
    try:
        return conversations.send(conn, host.config, sender=sender, conversation_id=conversation_id, text=text, **kw)
    finally:
        conn.close()


def mailbox(host: SeatHost, role: str = "operator") -> tuple[int, int]:
    """(last_seq, read_seq) of a role's mailbox."""
    import sqlite3

    conn = sqlite3.connect(host.config.db_path)
    row = conn.execute("SELECT last_seq, read_seq FROM mailboxes WHERE recipient = ?", (role,)).fetchone()
    return tuple(row) if row else (0, 0)


def stored_by_seat(host: SeatHost) -> list[tuple]:
    import sqlite3

    conn = sqlite3.connect(host.config.db_path)
    return conn.execute("SELECT DISTINCT id, conversation_id, text, author_kind FROM messages"
                        " WHERE sender = 'operator' ORDER BY conversation_seq").fetchall()
