"""Request, response and session values shared by the browser seat's layers."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import tzinfo


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)


class _Refusal(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


@dataclass
class _Request:
    method: str
    path: str
    query: str
    cookies: dict[str, str]
    form: dict[str, str]


@dataclass
class _Session:
    raw: str
    digest: str
    role: str
    zone: tzinfo

    @property
    def csrf(self) -> str:
        """Derived from the session id, so it is stored nowhere and differs for every session."""
        return hmac.new(self.raw.encode(), b"baton seat csrf", hashlib.sha256).hexdigest()
