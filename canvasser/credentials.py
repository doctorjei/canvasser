"""Canvasser's credential vocabulary over multipass resolution.

The precedence chain (flag > --secrets-file > env > file > prompt), the
per-field independence, the sshpass discipline, and the no-password-flag rule
all live in `multipass.credentials` now -- this module holds what is
canvasser's own: the variable spellings (including the `GATORLINK_*` legacy
names an existing `canvas.env` still carries) and the single `NAMES` object
every resolution call passes in. There is nothing to configure first; the
branding arrives with each call.
"""

from __future__ import annotations

from multipass.credentials import (
    ConfigError,
    CredentialNames,
    Resolved,
    _read_from_tty,
    parse_env_file,
    resolve_credentials,
    tty_available,
    warn_if_world_readable,
)

__all__ = [
    "LEGACY_PASSWORD_VAR",
    "LEGACY_USERNAME_VAR",
    "NAMES",
    "PASSWORD_VAR",
    "PASSWORD_VARS",
    "USERNAME_VAR",
    "USERNAME_VARS",
    "ConfigError",
    "Resolved",
    "_read_from_tty",
    "parse_env_file",
    "resolve_credentials",
    "tty_available",
    "warn_if_world_readable",
]

#: **Institution-neutral names**, matching `CANVAS_BASE_URL`, which was never
#: UF-branded. The old spellings were: this tool started at UF and named its
#: credentials after UF's identity system.
USERNAME_VAR = "CANVAS_USERNAME"
PASSWORD_VAR = "CANVAS_PASSWORD"

#: **Still honoured, and that is the whole point of renaming this way.** A
#: user's existing `canvas.env` holds the old names, and a rename that silently
#: stopped reading it would look exactly like a lost password -- a prompt, or a
#: refusal on a machine with no terminal, on a tool that had been working.
#: Checked *after* the new name within each source, so a file carrying both is
#: not ambiguous. Reported as deprecated by `-v` rather than warned about on
#: every run: nothing is broken, and a warning nobody can act on mid-script is
#: noise.
LEGACY_USERNAME_VAR = "GATORLINK_USERNAME"
LEGACY_PASSWORD_VAR = "GATORLINK_PASSWORD"

#: New name first. Each source is asked for every spelling before the next
#: source is tried, so source precedence still decides and the spellings are
#: genuinely aliases.
USERNAME_VARS = (USERNAME_VAR, LEGACY_USERNAME_VAR)
PASSWORD_VARS = (PASSWORD_VAR, LEGACY_PASSWORD_VAR)

#: The one object every `resolve_credentials` call in this package passes as
#: `names=`. Constructed once, here, so a third spelling would have one place
#: to land rather than one per call site.
NAMES = CredentialNames(
    username_vars=USERNAME_VARS,
    password_vars=PASSWORD_VARS,
    program="canvasser",
)
