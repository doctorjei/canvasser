"""Persistent browser session.

We use a persistent Playwright context rather than a fresh browser per run so
that Canvas's session cookie and Duo's "remember this device" cookie survive
between invocations. Those two cookies are the entire reason a run can proceed
without bothering the user: Duo's remembered-device window is ~10 hours, and
Canvas keeps its own session alongside it.

The profile therefore holds live credentials-equivalent state and lives in the
state directory under mode-700, not in the working directory.

The session machinery itself -- context, cookies, install -- is
`multipass.browser`. What stays here is the failure evidence: `save_debug_snapshot`
renders PNG *and* HTML under the sensitive-page gate, which is this package's
FERPA policy and not something a shared library can own.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from multipass.browser import (
    BrowserUnavailable,
    DOWNLOAD_SIZE,
    install_chromium,
    open_context,
    open_page,
)
from playwright.sync_api import Error as PlaywrightError, Page

from .config import state_dir
from .sensitive import why_withhold

__all__ = [
    "BrowserUnavailable",
    "DOWNLOAD_SIZE",
    "install_chromium",
    "open_context",
    "open_page",
    "save_debug_snapshot",
]


def save_debug_snapshot(page: Page, label: str, directory: Path | None = None) -> Path:
    """Dump a screenshot and the page HTML for diagnosing a stuck flow.

    Headless means we cannot simply look at the screen, so anything that fails
    in an unexpected place needs to leave evidence behind.

    **A page carrying student or grade data is not captured** -- see
    `sensitive.py`. A breadcrumb is written in its place, so the caller's
    "Snapshot: <path>" still names a real file and the reader learns both that
    the flow failed and why there is no render of it.

    This is the one place that decision is made, which is what keeps the 27
    call sites free of it: a caller that had to remember to ask would be one
    refactor from a caller that forgot.
    """
    # Under the state directory, not a path invented here. An earlier version
    # wrote to ~/canon/workbook/temp/snapshots -- meaningful only inside the
    # sandbox this was built in, and it created that tree on a real user's
    # laptop. Snapshots render whole Canvas pages and can contain student data,
    # so the mode-700 state directory is also the right place for them on
    # sensitivity grounds.
    directory = directory or state_dir() / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    # `mkdir` takes the umask, which is 022 on a stock machine -- so this comes
    # out world-readable unless it is set explicitly, and these files render
    # whole Canvas pages: student names, grades, submissions. The mode is
    # applied on EVERY call, not just at creation, because a directory made by
    # an older build (or by a `parents=True` walk) is already wrong and would
    # never be corrected otherwise.
    #
    # Do not rely on the parent for this. `state_dir()` has no guaranteed mode
    # -- it is 700 here only because this box points $CANVASSER_HOME at a vault
    # that happens to be 700, while the platform default (`~/.local/state`) is
    # not. Found 2026-09-09: the live directory was 755.
    directory.chmod(0o700)
    stem = directory / label

    # Asked BEFORE anything is written, not cleaned up afterwards. A render
    # that reaches disk and is deleted has still been on disk, and on a machine
    # where the state directory is the platform default it has been there
    # world-readable for the width of that window.
    withheld = why_withhold(page)
    if withheld:
        return _withhold(stem, label, page, withheld)

    page.screenshot(path=f"{stem}.png", full_page=True)
    Path(f"{stem}.html").write_text(page.content())
    for path in (Path(f"{stem}.png"), Path(f"{stem}.html")):
        path.chmod(0o600)
    return Path(f"{stem}.png")


def _withhold(stem: Path, label: str, page: Page, reasons: tuple[str, ...]) -> Path:
    """Record that a snapshot was deliberately not taken, and why.

    The URL is included: it names the route and the ids, which is what makes
    the failure locatable, and a numeric id is not a person's name. Nothing is
    read out of the page itself -- that is the whole point.
    """
    try:
        url = page.url
    except PlaywrightError:  # pragma: no cover - the fail-closed path's own edge
        url = "(could not be read)"
    path = Path(f"{stem}.txt")
    path.write_text(
        "canvasser: snapshot WITHHELD -- this page carries student or grade data.\n"
        f"label:  {label}\n"
        f"when:   {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"url:    {url}\n"
        f"why:    {'; '.join(reasons)}\n"
        "\n"
        "No screenshot or HTML was written. See canvasser/sensitive.py.\n"
    )
    path.chmod(0o600)
    return path
