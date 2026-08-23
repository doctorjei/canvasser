"""Duo second-factor handling.

Design note: the second factor sits behind the `Approver` interface so that the
login flow never needs to know how approval happened. Today there are two
implementations, both requiring a human. A future unattended implementation
(TOTP seed, or notify-then-push) can be added here without touching `auth.py`.

Honest caveat: the Duo pages below could not be inspected during development,
because reaching them requires valid GatorLink credentials. Everything here is
written to tolerate not finding what it expects, and to leave a snapshot behind
when it doesn't. Expect to refine the selectors on the first real run.
"""

from __future__ import annotations

import sys
from typing import Protocol

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .browser import save_debug_snapshot
from .progress import duo_box, glyphs_for

DUO_HOST_FRAGMENT = "duosecurity.com"

#: Duo "Verified Push" shows a number on screen that the user must tap on their
#: phone. Without it the push cannot be approved at all, so surfacing it is not a
#: nicety -- it is the difference between a working login and a stuck one.
VERIFICATION_CODE_SELECTORS = (
    ".verification-code",
    "[class*='verification-code']",
    "[class*='verification']",
    "[data-testid*='verification']",
)

#: Candidate labels for the "remember this device" affirmative. Clicking it is
#: what buys the ~10 hour window in which runs need no human at all, so it is
#: worth trying several wordings rather than one brittle selector.
TRUST_LABELS = (
    "Yes, this is my device",
    "Yes, trust browser",
    "Trust browser",
    "Remember me for 10 hours",
)

PASSCODE_LABELS = ("Enter a passcode", "Use a passcode", "Passcode")


class ApprovalError(RuntimeError):
    """Raised when the second factor could not be completed."""


class Approver(Protocol):
    """Completes the Duo challenge on an already-loaded Duo page."""

    name: str

    def approve(self, page: Page, timeout_ms: int) -> None: ...


def on_duo_page(page: Page) -> bool:
    return DUO_HOST_FRAGMENT in page.url


def _click_first_matching(page: Page, labels: tuple[str, ...], timeout_ms: int = 3_000) -> bool:
    """Click the first label that exists. Returns whether anything was clicked.

    Absence is not an error: Duo skips the trust prompt entirely when the device
    is already remembered, and skips factor selection when a default is set.
    """
    for label in labels:
        candidate = page.get_by_role("button", name=label).or_(page.get_by_text(label, exact=False))
        try:
            candidate.first.click(timeout=timeout_ms)
            return True
        except (PlaywrightTimeout, Exception):
            continue
    return False


#: Duo can bounce to its own *exit* endpoint carrying an error. That URL is still
#: on the Duo host, so a naive "have we left Duo yet" wait sits there until it
#: times out and then blames the user for not approving. Detect it explicitly.
DUO_EXIT_FRAGMENT = "/exit"

#: How often to re-read the number Duo is showing while we wait. Chosen with the
#: user: often enough to catch a re-sent push promptly, slow enough not to spam.
NUMBER_RESCAN_MS = 5_000


#: Whether a Duo box has been drawn this run. The caller leaves its
#: "Connecting..." line open, and Duo's box lands in the middle of it -- on
#: stderr, so it cannot be detected by watching stdout. This is how the caller
#: finds out that its line was interrupted and it must start a fresh one.
_announced = False


def was_announced() -> bool:
    return _announced


def announce_number(number: str | None) -> None:
    """Show Duo's Verified Push number, or the instruction when there is none.

    `None` is not an error: plenty of Duo tenants have Verified Push switched
    off, and a plain approve tap is then all that is needed. The frame is drawn
    either way so the screen keeps its shape.

    Goes to **stderr**, as it always has -- this is a prompt to a human, not
    part of any output being captured.
    """
    global _announced
    _announced = True
    box = duo_box(number, glyphs_for(sys.stderr))
    print("\n" + "\n".join(f"             {line}" for line in box) + "\n",
          file=sys.stderr, flush=True)


