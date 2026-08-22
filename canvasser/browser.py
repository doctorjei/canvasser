"""Persistent browser session.

We use a persistent Playwright context rather than a fresh browser per run so
that Canvas's session cookie and Duo's "remember this device" cookie survive
between invocations. Those two cookies are the entire reason a run can proceed
without bothering the user: Duo's remembered-device window is ~10 hours, and
Canvas keeps its own session alongside it.

The profile therefore holds live credentials-equivalent state and lives in the
state directory under mode-700, not in the working directory.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    sync_playwright,
)

from .config import PROFILE_DIR, SESSION_STATE_FILE


class BrowserUnavailable(RuntimeError):
    """Chromium could not be started, with an actionable reason."""


#: Roughly what `playwright install chromium` pulls down, for the prompt. Worth
#: naming: consenting to a download is different from consenting to a command.
DOWNLOAD_SIZE = "~150 MB"

INSTALL_HINT = (
    "Chromium is not installed. The `playwright` package on PyPI ships the "
    "library and its driver, but not the browser binaries -- those are a "
    "separate download, because they are platform-specific native builds "
    "rather than Python. Install them with:\n\n"
    "    canvasser install-browser\n\n"
    "which runs `playwright install chromium` for you, in this interpreter's "
    "own environment."
)


def _is_missing_browser(exc: Exception) -> bool:
    """Whether a launch failure means "no browser" rather than "launch broke".

    Matched on Playwright's own wording. Deliberately narrow: treating any
    launch failure as a missing browser would offer to download 150 MB in
    answer to a sandbox or permissions problem, which fixes nothing and looks
    like the tool guessing.
    """
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text


def _launch_failure(exc: Exception) -> BrowserUnavailable:
    """Turn Playwright's launch error into something worth reading.

    Playwright's own message does say what to do, buried in a wall of text
    about drivers and revisions, so the instruction is hoisted to the front.
    """
    if _is_missing_browser(exc):
        return BrowserUnavailable(INSTALL_HINT)
    return BrowserUnavailable(f"could not start Chromium: {exc}")


def install_chromium(with_deps: bool = False) -> None:
    """Run Playwright's own installer for Chromium. **Downloads ~150 MB.**

    `sys.executable -m playwright` rather than a bare `playwright`: the CLI on
    PATH may belong to a different environment than the one importing this
    module -- a venv that was never activated, a pipx install, a system Python.
    Installing into the wrong one downloads 150 MB and changes nothing here.

    Output is not captured, so the user sees the real progress rather than a
    silent multi-minute pause.
    """
    command = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        # Needs root on most Linux distributions; only offered explicitly.
        command.append("--with-deps")
    command.append("chromium")

    print(f"  Running: {' '.join(command)}", file=sys.stderr)
    result = subprocess.run(command)
    if result.returncode != 0:
        raise BrowserUnavailable(
            f"`{' '.join(command)}` exited {result.returncode}. On Linux the "
            f"browser may also need system libraries: try "
            f"`canvasser install-browser --with-deps` (which needs root)."
        )


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
    on_missing_browser=None,
) -> Iterator[BrowserContext]:
    """Open the persistent browser context, creating the profile if needed.

    `on_missing_browser` is an optional callable invoked when the launch fails
    *because Chromium was never downloaded*. Returning True means it has been
    installed and the launch should be retried once. The decision to prompt and
    download lives with the caller, not here -- this module should not own the
    question of whether there is a human to ask.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)

    def launch(p):
        return p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            slow_mo=slow_mo,
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="America/New_York",
            args=["--disable-blink-features=AutomationControlled"],
        )

    with sync_playwright() as p:
        try:
            context = launch(p)
        except PlaywrightError as exc:
            # One retry, and only for a missing browser. If the install ran and
            # the launch still fails, report that failure rather than looping.
            if not (_is_missing_browser(exc) and on_missing_browser
                    and on_missing_browser()):
                raise _launch_failure(exc) from exc
            try:
                context = launch(p)
            except PlaywrightError as retry_exc:
                raise _launch_failure(retry_exc) from retry_exc
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
    password -- so it lives in the state directory at mode 600, never in the
    working directory.
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
