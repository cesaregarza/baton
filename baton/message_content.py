"""Pure message content and retry identity validation."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from baton.errors import InvalidRequest, RequestTooOld

# Text is measured as it travels: JSON with every non-ASCII character escaped.
MAX_MESSAGE_BYTES = 64 * 1024
REQUEST_ID = re.compile(r"(\d{8}T\d{6}Z)-[A-Za-z0-9._:-]{1,100}")
REQUEST_MAX_AGE = timedelta(days=30)
REQUEST_MAX_SKEW = timedelta(minutes=5)
RELAYED_FIELDS = ("source", "at", "text")
AUTHOR_FIELDS = ("from_role", "author_kind", "author", "sender", "from", "role", "on_behalf_of",
                 "alias", "alias_id", "identity", "code")
MAX_SOURCE = 200


def new_request_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


def check(text: str, relayed: Any) -> list[dict[str, str]] | None:
    """Refuse an oversized message or malformed relayed entry; return entries as stored."""
    relayed = check_relayed(relayed)
    if wire_bytes(text) + (len(json.dumps(relayed)) if relayed else 0) > MAX_MESSAGE_BYTES:
        raise InvalidRequest(f"a message, with any relayed entries, is at most {MAX_MESSAGE_BYTES} bytes "
                             "once JSON-escaped")
    return relayed


def check_relayed(relayed: Any) -> list[dict[str, str]] | None:
    """Validate attributed words passed on from someone else."""
    if relayed is None:
        return None
    if not isinstance(relayed, list) or not relayed:
        raise InvalidRequest("relayed must be a non-empty list of {source, at, text} entries, or left out")
    for index, entry in enumerate(relayed):
        where = f"relayed[{index}]"
        if not isinstance(entry, dict):
            raise InvalidRequest(f"{where} must be an object with source, at and text")
        for key in entry:
            if key not in RELAYED_FIELDS:
                raise InvalidRequest(f"{where}.{key} is not a field; an entry has only source, at and text")
        if not isinstance(entry.get("source"), str) or not 1 <= len(entry["source"]) <= MAX_SOURCE:
            raise InvalidRequest(f"{where}.source must name who said it, in 1-{MAX_SOURCE} characters")
        if not isinstance(entry.get("at"), str) or not _zoned_time(entry["at"]):
            raise InvalidRequest(f"{where}.at must be an ISO 8601 time with its UTC offset, such as "
                                 "2026-10-29T07:00:00Z")
        if not isinstance(entry.get("text"), str) or not entry["text"]:
            raise InvalidRequest(f"{where}.text must be the relayed words")
    return [{key: entry[key] for key in RELAYED_FIELDS} for entry in relayed]


def wire_bytes(text: str) -> int:
    return len(json.dumps(text)) - 2


def _zoned_time(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def request_time(request_id: str) -> datetime:
    """Read the UTC creation time in a retry id and reject malformed, future or old ids."""
    match = REQUEST_ID.fullmatch(request_id)
    if match is None:
        raise InvalidRequest("a request_id is YYYYMMDDTHHMMSSZ-<suffix>: its UTC creation time, then up "
                             "to 100 of [A-Za-z0-9._:-]")
    try:
        created_at = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise InvalidRequest(f"request_id {request_id} does not carry a real UTC time") from None
    current = datetime.now(UTC)
    if created_at < current - REQUEST_MAX_AGE:
        raise RequestTooOld(f"request_id {request_id} is more than {REQUEST_MAX_AGE.days} days old")
    if created_at > current + REQUEST_MAX_SKEW:
        raise InvalidRequest(f"request_id {request_id} is dated in the future")
    return created_at