def _wait_for_duo_to_clear(page: Page, timeout_ms: int, watch_number: bool = False) -> None:
    """Wait until we are no longer on Duo, whatever the reason we were there.

    The success signal is deliberately selector-free -- Duo's markup changes,
    "we left the Duo host" does not. But we poll rather than blocking on a single
    predicate so that we can (a) narrate URL transitions, which is the only
    visibility anyone has into a headless login, and (b) catch Duo's own error
    exits, which never leave the host and would otherwise look like the user
    simply never tapped approve.
    """
    waited_ms = 0
    interval_ms = 1_000
    last_url = ""
    last_number: str | None = None

    while waited_ms < timeout_ms:
        url = page.url
        if url != last_url:
            _log_url(url)
            last_url = url

        if DUO_HOST_FRAGMENT not in url:
            return

        # Clicking "Yes, this is my device" is both what unblocks the flow and
        # what buys the ~10h remembered-device window, so it belongs here in the
        # loop -- see _try_accept_trust_prompt for why "after the wait" is wrong.
        if _try_accept_trust_prompt(page):
            print(
                "     ... approved; told Duo to remember this device.",
                file=sys.stderr,
                flush=True,
            )

        # Re-read the number every few seconds rather than once at the start.
        # Duo re-renders -- a push can expire and be re-sent with a *different*
        # number -- and a stale number on screen is worse than none: the user
        # taps a digit that is no longer correct and the login silently stalls.
        if watch_number and waited_ms % NUMBER_RESCAN_MS == 0:
            number = _scan_for_number(page)
            if number and number != last_number:
                announce_number(number)
                last_number = number

        if DUO_EXIT_FRAGMENT in url and "error=" in url:
            reason = url.split("error=", 1)[1].split("&", 1)[0]
            snapshot = save_debug_snapshot(page, "duo-exit-error")
            raise ApprovalError(
                f"Duo rejected the session with error '{reason}' -- this is Duo "
                f"refusing the browser, not you failing to approve.\n"
                f"URL: {url}\nSnapshot: {snapshot}"
            )

        try:
            page.wait_for_timeout(interval_ms)
        except Exception:
            break
        waited_ms += interval_ms

    snapshot = save_debug_snapshot(page, "duo-timeout")
    raise ApprovalError(
        f"Still on Duo after {timeout_ms // 1000}s.\n"
        f"Final URL: {page.url}\n"
        f"Snapshot: {snapshot}\n"
        f"If you DID approve, the tap reached Duo but Duo did not hand control "
        f"back -- check the snapshot before assuming the approval failed."
    )


def _log_url(url: str) -> None:
    """Narrate navigation. Headless means this is the only view of the flow."""
    print(f"     ... now at: {url[:120]}", file=sys.stderr, flush=True)


def _accept_trust_prompt(page: Page) -> bool:
    """Best-effort click of 'remember this device'; harmless when absent."""
    return _click_first_matching(page, TRUST_LABELS)


def _try_accept_trust_prompt(page: Page) -> bool:
    """Click "Yes, this is my device" if it is on screen right now.

    Must run *during* the wait for Duo to clear, not after it. Duo shows this
    prompt while we are still on the Duo host, so any design that waits to leave
    Duo before handling it deadlocks: the wait blocks the click, and the missing
    click is the very thing that would let us leave. That deadlock burned two
    live logins -- the user's approval had succeeded both times.

    Kept cheap (count first, short click timeout) because it runs every poll.
    """
    for label in TRUST_LABELS:
        try:
            button = page.get_by_role("button", name=label)
            if button.count() == 0:
                continue
            button.first.click(timeout=2_000)
            return True
        except Exception:
            continue
    return False


