"""Canvasser's Duo wiring over the duo-pass package.

The challenge logic — push approval with Verified Push number matching, the
typed passcode, the wait for leaving the Duo host — lives in `duo_pass` (the
`duo-pass` dependency) and is not repeated here. This module holds what only
the host can supply — where snapshots go, how the number is announced — and
re-exports the package surface so existing importers don't move.

Design note, carried over: the second factor sits behind the `Approver`
interface so that the login flow never needs to know how approval happened. A
future unattended implementation (TOTP seed, or notify-then-push) can be added
without touching `auth.py`.

One deliberate change arrived with the move: `PasscodeApprover()` with no
arguments now prompts on the controlling terminal (`/dev/tty`) instead of
reading stdin. That is the credentials convention (`_read_from_tty`, never
stdin) applied to the last prompt that didn't follow it; no `code_source` is
injected because there is no vault or UI source to supply, and the default is
the correction. sshpass flows are unaffected — its `assword` match string
never appears in the Duo prompt.
"""

from __future__ import annotations

import sys

from duo_pass import (
    APPROVERS,
    ApprovalError,
    Approver,
    PasscodeApprover,
    PushApprover,
    complete_duo,
    configure,
    on_duo_page,
    read_verification_number,
)

from .browser import save_debug_snapshot
from .progress import duo_box, glyphs_for, print_box

__all__ = [
    "APPROVERS",
    "ApprovalError",
    "Approver",
    "PasscodeApprover",
    "PushApprover",
    "announce_number",
    "complete_duo",
    "on_duo_page",
    "read_verification_number",
    "was_announced",
]

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
    print_box(duo_box(number, glyphs_for(sys.stderr)), sys.stderr)


configure(snapshot=save_debug_snapshot, announce=announce_number)
