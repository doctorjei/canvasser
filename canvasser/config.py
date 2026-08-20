"""Configuration and secret loading.

Secrets live in ~/vault/rw/secrets/ and never in the workspace. The vault is the
only writable store that survives a full box rebuild, and keeping credentials
physically outside the repo means they cannot be committed by accident.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

VAULT_SECRETS = Path.home() / "vault" / "rw" / "secrets"
ENV_FILE = VAULT_SECRETS / "canvas.env"

#: Browser profile lives beside the secrets, for the same durability reason. It
#: holds live session cookies, so it is exactly as sensitive as the password.
PROFILE_DIR = VAULT_SECRETS / "browser-profile"

#: Cookies saved between runs. Canvas and Shibboleth both issue *non-persistent*
#: session cookies, which Chromium discards on close, so the persistent profile
#: alone loses the login every time. This file is credential-equivalent: it grants
#: Canvas access with no password. Mode 600, in the vault, never in the workspace.
SESSION_STATE_FILE = VAULT_SECRETS / "storage_state.json"

CANVAS_BASE_URL = "https://ufl.instructure.com"
GATORLINK_SSO_URL = f"{CANVAS_BASE_URL}/login/saml/355"
IDP_HOST = "login.ufl.edu"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _warn_if_world_readable(path: Path) -> None:
    """A credentials file readable beyond its owner is worth complaining about."""
    if not path.exists():
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"WARNING: {path} is readable beyond its owner. Run: chmod 600 {path}")


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    base_url: str = CANVAS_BASE_URL
    profile_dir: Path = PROFILE_DIR

    @property
    def sso_url(self) -> str:
        return GATORLINK_SSO_URL


def load_config(env_file: Path = ENV_FILE) -> Config:
    """Read credentials from the environment, falling back to the vault file.

    Real environment variables win, so a one-off run can override without
    editing anything on disk.
    """
    _warn_if_world_readable(env_file)
    file_values = _parse_env_file(env_file)

    def get(key: str) -> str:
        return os.environ.get(key) or file_values.get(key, "")

    username = get("GATORLINK_USERNAME")
    password = get("GATORLINK_PASSWORD")

    missing = [
        name
        for name, value in (("GATORLINK_USERNAME", username), ("GATORLINK_PASSWORD", password))
        if not value
    ]
    if missing:
        raise ConfigError(
            f"Missing {', '.join(missing)}.\n"
            f"Set them in {env_file} (mode 600) or in the environment."
        )

    return Config(
        username=username,
        password=password,
        base_url=get("CANVAS_BASE_URL") or CANVAS_BASE_URL,
    )
