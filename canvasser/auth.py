"""SSO login and session liveness, over the multipass loop.

The liveness check rests on a fact mapped during reconnaissance: an
*unauthenticated* request to the Canvas root is redirected away, while an
authenticated one stays put and renders the dashboard. "Which host did we end
up on" is therefore a reliable, markup-independent signal -- far more durable
than probing for some element Canvas may restyle next term.

**It is an allowlist: authenticated iff we are still on the configured Canvas
host.** It used to be a denylist of the two hosts UF bounces to, which meant
every new institution had to enumerate every host it might land on -- and
recon 2026-09-09 showed those hosts are not even the same *kind* of thing.
UF's unauthenticated root lands on `elearning.ufl.edu`, a WordPress help site;
Temple's lands on `fim.temple.edu`, the IdP itself. One allowlist covers both
with no per-institution configuration, and it is the simpler code.

## Signing in: read the page, do not follow a script

The loop itself -- dispatching page states until authenticated or unanswered --
is `multipass.auth`, with Microsoft Entra ID arriving as the `entra_pack`.
What stays here is what only this package knows: which Canvas is in play (the
allowlist above), the credential spellings, the authenticator-box display, and
the per-run assembly of target plus pack from a `Config`.

`IDPS` holds one row per identity-provider family -- Shibboleth (UF, Temple)
and Microsoft Entra ID (UCF) -- and detection decides which is on screen
from the page's own controls, refusing when two match or none does.

`log_in` is therefore a **recogniser loop**: a set of page states, each with a
detector and an answer, dispatched against whatever is on screen until we are
authenticated or nothing matches.

    a sign-in form                   -> both fields at once (Shibboleth)
    a username                       -> type it            (absent if known)
    which credential to use          -> password, or the app with --passwordless
    a password                       -> type it   (absent if none is wanted)
    which verification method to use -> choose by authMethodId
    an authenticator code            -> prompt the human; never stored
    a Duo challenge                  -> hand to the approver

Those are the names `RECOGNISERS` actually carries, and they are what the log and
any refusal print.
"""

from __future__ import annotations

import sys

from multipass.auth import (
    IDPS as _CORE_IDPS,
    NO_OPERABLE_PASSWORD,
    OPERABLE_PASSWORD,
    RECOGNISERS as _CORE_RECOGNISERS,
    IdP,
    LoginError,
    LoginState,
    LoginTarget,
    Recogniser,
    _check_for_credential_rejection,
    _drive_login,
    _match,
    _submit_single_screen,
    detect_idp,
    ensure_logged_in as _mp_ensure_logged_in,
    quiesce,
)
from multipass.auth import NO_PASSKEY, MAX_LOGIN_STEPS
from multipass.entra import (
    ENTRA,
    ENTRA_FACTOR_FOR,
    ENTRA_OTP,
    ENTRA_OTP_FIELD,
    ENTRA_OTP_SUBMIT,
    ENTRA_PUSH,
    EntraPack,
    _choose_entra_factor,
    _looks_like_a_rejected_username,
    _read_entra_number,
    _report_pending_factor as _entra_report_pending_factor,
    entra_pack,
)
from playwright.sync_api import Page

from .config import Config, canvas_host, state_dir
from .credentials import NAMES
from .duo import Approver
from .progress import code_box, glyphs_for, print_box

__all__ = [
    "ENTRA",
    "ENTRA_FACTOR_FOR",
    "ENTRA_OTP",
    "ENTRA_OTP_FIELD",
    "ENTRA_OTP_SUBMIT",
    "ENTRA_PUSH",
    "ENTRA_RECOGNISERS",
    "IDPS",
    "MAX_LOGIN_STEPS",
    "NO_OPERABLE_PASSWORD",
    "NO_PASSKEY",
    "OPERABLE_PASSWORD",
    "PASSWORD_FIELD",
    "RECOGNISERS",
    "SHIBBOLETH",
    "SUBMIT_BUTTON",
    "USERNAME_FIELD",
    "Approver",
    "EntraPack",
    "IdP",
    "LoginError",
    "LoginState",
    "LoginTarget",
    "Recogniser",
    "_answer_credential_picker",
    "_answer_password_box",
    "_answer_username_box",
    "_check_for_credential_rejection",
    "_choose_entra_factor",
    "_drive_login",
    "_looks_like_a_rejected_username",
    "_match",
    "_read_entra_number",
    "_report_pending_factor",
    "_sees_credential_picker",
    "_sees_method_picker",
    "_sees_password_box",
    "_sees_username_box",
    "_submit_single_screen",
    "detect_idp",
    "entra_pack",
    "ensure_logged_in",
    "is_logged_in",
    "quiesce",
]


