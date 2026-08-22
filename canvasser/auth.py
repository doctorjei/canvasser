"""GatorLink SSO login and session liveness.

The liveness check exploits a quirk of this Canvas instance mapped during
reconnaissance: an *unauthenticated* request to the Canvas root redirects out to
elearning.ufl.edu, while an authenticated one stays on ufl.instructure.com and
renders the dashboard. That makes "which host did we end up on" a reliable,
markup-independent signal -- far more durable than probing for some element that
Canvas may restyle next term.
"""

from __future__ import annotations

import sys

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .browser import save_debug_snapshot
from .config import Config, IDP_HOST
from .duo import Approver, PushApprover, complete_duo, on_duo_page

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
    return _looks_authenticated(page.url)


def _looks_authenticated(url: str) -> bool:
    return ELEARNING_HOST not in url and IDP_HOST not in url


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
            f"GatorLink login form never appeared (url={page.url}). Snapshot: {snapshot}"
        ) from exc

    page.fill(USERNAME_FIELD, config.username)
    page.fill(PASSWORD_FIELD, config.password)
    page.click(SUBMIT_BUTTON)
    page.wait_for_load_state("domcontentloaded")


def _check_for_credential_rejection(page: Page) -> None:
    """Fail fast and clearly on a bad password, rather than timing out later.

    Worth being decisive here: repeatedly submitting a wrong password is how
    accounts get locked out, which is a far worse outcome than an early error.
    """
    if IDP_HOST not in page.url:
        return
    body = page.inner_text("body").lower()
    for phrase in ("incorrect", "invalid", "failed", "try again", "locked"):
        if phrase in body and "password" in body:
            snapshot = save_debug_snapshot(page, "login-rejected")
            raise LoginError(
                "GatorLink rejected the credentials. Check GATORLINK_USERNAME / "
                f"GATORLINK_PASSWORD in the secrets file. Snapshot: {snapshot}"
            )


def log_in(page: Page, config: Config, approver: Approver | None = None) -> None:
    """Perform a full SSO login: credentials, then the Duo second factor."""
    approver = approver or PushApprover()

    _log("Session expired or absent -- authenticating.")
    page.goto(config.sso_url, wait_until="domcontentloaded")
    quiesce(page)

    # A still-valid IdP session can carry us straight through without a form.
    if _looks_authenticated(page.url):
        _log("IdP session still valid; no credentials needed.")
        return

    _submit_credentials(page, config)
    _settle(page)
    _check_for_credential_rejection(page)

    if on_duo_page(page):
        _log(f"Duo challenge presented; using '{approver.name}' approval.")
        # Snapshot the Duo page while we are actually on it. Its DOM is otherwise
        # unobservable during development (reaching it needs real credentials),
        # and this is the evidence any future selector repair depends on.
        _log(f"Duo page captured: {save_debug_snapshot(page, 'duo-live')}")
        complete_duo(page, approver)
    else:
        _log("No Duo challenge -- device already remembered.")

    _settle(page)

    # Poll rather than sampling once. After Duo the browser is still walking a
    # redirect chain (Duo -> IdP -> SAML POST -> Canvas), so a single check runs
    # mid-chain and reports failure for a login that is merely still in flight.
    # This produced a "not authenticated" error for a session that was, in fact,
    # sitting on the Canvas dashboard.
    if not _wait_until_authenticated(page):
        snapshot = save_debug_snapshot(page, "login-incomplete")
        raise LoginError(
            f"Login flow finished but we are not authenticated (url={page.url}). "
            f"Snapshot: {snapshot}"
        )
    _log(f"Authenticated. Landed on {page.url}")


def _wait_until_authenticated(page: Page, timeout_ms: int = 30_000) -> bool:
    """Give the post-Duo redirect chain time to actually finish."""
    waited = 0
    while waited < timeout_ms:
        if _looks_authenticated(page.url):
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
