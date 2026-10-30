"""The dashboard's viewer is a display-only role: it can never hold a credential or be read as a person."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from seat_helpers import make_seat_host, seat_config_text

from baton import cli
from baton import config as config_module
from baton.auth import new_token
from baton.config import ConfigError


def _load(tmp_path: Path, **overrides) -> config_module.Config:
    _, seat_digest = new_token()
    _, agent_digest = new_token()
    path = tmp_path / "baton.toml"
    path.write_text(seat_config_text(tmp_path, seat_digest, agent_digest, **overrides))
    return config_module.load(path)


def test_a_display_only_viewer_loads(tmp_path: Path) -> None:
    config = _load(tmp_path)
    assert config.roles["viewer"].display_only and not config.roles["viewer"].human
    assert config.roles["operator"].display_name == "Alex Rivera"


def test_a_viewer_marked_human_without_display_only_does_not_load(tmp_path: Path) -> None:
    """The shape the live host has today: no shim keeps it working, and the error names the fix."""
    with pytest.raises(ConfigError, match="display_only"):
        _load(tmp_path, viewer_extra="human = true\n")


def test_a_viewer_with_neither_flag_does_not_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="display_only"):
        _load(tmp_path, viewer_extra="")


def test_display_only_and_human_together_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="display_only"):
        _load(tmp_path, viewer_extra="display_only = true\nhuman = true\n")


def test_a_token_for_a_display_only_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="display_only"):
        _load(tmp_path, seat_token_name="viewer-token", seat_token_role="viewer")


def test_oauth_may_not_grant_a_display_only_role(tmp_path: Path) -> None:
    from conftest import PASSPHRASE, REDIRECT  # noqa: F401 - the shared synthetic values

    from baton.auth import hash_passphrase

    oauth = (f'[auth.oauth]\nenabled = true\npassphrase_hash = "{hash_passphrase(PASSPHRASE)}"\n'
             f'roles = ["viewer"]\nredirect_uris = ["{REDIRECT}"]\n')
    with pytest.raises(ConfigError, match="display_only"):
        _load(tmp_path, seat_extra="\n" + oauth)


def test_the_seat_role_must_be_human(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="human"):
        _load(tmp_path, operator_extra="")


def test_the_seat_role_may_not_be_the_viewer(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, seat_role="viewer", include_seat_token=False)


def test_the_cli_refuses_to_act_as_a_display_only_role(tmp_path: Path, capsys) -> None:
    host = make_seat_host(tmp_path)
    code = cli.main(["--config", str(host.path), "read", "--as", "viewer"])
    assert code == 2
    assert "display_only" in capsys.readouterr().out


def test_token_new_for_a_display_only_role_generates_no_secret(tmp_path: Path, monkeypatch) -> None:
    host = make_seat_host(tmp_path)

    def never(*_: object) -> tuple[str, str]:
        raise AssertionError("a secret was generated for a display-only role")

    monkeypatch.setattr("baton.auth.new_token", never)
    args = argparse.Namespace(token_role="viewer", name="viewer-phone")
    with pytest.raises(ConfigError, match="display_only"):
        cli._new_token(host.config, args)


@pytest.mark.parametrize("origin", ["https://127.0.0.1:5444", "https://127.0.0.1"])
def test_a_seat_on_the_hosts_own_name_needs_written_trust(tmp_path: Path, origin: str) -> None:
    """Cookies are shared by every port on a host name, so a seat there trusts every service on it."""
    with pytest.raises(ConfigError, match="same_host_trust_accepted"):
        _load(tmp_path, seat_origin=origin)
    config = _load(tmp_path, seat_origin=origin, seat_extra="same_host_trust_accepted = true\n")
    assert config.seat.same_host_trust_accepted


@pytest.mark.parametrize("origin", ["http://seat.example.test", "https://seat.example.test/seat",
                                    "https://seat.example.test?x=1", "seat.example.test"])
def test_the_seat_origin_is_one_exact_https_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ConfigError, match="seat.origin"):
        _load(tmp_path, seat_origin=origin)
