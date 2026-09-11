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
    parse_env_file,
    resolve_credentials,
)

__all__ = [
    "CANVAS_BASE_URL",
    "Config",
    "ConfigError",
    "ENV_FILE",
    "GATORLINK_SSO_URL",
    "HOME_VAR",
    "IDP_HOST",
    "INSTRUCTURE_SUFFIX",
    "PROFILE_DIR",
    "SESSION_STATE_FILE",
    "SSO_PATH_VAR",
    "STATE_DIR",
    "UF_HOST",
    "UF_SSO_PATH",
    "canvas_host",
    "institution_key",
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

#: **Only official Instructure-hosted Canvas is supported** (user's call,
#: 2026-09-09). Self-hosted and vanity-domain Canvas are deliberately out of
#: scope for now: the institution key below is *derived* from the subdomain,
#: and a bespoke host has no subdomain to derive it from. This is a validation
#: rule rather than an assumption -- see `institution_key`.
INSTRUCTURE_SUFFIX = ".instructure.com"

#: The institution this was built against. Named rather than assumed: every
#: UF-specific value below is keyed to this host, so a different Canvas does
#: not silently inherit any of them.
UF_HOST = "ufl.instructure.com"

#: **UF's SAML provider id, not a Canvas convention.** `355` is an
#: account-level identifier; another institution's differs and may not be a
#: `saml` route at all. So it is bound to UF's host and never used as a
#: fallback for anywhere else -- a wrong SSO path sends someone's credentials
#: to the wrong institution's IdP, which is the one mistake here worth
#: refusing outright.
UF_SSO_PATH = "/login/saml/355"

#: UF's IdP. Kept for the credential-rejection check, which reads the page's
#: own words and therefore has to know it is *on* the IdP. Liveness no longer
#: uses it: see `auth._looks_authenticated`.
IDP_HOST = "login.ufl.edu"

CANVAS_BASE_URL = f"https://{UF_HOST}"
GATORLINK_SSO_URL = f"{CANVAS_BASE_URL}{UF_SSO_PATH}"

#: Where a non-default SSO path is configured, resolved like `CANVAS_BASE_URL`.
SSO_PATH_VAR = "CANVAS_SSO_PATH"


def canvas_host(base_url: str) -> str:
    """The bare hostname of a Canvas base URL, lowercased.

    Used for both the liveness allowlist and the institution key, so the two
    cannot disagree about what host a config names.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(base_url if "//" in base_url else f"https://{base_url}")
    return (parsed.hostname or "").lower()


def institution_key(base_url: str) -> str:
    """The instructure subdomain, which is this tool's identity for a Canvas.

    `templeu.instructure.com` -> `templeu`. It is already unique, already in
    the URL, and needs nothing invented (user's design, 2026-09-09).

    **Refused rather than guessed for anything else.** Taking "the first label"
    of an arbitrary host would produce a key from whatever that label happened
    to be -- so two unrelated self-hosted Canvases could collide on one
    directory of saved cookies, which is a credential mix-up rather than a
    naming inconvenience.
    """
    host = canvas_host(base_url)
    if not host.endswith(INSTRUCTURE_SUFFIX) or host == INSTRUCTURE_SUFFIX[1:]:
        raise ConfigError(
            f"{base_url!r} is not an official Instructure-hosted Canvas. This "
            f"build supports *{INSTRUCTURE_SUFFIX} only, because it identifies "
            f"an institution by its subdomain. Self-hosted and vanity-domain "
            f"Canvas are not supported yet."
        )
    return host[: -len(INSTRUCTURE_SUFFIX)]


#: Which institution this run is for, resolved like everything else here:
#: flag > environment > file > default. **Do not invent a fourth resolution
#: pattern** -- credentials and course selection already share this shape.
INSTITUTION_VAR = "CANVAS_INSTITUTION"

#: A line in the ROOT secrets file naming the institution to use when no flag
#: and no environment variable say otherwise. The cheapest place to record a
#: switchable default: it needs no new file, and the root secrets file is
#: already the thing a single-account user has.
DEFAULT_INSTITUTION_SETTING = "default_institution"


@dataclass(frozen=True)
class StatePaths:
    """Where one institution's state lives.

    **The single-account case is the root of the state directory, unchanged.**
    That is the whole point of the default: an existing `canvas.env`,
    `storage_state.json` and browser profile keep working with no migration and
    no re-login. Alternates get a subdirectory holding *the same three things
    under the same names* -- one layout to understand, not two.
    """

    root: Path
    institution: str | None = None

    @property
    def env_file(self) -> Path:
        return self.root / "canvas.env"

    @property
    def profile_dir(self) -> Path:
        return self.root / "browser-profile"

    @property
    def session_state_file(self) -> Path:
        return self.root / "storage_state.json"


def root_institution(env_file: Path | None = None) -> str:
    """Whose state the root of the state directory holds.

    Derived from the root secrets file's own `CANVAS_BASE_URL`, defaulting to
    UF because that is what an existing installation has. **This is what stops
    `--institution ufl` from creating an empty `ufl/` directory beside the real
    state**, and what lets the default be switched to another institution
    without stranding the original one.
    """
    values = parse_env_file(env_file or (state_dir() / "canvas.env"))
    return institution_key(values.get("CANVAS_BASE_URL") or CANVAS_BASE_URL)


def resolve_institution(explicit: str | None = None,
                        env_file: Path | None = None) -> str | None:
    """Which institution this run is for, or None for "whatever the root is"."""
    chosen = (explicit or os.environ.get(INSTITUTION_VAR)
              or parse_env_file(env_file or (state_dir() / "canvas.env")).get(
                  DEFAULT_INSTITUTION_SETTING))
    return chosen.strip().lower() if chosen else None


def paths_for(institution: str | None, root: Path | None = None) -> StatePaths:
    """The state paths for an institution, honouring the single-account case."""
    base = root or state_dir()
    if institution is None or institution == root_institution(base / "canvas.env"):
        return StatePaths(root=base, institution=institution)
    return StatePaths(root=base / institution, institution=institution)


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    base_url: str = CANVAS_BASE_URL
    profile_dir: Path = PROFILE_DIR
    #: Human-readable provenance, e.g. "username from $CANVAS_USERNAME,
    #: password from prompt". Never contains the values themselves.
    credential_sources: str = ""
    #: The SSO entry path for THIS institution. `None` means "not configured",
    #: which is only usable at UF -- see `sso_url`.
    sso_path: str | None = None
    #: Authenticate with the IdP's authenticator app as the PRIMARY credential,
    #: with no password at all. Carried on the config because `auth` needs it
    #: at the credential picker, and because it changes what
    #: `resolve_credentials` is even allowed to demand.
    passwordless: bool = False
    #: Saved cookies for THIS institution. Carried on the config rather than
    #: read from a module constant, because two institutions' sessions must not
    #: land in one file -- logging in to the second would silently destroy the
    #: first, and the symptom would be an unexplained re-login loop.
    session_state_file: Path = SESSION_STATE_FILE

    @property
    def host(self) -> str:
        return canvas_host(self.base_url)

    @property
    def institution(self) -> str:
        """This Canvas's instructure subdomain."""
        return institution_key(self.base_url)

    @property
    def sso_url(self) -> str:
        # Built from THIS config's base_url, never the module constant. Reading
        # the constant meant an overridden CANVAS_BASE_URL produced a config
        # that authenticated at one institution and read from another.
        path = self.sso_path or (UF_SSO_PATH if self.host == UF_HOST else None)
        if path is None:
            raise ConfigError(
                f"No SSO path configured for {self.host}. Set {SSO_PATH_VAR} "
                f"(in the secrets file or the environment) to this "
                f"institution's Canvas login route. There is deliberately no "
                f"default: {UF_SSO_PATH} is UF's own SAML provider id, and "
                f"using it anywhere else would send credentials to UF's IdP."
            )
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


def load_config(
    env_file: Path | None = None,
    *,
    username: str | None = None,
    secrets_file: Path | None = None,
    allow_prompt: bool = True,
    institution: str | None = None,
    passwordless: bool = False,
) -> Config:
    """Resolve credentials from every supported source, by precedence.

    Defaults preserve the original behaviour (the state directory's secrets
    file, with environment variables winning over it), so callers that pass
    nothing keep working.

    **The institution is resolved FIRST, because it decides which secrets file
    "the default one" is.** A named institution's state lives in its own
    subdirectory; the unnamed case is the root, exactly as before.
    """
    chosen = resolve_institution(institution)
    paths = paths_for(chosen)
    # An explicit `env_file` still wins -- it is a caller saying exactly which
    # file to read, and explicit instruction beats a derived location here for
    # the same reason `--out` is never guarded and an exact course id always
    # matches.
    env_file = env_file or paths.env_file

    resolved_user, resolved_pass = resolve_credentials(
        username=username,
        secrets_file=secrets_file,
        default_file=env_file,
        allow_prompt=allow_prompt,
        require_password=not passwordless,
    )

    # Neither of these is a secret, so resolution stays simple: explicit
    # secrets file, then environment, then the state directory's file, then
    # the default. **One helper for both**, so a second setting cannot acquire
    # a subtly different precedence from the first.
    file_values = parse_env_file(secrets_file) if secrets_file else {}
    default_values = parse_env_file(env_file)

    def setting(name: str) -> str | None:
        return (file_values.get(name) or os.environ.get(name)
                or default_values.get(name) or None)

    # **A named institution derives its own base URL**, because the key IS the
    # subdomain -- so an alternate needs no CANVAS_BASE_URL at all. An explicit
    # one still wins, but it must agree: a `templeu` directory whose config
    # points at UF would read one Canvas with the other's saved cookies.
    derived = f"https://{chosen}{INSTRUCTURE_SUFFIX}" if chosen else CANVAS_BASE_URL
    base_url = setting("CANVAS_BASE_URL") or derived
    # Refused here, at the point the value is read, rather than wherever it
    # first fails to work. `institution_key` raises with the reason.
    named = institution_key(base_url)
    if chosen and named != chosen:
        raise ConfigError(
            f"institution {chosen!r} was selected, but {env_file} sets "
            f"CANVAS_BASE_URL to {base_url!r} ({named}). One of them is wrong, "
            f"and guessing which would read one Canvas using another's saved "
            f"session."
        )

    return Config(
        username=resolved_user.value,
        password=resolved_pass.value,
        base_url=base_url,
        profile_dir=paths.profile_dir,
        session_state_file=paths.session_state_file,
        sso_path=setting(SSO_PATH_VAR),
        passwordless=passwordless,
        credential_sources=(
            f"username from {resolved_user.source}, "
            f"password from {resolved_pass.source}"
        ),
    )
