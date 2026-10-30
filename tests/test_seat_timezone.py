"""The seat shows times in one configured zone (the pages run no scripts), UTC unless set."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from seat_helpers import agent_says, conversation, get, log_in, make_seat_host

from baton.config import ConfigError

try:
    from baton import seat
except ImportError:  # written before the module: each test then fails on its own instead of the whole run stopping
    seat = None  # type: ignore[assignment]


@pytest.mark.parametrize(("at", "zone", "shown"), [
    ("2026-10-30T01:14:00+00:00", "America/Chicago", "29 Oct, 8:14 pm CDT"),
    ("2026-12-01T18:05:00+00:00", "America/Chicago", "1 Dec, 12:05 pm CST"),
    ("2026-10-30T01:14:00+00:00", "UTC", "30 Oct, 1:14 am UTC"),
])
def test_a_time_reads_in_the_configured_zone(at: str, zone: str, shown: str) -> None:
    assert seat._when(at, ZoneInfo(zone)) == shown


def test_the_zone_is_configured_and_used_on_the_page(tmp_path: Path) -> None:
    host = make_seat_host(tmp_path, seat_extra='timezone = "America/Chicago"\n')
    assert host.config.seat.timezone == "America/Chicago"
    room = conversation(host)
    agent_says(host, room, "hello")
    _, cookies = log_in(host.config, host.seat_token)
    page = get(host.config, f"/seat/c/{room}", cookies=cookies).body.decode()
    assert " CDT</time>" in page or " CST</time>" in page


def test_the_zone_defaults_to_utc(tmp_path: Path) -> None:
    assert make_seat_host(tmp_path).config.seat.timezone == "UTC"


UNKNOWN_ZONES = ("Mars/Olympus", "", "../../zoneinfo/UTC", "America/Chicago ")


@pytest.mark.parametrize("zone", UNKNOWN_ZONES)
def test_an_unknown_zone_is_refused(tmp_path: Path, zone: str) -> None:
    with pytest.raises(ConfigError, match="seat.timezone"):
        make_seat_host(tmp_path, seat_extra=f'timezone = "{zone}"\n')
