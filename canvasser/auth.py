"""SSO login and session liveness.

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
"""

from __future__ import annotations

import sys

from playwright.sync_api import (Error as PlaywrightError, Page,
                                 TimeoutError as PlaywrightTimeout)

from .browser import save_debug_snapshot
from .config import Config, canvas_host
from .credentials import PASSWORD_VAR, USERNAME_VAR
from .duo import Approver, PushApprover, complete_duo, on_duo_page

#: UF's unauthenticated landing site -- a WordPress help site, NOT the IdP.
#: No longer part of any decision here: liveness is an allowlist, and the
#: credential check is structural. Kept because "never point tooling at
#: elearning.ufl.edu" is a standing rule and the name is what makes it
#: findable. Confirmed still true 2026-09-09: UF's own `/login` redirects here.
ELEARNING_HOST = "elearning.ufl.edu"

USERNAME_FIELD = "input[name='j_username']"
PASSWORD_FIELD = "input[name='j_password']"
SUBMIT_BUTTON = "button[name='_eventId_proceed']"


class LoginError(RuntimeError):
    """Raised when authentication cannot be completed."""


def _log(message: str) -> None:
    print(f"  {message}", file=sys.stderr, flush=True)


def _settle(page: Page, timeout_ms: int = 15_000) -> None:
    """Let the page finish loading, without demanding network silence.

    `networkidle` is the wrong tool anywhere Duo may be involved: Duo holds a
    long-poll connection open while it waits for the user to tap approve, so the
    network never goes idle and the wait times out *mid-login* -- killing the
    flow during the exact window the human is being asked to act. Learned the
    hard way on the first live run.

    A timeout here is not fatal; the caller's own URL/selector checks decide
    whether we actually got where we meant to go.
    """
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


def quiesce(page: Page, timeout_ms: int = 15_000) -> None:
    """Wait for network idle where it genuinely helps, but never block on it.

    The IdP page does a client-side navigation shortly after load, so touching
    the DOM too early destroys the evaluation context. Waiting for idle avoids
    that -- but idle is not guaranteed to arrive, so a timeout is tolerated
    rather than raised. See `_settle` for why this matters on Duo.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


def is_logged_in(page: Page, config: Config) -> bool:
    """Return whether the persistent session still has us authenticated."""
    page.goto(config.base_url, wait_until="domcontentloaded")
    quiesce(page)
    return _looks_authenticated(page.url, config)


def _looks_authenticated(url: str, config: Config) -> bool:
    """Whether this URL means we are logged in to THIS Canvas.

    **An allowlist of one host**, not a denylist of the places we might have
    been sent. Compared on the parsed hostname rather than by substring: a
    substring test would call `https://evil.example/?ufl.instructure.com`
    authenticated, and would also match a host that merely ends with the right
    letters.
    """
    return canvas_host(url) == config.host


def _submit_credentials(page: Page, config: Config) -> None:
    """Fill and submit the Shibboleth form.

    The form action carries a stateful `execution=e1sN` token that increments per
    step, so we always submit the form as rendered rather than posting anywhere
    we constructed ourselves.
    """
    try:
        page.wait_for_selector(USERNAME_FIELD, timeout=30_000)
    except PlaywrightTimeout as exc:
        snapshot = save_debug_snapshot(page, "login-form-missing")
        raise LoginError(
            f"The SSO login form never appeared (url={page.url}). Snapshot: {snapshot}"
        ) from exc

    page.fill(USERNAME_FIELD, config.username)
    page.fill(PASSWORD_FIELD, config.password)
    page.click(SUBMIT_BUTTON)
    page.wait_for_load_state("domcontentloaded")


#: True once the submitted credential form is no longer on the page. That is
#: the STRUCTURAL signal that primary authentication was accepted: Shibboleth
#: re-renders its own form when it rejects a password, and navigates away when
#: it does not.
FORM_GONE = f"() => !document.querySelector({PASSWORD_FIELD!r})"

#: Words an IdP uses when it turns credentials down. **These no longer decide
#: anything** -- they are used to quote the page back to the user. Deciding by
#: wording meant only UF's phrasing was ever recognised.
REJECTION_WORDS = ("incorrect", "invalid", "failed", "try again", "locked",
                   "unsuccessful", "not recognized", "again")


def _idp_complaint(page: Page) -> str:
    """Whatever the IdP says about the rejection, if it says anything.

    Best-effort and never load-bearing: an IdP that explains itself gets
    quoted, and one that does not still produces a clear error.
    """
    try:
        lines = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                   '.alert, .form-error, [role=alert], .output--error, p, span'))
                 .map(e => (e.innerText || '').trim())
                 .filter(t => t && t.length < 200)""")
    except PlaywrightError:
        return ""
    for line in lines or []:
        low = line.lower()
        if any(word in low for word in REJECTION_WORDS):
            return line
    return ""