def _scan_for_number(page: Page) -> str | None:
    for selector in VERIFICATION_CODE_SELECTORS:
        try:
            text = page.locator(selector).first.inner_text(timeout=1_000)
        except Exception:
            continue
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            return digits

    # Fallback: a standalone all-digits line in the visible text. Observed Duo
    # codes here have all been exactly 3 digits (565, 180, 161), and loosening
    # this to 1-3 digits made it announce a bogus "TAP 1" scraped from unrelated
    # markup. Telling the user to tap a wrong number is worse than telling them
    # nothing, so keep the match strict.
    try:
        body = page.inner_text("body", timeout=2_000)
    except Exception:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.isdigit() and len(stripped) == 3:
            return stripped
    return None


def read_verification_number(page: Page, timeout_ms: int = 15_000) -> str | None:
    """Return the Verified Push number Duo is displaying, if there is one.

    Duo sends the push and renders the number on its own -- nothing needs to be
    clicked -- but the number appears via client-side render, so we poll briefly
    rather than reading once and concluding there isn't one.

    Not every Duo tenant enables Verified Push; when it is off there is no number
    and a plain approve/deny tap is enough. Absence is therefore normal and must
    not be treated as an error.
    """
    deadline_polls = max(1, timeout_ms // 1_500)
    for _ in range(deadline_polls):
        number = _scan_for_number(page)
        if number:
            return number
        try:
            page.wait_for_timeout(1_500)
        except Exception:
            break
    return None


class PushApprover:
    """Send a Duo push and wait for the user to tap approve on their phone.

    The default. Requires no selectors for the approval *itself* -- we simply
    wait for Duo to hand control back -- which makes it the most durable of the
    two against Duo redesigns. Reading the Verified Push number does need
    selectors, but failing to find one is non-fatal.
    """

    name = "push"

    def approve(self, page: Page, timeout_ms: int = 120_000) -> None:
        print(
            "\n  >> Duo push sent. Watch for the number below and tap it on your phone."
            "\n     If you did NOT expect this prompt, DENY it -- an unexpected"
            "\n     push means someone else has the password.\n",
            file=sys.stderr,
            flush=True,
        )

        # Unconditional: a tenant with Verified Push switched off returns None,
        # and that person still needs telling to go and tap Approve. Guarding
        # this on a number meant they got a silent screen.
        announce_number(read_verification_number(page))

        # watch_number: keep re-reading. The number on screen is not a constant.
        _wait_for_duo_to_clear(page, timeout_ms, watch_number=True)


class PasscodeApprover:
    """Prompt for a 6-digit Duo passcode and submit it.

    The fallback for when push is unavailable or inconvenient. Needs the user at
    a terminal, unlike push. Reads the code from Duo Mobile or a hardware token;
    no TOTP seed is stored anywhere.
    """

    name = "passcode"

    def approve(self, page: Page, timeout_ms: int = 120_000) -> None:
        _click_first_matching(page, PASSCODE_LABELS)

        code = input("  Enter your 6-digit Duo passcode: ").strip()
        if not (code.isdigit() and len(code) == 6):
            raise ApprovalError(f"Expected 6 digits, got {code!r}")

        field = page.locator("input[type='text'], input[type='tel'], input[name*='passcode' i]")
        try:
            field.first.fill(code, timeout=10_000)
            field.first.press("Enter")
        except PlaywrightTimeout as exc:
            raise ApprovalError("Could not find the Duo passcode field.") from exc

        _wait_for_duo_to_clear(page, timeout_ms)


def complete_duo(page: Page, approver: Approver, timeout_ms: int = 120_000) -> None:
    """Run the Duo challenge to completion, then accept the trust prompt."""
    approver.approve(page, timeout_ms)

    # The trust prompt may appear after approval rather than before it.
    if on_duo_page(page):
        _accept_trust_prompt(page)
        _wait_for_duo_to_clear(page, 30_000)


APPROVERS: dict[str, type] = {
    "push": PushApprover,
    "passcode": PasscodeApprover,
}
