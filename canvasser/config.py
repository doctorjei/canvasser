"""Configuration and secret loading.

Secrets live in ~/vault/rw/secrets/ and never in the workspace. The vault is the
only writable store that survives a full box rebuild, and keeping credentials
physically outside the repo means they cannot be committed by accident.

That vault file is the *default* source, not the only one -- see
`credentials.py` for the full precedence chain (flag > --secrets-file > env >
vault > prompt).
"""

from __future__ import annotations

import os
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
    "IDP_HOST",
    "PROFILE_DIR",
    "SESSION_STATE_FILE",
    "load_config",
]

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
