"""Writing dates back into Canvas's assignment edit form.

This is the only module that changes anything. Everything it does is shaped by
two hazards found on the real form, both of which fail *silently* if ignored.

## Hazard 1: the form speaks the USER's timezone, not the course's

Observed live: the course runs `America/New_York`, the user's Canvas profile is
`Asia/Tokyo`, and the edit form renders `2026-02-01 23:59:59` course-time as
`Feb 2, 2026` / `1:59 PM`. Typing a course-time value straight in would move
every deadline by 14 hours, with nothing to notice.

So every value is converted from the sheet's zone to the profile zone before it
is typed, and `ENV.TIMEZONE` on the page itself is the authority for what the
profile zone is -- not a guess, not a setting.

Changing the user's profile timezone to dodge this is not an option: it is
global, and would silently reinterpret every other course they teach.

## Hazard 2: saving submits EVERY date card

Canvas's edit form posts all of its "Assign to" cards on save, not just the one
that was touched. Rebuilding form state from a CSV would therefore delete any
override the sheet does not know about -- a student's accommodation date, for
instance. Two rules follow:

1. **Load the form and modify it in place.** Never construct its state.
2. **Refuse outright to write an assignment that has overrides**, until the
   multi-card path is built and tested against a course that actually has them.
   The course this was developed on has none, so nothing here has ever
   exercised that case, and a write that "probably works" is not good enough
   when the failure silently removes a student's accommodation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .browser import save_debug_snapshot
from .config import Config
from .dateparse import DateFormatError, resolve, zone_of

#: Labels Canvas gives the three date fields. Stable and meaningful, unlike the
#: React ids beside them (`Selectable___1`, `Select___2`) which are render-order
#: counters and differ between two loads of the same page.
FIELD_LABELS = {
    "unlock_at": "Available from",
    "due_at": "Due",
    "lock_at": "Until",
}


class WriteRefused(Exception):
    """A write was not attempted, on purpose."""


class WriteFailed(Exception):
    """A write was attempted and did not take."""


@dataclass(frozen=True)
class Written:
    assignment_id: str
    field: str
    wanted: str      # course-local, as the sheet expressed it
    typed: str       # profile-local, as the form received it
    confirmed: str   # what ENV held afterwards, course-local


def to_profile_time(
    date_text: str, time_text: str, source_tz: str, profile_tz: str
) -> datetime:
    """A course-local date/time as an instant in the profile's zone.

    The zone math -- including the refusal of a wall clock that does not exist
    -- lives in `dateparse.resolve`, shared with the sheet realignment `push`
    does. Only the exception type is translated: a bad cell reaching this far
    is a write that must not be attempted, not a parse problem.
    """
    try:
        local = resolve(date_text, time_text, source_tz)
        return local.astimezone(zone_of(profile_tz))
    except DateFormatError as exc:
        raise WriteRefused(str(exc)) from exc


def format_for_form(moment: datetime) -> tuple[str, str]:
    """`Feb 2, 2026` and `1:59 PM`, the shapes the form renders."""
    return moment.strftime("%b %-d, %Y"), moment.strftime("%-I:%M %p")


#: How the visible inputs are laid out, confirmed by recon:
#:
#:     Selectable___1  'Due Date'        Feb 2, 2026
#:     Select___1      'Time'            1:59 PM
#:     Selectable___3  'Available from'  Jan 12, 2026
#:     Select___2      'Time'            2:00 PM
#:     ...             'Until'
#:     Select___3      'Time'
#:
#: All three time boxes are labelled just "Time", so a time input can only be
#: identified by *which date box it follows*. Document-order adjacency is the
#: relationship being relied on -- not a container class or an xpath, both of
#: which are the kind of thing Canvas restyles.
_INPUT_INDEX = """(label) => {
    const labelOf = e => {
        const byFor = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        return (byFor?.textContent || e.getAttribute('aria-label') || '').trim();
    };
    const inputs = [...document.querySelectorAll('input')]
        .filter(e => e.type !== 'hidden' && e.offsetParent !== null);
    const i = inputs.findIndex(e => labelOf(e).toLowerCase().startsWith(label.toLowerCase()));
    if (i < 0) return null;
    // The following input must be the paired time box, or the pairing
    // assumption is wrong and nothing should be typed.
    const next = inputs[i + 1];
    return {date: i, time: next && /^time$/i.test(labelOf(next)) ? i + 1 : null};
}"""


def find_field(page: Page, label: str) -> tuple[int, int]:
    """Indices of the (date, time) inputs for one Canvas date field.

    Raises rather than returning a partial answer: writing a date without its
    time, or into an input we only think is the right one, is worse than not
    writing at all.
    """
    found = page.evaluate(_INPUT_INDEX, label)
    if not found:
        raise WriteFailed(f"no visible input labelled {label!r} on {page.url}")
    if found["time"] is None:
        raise WriteFailed(
            f"the input after {label!r} is not its time box -- Canvas's date "
            f"field layout has changed and this must be re-checked before any "
            f"write."
        )
    return found["date"], found["time"]


#: A quiz-backed assignment redirects to /quizzes/<qid>/edit and carries
#: ENV.QUIZ; a plain one stays put and carries ENV.ASSIGNMENT. The *date widget
#: is identical on both* -- same "Due Date"/"Available from"/"Until" labels,
#: same date-then-time adjacency -- so only the state lookups differ, not the
#: typing.
SUBJECT_READY = "() => !!(window.ENV && (window.ENV.ASSIGNMENT || window.ENV.QUIZ))"

_SUBJECT = "(window.ENV?.ASSIGNMENT || window.ENV?.QUIZ || {})"


def read_overrides(page: Page) -> int:
    return page.evaluate(
        f"() => ({_SUBJECT}.assignment_overrides || []).length"
        " + (window.ENV?.ASSIGNMENT_OVERRIDES || []).length"
    )


def profile_timezone(page: Page) -> str:
    return page.evaluate("() => window.ENV?.TIMEZONE || ''")


def apply_changes(
    page: Page,
    config: Config,
    course_id: str,
    assignment_id: str,
    changes: dict[str, tuple[str, str]],
    source_tz: str,
) -> list[Written]:
    """Set date fields on one assignment and save. **This writes.**

    `changes` maps a Canvas field name to the (date, time) the sheet asks for,
    in the sheet's own zone. Returns what was actually confirmed afterwards.

    The form is loaded and edited in place -- never rebuilt -- and an
    assignment carrying overrides is refused outright. See the module docstring
    for why both of those are non-negotiable.
    """
    _open_editor(page, config, course_id, assignment_id)
    # The date fields are React and mount after ENV lands.
    page.wait_for_timeout(2_500)

    overrides = read_overrides(page)
    if overrides:
        raise WriteRefused(
            f"assignment {assignment_id} has {overrides} override(s). Saving "
            f"this form submits every date card, and the multi-card path has "
            f"never been tested -- a wrong move here deletes a student's "
            f"accommodation date. Refusing."
        )

    profile_tz = profile_timezone(page)
    if not profile_tz:
        raise WriteRefused(
            f"cannot read ENV.TIMEZONE on {page.url}; without the profile "
            f"timezone every value typed would be off by the course/profile "
            f"offset."
        )

    inputs = page.locator("input:visible")
    written: list[Written] = []

    for field, (date_text, time_text) in changes.items():
        if not date_text:
            raise WriteRefused(
                f"clearing {field} is not implemented -- emptying a date field "
                f"needs its own handling and its own test."
            )
        label = FIELD_LABELS[field]
        moment = to_profile_time(date_text, time_text, source_tz, profile_tz)
        form_date, form_time = format_for_form(moment)

        date_index, time_index = find_field(page, label)
        for index, value in ((date_index, form_date), (time_index, form_time)):
            box = inputs.nth(index)
            box.click()
            box.fill("")
            box.type(value, delay=25)
            # Canvas's pickers commit on blur/Enter; Escape closes the popup
            # without discarding what was typed.
            box.press("Enter")
            page.wait_for_timeout(300)
            box.press("Escape")

        written.append(
            Written(
                assignment_id=assignment_id,
                field=field,
                wanted=f"{date_text} {time_text}".strip(),
                typed=f"{form_date} {form_time}",
                confirmed="",
            )
        )

    save = _save_button(page)
    if save is None:
        snapshot = save_debug_snapshot(page, f"no-save-button-{assignment_id}")
        raise WriteFailed(
            f"no Save button found on {page.url}. Snapshot: {snapshot}"
        )
    save.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2_000)
    return written


def verify(page: Page, config: Config, course_id: str, assignment_id: str) -> dict:
    """Re-read an assignment's dates straight from ENV after a save.

    A save that appears to succeed and did not is the failure this exists to
    catch: Canvas can reject a date silently and re-render the old value.
    """
    page.goto(
        f"{config.base_url}/courses/{course_id}/assignments/{assignment_id}/edit",
        wait_until="domcontentloaded",
    )
    page.wait_for_function(SUBJECT_READY, timeout=20_000)
    return page.evaluate(
        f"""() => {{ const a = {_SUBJECT};
             return {{due_at: a.due_at, unlock_at: a.unlock_at, lock_at: a.lock_at}}; }}"""
    )


def _open_editor(page: Page, config: Config, course_id: str, assignment_id: str) -> None:
    """Land on whichever edit page actually carries this item's date state.

    Three shapes, and only the first is the plain case:

        plain assignment  /assignments/<id>/edit          ENV.ASSIGNMENT
        classic quiz      -> redirects to /quizzes/<qid>/edit   ENV.QUIZ
        New Quizzes       /assignments/<id>/edit lands on a `/build/` app with
                          an EMPTY ENV; the real form is at ?quiz_lti=true

    The read path learned this already. Repeating it here rather than sharing
    one helper because the read path starts from the index href it was given,
    while a write only has an id -- but the failure being guarded against is
    the same: a page that looks loaded and carries no state at all.
    """
    base = f"{config.base_url}/courses/{course_id}/assignments/{assignment_id}/edit"
    for url in (base, f"{base}?quiz_lti=true"):
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_function(SUBJECT_READY, timeout=12_000)
            return
        except PlaywrightTimeout:
            continue
    snapshot = save_debug_snapshot(page, f"no-editor-{assignment_id}")
    raise WriteFailed(
        f"no date-bearing edit form for assignment {assignment_id}: neither "
        f"{base} nor its quiz_lti form carried ENV.ASSIGNMENT or ENV.QUIZ. "
        f"Refusing to type into a page whose state cannot be read back. "
        f"Snapshot: {snapshot}"
    )


def _save_button(page: Page):
    """The form's one visible Save control, or None.

    The quiz edit page carries **two** buttons reading "Save": the real one,
    and one belonging to a hidden "move quiz item" dialog
    (`#move_quiz_item_submit_btn`). Worse, the real Save sits *outside* any
    `<form>` while the hidden one is inside one -- so the obvious selector
    `form button:text-is('Save')` finds precisely the wrong button, and
    `button[type=submit]:has-text('Save')` finds both.

    So: visible only, never "Save & Publish" (publishing is instantly visible
    to students and is not a date edit's business), and **if more than one
    survives, return None rather than pick.** Clicking an unknown Save on a
    page full of quiz-editing dialogs is not a guess worth making.
    """
    found = page.locator("button:visible, input[type=submit]:visible").filter(
        has_text=re.compile(r"^\s*save\s*$", re.I)
    )
    count = found.count()
    if count == 1:
        return found.first
    if count > 1:
        # Fall back to the accessible-name lookup, which is the one that
        # distinguished them correctly in recon.
        by_role = page.get_by_role("button", name="Save", exact=True)
        return by_role.first if by_role.count() == 1 else None
    return None
