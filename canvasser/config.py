"""Configuration and secret loading.

Secrets, the saved session, and the browser profile live in one per-user state
directory and **never in the working directory** -- keeping them physically
outside any repo means they cannot be committed by accident.

Where that directory is depends on the machine (`state_dir`):

    $CANVASSER_HOME                        explicit, wins outright
    %LOCALAPPDATA%\\canvasser               Windows
    ~/Library/Application Support/...      macOS
    $XDG_STATE_HOME/canvasser              otherwise (~/.local/state/canvasser)

The platform directory is the default; `$CANVASSER_HOME` is the only thing that
changes it. Notably that includes the sandbox this was developed in, which
keeps its state on a durable mount -- it sets the variable like anywhere else
would, rather than being special-cased here.

The secrets file is the *default* credential source, not the only one -- see
`credentials.py` for the full precedence chain (flag > --secrets-file > env >
file > prompt).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .credentials import (
    ConfigError,
    PASSWORD_VAR,
    USERNAME_VAR,
    parse_env_file,
    resolve_credentials,
    warn_if_world_readable,
)

__all__ = [
    "CANVAS_BASE_URL",
    "Config",
    "ConfigError",
    "ENV_FILE",
    "GATORLINK_SSO_URL",
    "HOME_VAR",
    "IDP_HOST",
    "PROFILE_DIR",
    "SESSION_STATE_FILE",
    "STATE_DIR",
    "load_config",
    "state_dir",
]

#: The one override. Everything else is the platform's own answer.
HOME_VAR = "CANVASSER_HOME"


def state_dir() -> Path:
    """Where the session, browser profile, and secrets file live.

    Everything under here is credential-equivalent -- the saved cookies grant
    Canvas access with no password -- so it is never the working directory and
    never the installed package. It is per-user state, and it goes where the
    platform puts per-user state.

    **The platform directory is the default, always.** An earlier version
    preferred `~/vault/rw/secrets` whenever that path happened to exist, which
    made the location depend on an unrelated directory being present -- fine in
    the sandbox this was built in, surprising anywhere else. A machine that
    wants a different location says so with `$CANVASSER_HOME`, which is
    explicit and needs no rule to explain it.
    """
    override = os.environ.get(HOME_VAR)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "canvasser"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "canvasser"
    # XDG. `state` rather than `config` or `cache`: this is data the program
    # writes and needs back, and losing it costs a re-login.
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "canvasser"


STATE_DIR = state_dir()
ENV_FILE = STATE_DIR / "canvas.env"

#: Browser profile lives beside the secrets, for the same durability reason. It
#: holds live session cookies, so it is exactly as sensitive as the password.
PROFILE_DIR = STATE_DIR / "browser-profile"

#: Cookies saved between runs. Canvas and Shibboleth both issue *non-persistent*
#: session cookies, which Chromium discards on close, so the persistent profile
#: alone loses the login every time. This file is credential-equivalent: it grants
#: Canvas access with no password. Mode 600, alongside the state, never in the
#: working directory.
SESSION_STATE_FILE = STATE_DIR / "storage_state.json"

CANVAS_BASE_URL = "https://ufl.instructure.com"
GATORLINK_SSO_URL = f"{CANVAS_BASE_URL}/login/saml/355"
IDP_HOST = "login.ufl.edu"


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    base_url: str = CANVAS_BASE_URL
    profile_dir: Path = PROFILE_DIR
    #: Human-readable provenance, e.g. "username from $GATORLINK_USERNAME,
    #: password from prompt". Never contains the values themselves.
    credential_sources: str = ""

    @property
    def sso_url(self) -> str:
        return GATORLINK_SSO_URL


def load_config(
    env_file: Path = ENV_FILE,
    *,
    username: str | None = None,
    secrets_file: Path | None = None,
    allow_prompt: bool = True,
) -> Config:
    """Resolve credentials from every supported source, by precedence.

    Defaults preserve the original behavior (vault file, env vars win over it),
    so callers that pass nothing keep working.
    """
    resolved_user, resolved_pass = resolve_credentials(
        username=username,
        secrets_file=secrets_file,
        default_file=env_file,
        allow_prompt=allow_prompt,
    )

    # base_url is not a secret, so its resolution stays simple: explicit
    # secrets file, then environment, then the vault file, then the default.
    file_values = parse_env_file(secrets_file) if secrets_file else {}
    base_url = (
        file_values.get("CANVAS_BASE_URL")
        or os.environ.get("CANVAS_BASE_URL")
        or parse_env_file(env_file).get("CANVAS_BASE_URL")
        or CANVAS_BASE_URL
    )

    return Config(
        username=resolved_user.value,
        password=resolved_pass.value,
        base_url=base_url,
        credential_sources=(
            f"username from {resolved_user.source}, "
            f"password from {resolved_pass.source}"
        ),
    )
