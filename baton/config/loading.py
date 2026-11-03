"""Configuration path selection and TOML file loading.

The selected file is decoded here, then validated by parsing.parse relative to its directory."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import parsing
from .models import Config, ConfigError

CONFIG_ENV = "BATON_CONFIG"
DEFAULT_CONFIG = Path("~/.config/baton/baton.toml")


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    return Path(explicit or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).expanduser()


def load(explicit: str | os.PathLike[str] | None = None) -> Config:
    path = config_path(explicit)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}; see the README quickstart") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    return parsing.parse(raw, base=path.parent)