def _looks_authenticated(url: str, config: Config) -> bool:
    """Whether this URL means we are logged in to THIS Canvas.

    **An allowlist of one host**, not a denylist of the places we might have
    been sent. Compared on the parsed hostname rather than by substring: a
    substring test would call `https://evil.example/?ufl.instructure.com`
    authenticated, and would also match a host that merely ends with the right
    letters.
    """
    return canvas_host(url) == config.host


def is_logged_in(page: Page, config: Config) -> bool:
    """Return whether the persistent session still has us authenticated."""
    page.goto(config.base_url, wait_until="domcontentloaded")
    quiesce(page)
    return _looks_authenticated(page.url, config)


def _target_for(config: Config) -> LoginTarget:
    """Where to start logging in, and what counts as logged in, for one run.

    The SSO entry URL and the allowlist host come from THIS config's own
    institution -- never a module constant -- so a run aimed at one Canvas
    cannot authenticate at another. Auth snapshots land beside the state
    directory's other snapshots, exactly where they always have.
    """
    return LoginTarget(
        sso_url=config.sso_url,
        home_host=config.host,
        snapshot_dir=state_dir() / "snapshots",
        names=NAMES,
    )


def _show_authenticator_box(number: str | None) -> None:
    """Draw the number-match box. The box half of the pending-factor report;
    the page's own text follows, printed by the pack."""
    print_box(
        code_box(
            number,
            heading="Microsoft Authenticator: Approve",
            instruction="Approve in Authenticator",
            glyphs=glyphs_for(sys.stderr),
        ),
        sys.stderr,
    )


def _report_pending_factor(page: Page) -> None:
    """Print what the IdP is showing, when it is showing something we cannot drive.

    The number-match code exists only on the headless screen, so without this
    the run is unapprovable even with the person sitting right there. The box
    is drawn whether or not a number was found; the page's own text follows.
    """
    _entra_report_pending_factor(page, _show_authenticator_box)


def _pack_for(config: Config) -> EntraPack:
    """The Entra pack for one run's choices.

    `passwordless` makes the authenticator app the primary credential, so no
    password box is ever answered; the number box draws in this package's own
    style. Built per run because both answers can differ between runs.
    """
    return entra_pack(passwordless=config.passwordless,
                      show_number=_show_authenticator_box)


def ensure_logged_in(page: Page, config: Config,
                     approver: Approver | None = None) -> bool:
    """Guarantee an authenticated session. Returns True if a login was performed.

    Every task entry point should call this first. When the session is warm this
    costs one page load and involves no human at all.
    """
    if is_logged_in(page, config):
        return False
    target = _target_for(config)
    pack = _pack_for(config)
    _mp_ensure_logged_in(page, target, config.username, config.password,
                         approver, idps=IDPS,
                         extra_recognisers=pack.recognisers,
                         on_refuse=pack.on_refuse)
    return True


#: Every IdP family this build can drive: the core table plus Entra.
#: **Order is not significance** -- detection refuses unless exactly one row
#: matches, so adding a row cannot silently re-route an existing institution.
IDPS: tuple[IdP, ...] = _CORE_IDPS + (ENTRA,)

#: Every page state this build can answer: the core states plus the pack's,
#: built with this package's own display. The names below are what the log and
#: any refusal print.
_DEFAULT_PACK = entra_pack(show_number=_show_authenticator_box)
RECOGNISERS: tuple[Recogniser, ...] = (
    _CORE_RECOGNISERS + _DEFAULT_PACK.recognisers
)

#: The pack rows on their own, for callers that drive the loop directly
#: (`_drive_login(state, ENTRA_RECOGNISERS)`). Same rows as above, not copies.
ENTRA_RECOGNISERS: tuple[Recogniser, ...] = _DEFAULT_PACK.recognisers

_BY_NAME = {r.name: r for r in RECOGNISERS}

#: The pack's detectors and answers, picked out under their own names so callers
#: (and the offline suite) name states rather than positions. One definition of
#: each -- these ARE the rows above, not copies.
_sees_username_box = _BY_NAME["a username"].detect
_answer_username_box = _BY_NAME["a username"].handle
_sees_password_box = _BY_NAME["a password"].detect
_answer_password_box = _BY_NAME["a password"].handle
_sees_credential_picker = _BY_NAME["which credential to use"].detect
_answer_credential_picker = _BY_NAME["which credential to use"].handle
_sees_method_picker = _BY_NAME["which verification method to use"].detect
_answer_method_picker = _BY_NAME["which verification method to use"].handle

#: The Shibboleth row's selectors, kept under their original names because they
#: are what error messages and the existing checks refer to.
SHIBBOLETH = IDPS[0]
USERNAME_FIELD = SHIBBOLETH.username_field
PASSWORD_FIELD = SHIBBOLETH.password_field
SUBMIT_BUTTON = SHIBBOLETH.submit