def _check_for_credential_rejection(
    page: Page, config: Config, timeout_ms: int = 20_000,
) -> None:
    """Fail fast and clearly on a bad password, rather than timing out later.

    Worth being decisive: repeatedly submitting a wrong password is how
    accounts get locked out, which is far worse than an early error.

    **The signal is structural, not textual** -- the credential form either
    survives the submission or it does not. That works at any Shibboleth IdP,
    including one whose wording nobody here has ever read, which the previous
    version could not: it matched phrases seen on UF's page, so a wrong
    password anywhere else fell through to a vague "we are not authenticated"
    twenty seconds later. The page's own words are still *quoted* when it
    offers any (`_idp_complaint`); they simply no longer decide.

    **Waiting for a state, never a duration.** Checking "is the form still
    there" after a fixed settle races the IdP's navigation -- the same race
    that crashed a live push from the write path's post-save wait. So this
    waits for the form to *go*, and only a timeout means it stayed.
    """
    if _looks_authenticated(page.url, config):
        return  # already through; nothing was rejected
    try:
        page.wait_for_function(FORM_GONE, timeout=timeout_ms)
        return
    except PlaywrightTimeout:
        pass
    except PlaywrightError:
        # The context was destroyed under us, which means the page navigated --
        # and navigating away IS the success signal. Not an error. Same
        # reasoning as `writer._settle_after_save`, which guards its evaluate
        # for exactly this.
        return

    complaint = _idp_complaint(page)
    snapshot = save_debug_snapshot(page, "login-rejected")
    said = f" It says: {complaint!r}." if complaint else ""
    raise LoginError(
        f"The sign-in form at {canvas_host(page.url)} was still on screen "
        f"{timeout_ms // 1000}s after the credentials were submitted, so they "
        f"were not accepted.{said} Check {USERNAME_VAR} / {PASSWORD_VAR} in "
        f"the secrets file. Nothing was retried -- repeated attempts lock "
        f"accounts. Snapshot: {snapshot}"
    )


def log_in(page: Page, config: Config, approver: Approver | None = None) -> None:
    """Perform a full SSO login: credentials, then the Duo second factor."""
    approver = approver or PushApprover()

    _log("Session expired or absent -- authenticating.")
    page.goto(config.sso_url, wait_until="domcontentloaded")
    quiesce(page)

    # A still-valid IdP session can carry us straight through without a form.
    if _looks_authenticated(page.url, config):
        _log("IdP session still valid; no credentials needed.")
        return

    _submit_credentials(page, config)
    _settle(page)
    _check_for_credential_rejection(page, config)

    if on_duo_page(page):
        _log(f"Duo challenge presented; using '{approver.name}' approval.")
        # Snapshot the Duo page while we are actually on it. Its DOM is otherwise
        # unobservable during development (reaching it needs real credentials),
        # and this is the evidence any future selector repair depends on.
        _log(f"Duo page captured: {save_debug_snapshot(page, 'duo-live')}")
        complete_duo(page, approver)
    else:
        # **Not necessarily a remembered device.** At UF, no Duo page means Duo
        # remembered this browser; at Temple it means the IdP asked for no
        # second factor at all (observed on the first live login, 2026-09-09).
        # Reporting the UF reading everywhere would tell a Temple user their
        # device is remembered when nothing is remembering anything.
        _log("No second factor presented (remembered device, or none required "
             "by this institution).")

    _settle(page)

    # Poll rather than sampling once. After Duo the browser is still walking a
    # redirect chain (Duo -> IdP -> SAML POST -> Canvas), so a single check runs
    # mid-chain and reports failure for a login that is merely still in flight.
    # This produced a "not authenticated" error for a session that was, in fact,
    # sitting on the Canvas dashboard.
    if not _wait_until_authenticated(page, config):
        snapshot = save_debug_snapshot(page, "login-incomplete")
        raise LoginError(
            f"Login flow finished but we are not authenticated (url={page.url}). "
            f"Snapshot: {snapshot}"
        )
    _log(f"Authenticated. Landed on {page.url}")


def _wait_until_authenticated(
    page: Page, config: Config, timeout_ms: int = 30_000,
) -> bool:
    """Give the post-Duo redirect chain time to actually finish."""
    waited = 0
    while waited < timeout_ms:
        if _looks_authenticated(page.url, config):
            return True
        page.wait_for_timeout(1_000)
        waited += 1_000
    return False


def ensure_logged_in(page: Page, config: Config, approver: Approver | None = None) -> bool:
    """Guarantee an authenticated session. Returns True if a login was performed.

    Every task entry point should call this first. When the session is warm this
    costs one page load and involves no human at all.
    """
    if is_logged_in(page, config):
        _log("Existing session is still good.")
        return False
    log_in(page, config, approver)
    return True
