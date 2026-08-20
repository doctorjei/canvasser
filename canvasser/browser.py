"""Persistent browser session.

We use a persistent Playwright context rather than a fresh browser per run so
that Canvas's session cookie and Duo's "remember this device" cookie survive
between invocations. Those two cookies are the entire reason a run can proceed
without bothering the user: Duo's remembered-device window is ~10 hours, and
Canvas keeps its own session alongside it.

The profile therefore holds live credentials-equivalent state and lives in the
vault under mode-700, not in the workspace.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import PROFILE_DIR, SESSION_STATE_FILE

#: The bundled headless build advertises "HeadlessChrome", which is both an
#: unnecessary tell and a plausible trigger for bot-detection on the IdP. Present
#: as ordinary desktop Chrome instead.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1440, "height": 900}


@contextlib.contextmanager
def open_context(
    profile_dir: Path = PROFILE_DIR,
    headless: bool = True,
    slow_mo: int = 0,
) -> Iterator[BrowserContext]:
    """Open the persistent browser context, creating the profile if needed."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            slow_mo=slow_mo,
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="America/New_York",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.set_default_timeout(30_000)
        _restore_session_cookies(context)
        try:
            yield context
        finally:
            # Save before close: storage_state() needs a live context.
            _save_session_cookies(context)
            context.close()


def _restore_session_cookies(context: BrowserContext) -> None:
    """Re-inject cookies saved from a previous run.

    A persistent profile is *not* sufficient on its own. Canvas's session cookie
    and the Shibboleth IdP cookie are both non-persistent, so Chromium drops them
    the moment the browser closes -- verified by inspecting the profile's cookie
    store after a successful login: not one ufl.instructure.com cookie survived.
    Duo's remembered-device cookie *is* persistent and does survive.

    Saving and re-injecting the session ourselves is what makes a login last
    beyond a single process.
    """
    if not SESSION_STATE_FILE.is_file():
        return
    try:
        cookies = json.loads(SESSION_STATE_FILE.read_text()).get("cookies", [])
    except (OSError, ValueError):
        return  # A corrupt cache is not worth failing a run over; just re-login.
    if cookies:
        with contextlib.suppress(Exception):
            context.add_cookies(cookies)


def _save_session_cookies(context: BrowserContext) -> None:
    """Persist cookies, including the session cookies Chromium would discard.

    The file is credential-equivalent -- it grants Canvas access without a
    password -- so it lives in the vault at mode 600, never in the workspace.
    """
    try:
        state = context.storage_state()
    except Exception:
        return
    SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_STATE_FILE.write_text(json.dumps(state))
    SESSION_STATE_FILE.chmod(0o600)


@contextlib.contextmanager
def open_page(**kwargs) -> Iterator[Page]:
    """Convenience wrapper yielding a single page from a persistent context."""
    with open_context(**kwargs) as context:
        page = context.pages[0] if context.pages else context.new_page()
        yield page


def save_debug_snapshot(page: Page, label: str, directory: Path | None = None) -> Path:
    """Dump a screenshot and the page HTML for diagnosing a stuck flow.

    Headless means we cannot simply look at the screen, so anything that fails
    in an unexpected place needs to leave evidence behind.
    """
    directory = directory or Path.home() / "canon" / "workbook" / "temp" / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / label
    page.screenshot(path=f"{stem}.png", full_page=True)
    Path(f"{stem}.html").write_text(page.content())
    return Path(f"{stem}.png")
