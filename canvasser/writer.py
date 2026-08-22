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


#: The same date/time pair, read back out of the DOM. Located by label and
#: adjacency exactly as `_INPUT_INDEX` does, rather than by remembering an
#: index: the picker popup adds and removes visible inputs, so an index taken
#: before typing does not reliably still mean the same box afterwards.
_FIELD_VALUES = """(label) => {
    const labelOf = e => {
        const byFor = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        return (byFor?.textContent || e.getAttribute('aria-label') || '').trim();
    };
    const inputs = [...document.querySelectorAll('input')]
        .filter(e => e.type !== 'hidden' && e.offsetParent !== null);
    const i = inputs.findIndex(e => labelOf(e).toLowerCase().startsWith(label.toLowerCase()));
    if (i < 0) return null;
    const next = inputs[i + 1];
    if (!next || !/^time$/i.test(labelOf(next))) return null;
    return {date: inputs[i].value.trim(), time: next.value.trim()};
}"""

#: What "the form has finished initialising" actually looks like.
#:
#: Canvas disables its submit button and labels it **"Loading..."** until every
#: async panel on the page has loaded. On a Turnitin-backed assignment the
#: plagiarism panel keeps that going for many seconds. Proved live 2026-08-22:
#: two assignments failed with "no Save button" and their snapshots showed a
#: fully-typed form, a spinning Turnitin panel, and a greyed "Loading..."
#: button.
#:
#: Waiting for this state rather than sleeping is the whole fix. A fixed sleep
#: also silently corrupts the *successful* path: if the async load lands after
#: the dates are typed, React re-initialises the form from server state, the
#: typed values vanish, and the save writes the old dates while looking
#: perfectly healthy.
SAVE_READY = """() => {
    const controls = [...document.querySelectorAll('button, input[type=submit]')]
        .filter(e => e.offsetParent !== null);
    return controls.some(e =>
        /^\\s*save\\s*$/i.test((e.textContent || e.value || '').trim()) && !e.disabled);
}"""

#: The other half of "ready", and the half a Save-only check misses.
#:
#: **The Assign-To card mounts AFTER the submit button goes live.** Found by
#: recon on 2026-08-22: immediately after `SAVE_READY` became true, the form's
#: visible inputs were Assignment Name, Points, two checkboxes -- and no date
#: row at all. Acting on that moment finds no date field and no Clear button,
#: which is exactly how the first live clearing attempt failed.
DATES_READY = """() => {
    const labelOf = e => {
        const byFor = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        return (byFor?.textContent || e.getAttribute('aria-label') || '').trim();
    };
    const inputs = [...document.querySelectorAll('input')]
        .filter(e => e.type !== 'hidden' && e.offsetParent !== null);
    const i = inputs.findIndex(e =>
        /^(due|available from|until)\\b/i.test(labelOf(e)));
    if (i < 0) return false;
    const next = inputs[i + 1];
    return !!next && /^time$/i.test(labelOf(next));
}"""

#: Both halves. Waiting on one of them is waiting on half a form.
FORM_READY = f"() => ({SAVE_READY})() && ({DATES_READY})()"


#: Each date row carries its own **Clear** button, and that button says which
#: row it belongs to -- in a screen-reader span, not on screen:
#:
#:     <button>  "Clear due date/time for Everyone"            + "Clear"
#:     <button>  "Clear available from date/time for Everyone" + "Clear"
#:     <button>  "Clear until date/time for Everyone"          + "Clear"
#:
#: So `textContent` reads `"Clear due date/time for EveryoneClear"`. A first
#: attempt matched `^clear$` against that and found nothing on the live form
#: (2026-08-22), which is why this is now matched on the accessible phrase.
#:
#: That is a better handle than the document-order adjacency used for the
#: inputs: it **names the field**, so it cannot silently land on a neighbouring
#: row. Canvas's own control is used rather than blanking the boxes by hand,
#: because a hand-blanked widget still holds parsed state its text no longer
#: matches, and what then gets submitted is anyone's guess.
#:
#: The match is tagged with an attribute so the click targets that exact
#: element rather than an index the click itself could invalidate.
_MARK_CLEAR = """(label) => {
    const visible = e => e.offsetParent !== null;
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const escaped = label.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&').replace(/ +/g, '\\\\s+');
    const wanted = new RegExp('^clear\\\\s+' + escaped + '\\\\s+date/time\\\\b', 'i');

    document.querySelectorAll('[data-canvasser-clear]')
        .forEach(e => e.removeAttribute('data-canvasser-clear'));
    const hits = [...document.querySelectorAll('button')]
        .filter(visible)
        .filter(e => wanted.test(norm(e.textContent)));
    if (hits.length === 1) hits[0].setAttribute('data-canvasser-clear', '1');
    return hits.length;
}"""


def clear_field(page: Page, label: str) -> None:
    """Empty one date row using the form's own Clear control.

    Raises rather than falling back to blanking the boxes by hand, and refuses
    a tie rather than picking: a date the tool *thinks* it cleared and did not
    is a deadline still hanging over a class.
    """
    found = page.evaluate(_MARK_CLEAR, label)
    if found != 1:
        snapshot = save_debug_snapshot(page, f"no-clear-{label.replace(' ', '-')}")
        raise WriteFailed(
            f"expected exactly one 'Clear' button for the {label!r} date on "
            f"{page.url}, found {found}. Canvas's date-row layout has changed "
            f"and clearing must be re-checked before it is trusted. "
            f"Snapshot: {snapshot}"
        )
    page.locator('[data-canvasser-clear="1"]').click()
    page.wait_for_timeout(200)


def wait_for_form(page: Page, timeout: int = 90_000) -> None:
    """Block until the edit form can actually be saved.

    Generous timeout: this is waiting on a third-party LTI panel, it happens
    once per assignment, and the cost of giving up early is either a refused
    write or -- far worse -- a silent one that saves the wrong dates.
    """
    try:
        page.wait_for_function(FORM_READY, timeout=timeout)
    except PlaywrightTimeout:
        snapshot = save_debug_snapshot(page, "form-never-ready")
        save_ready = page.evaluate(SAVE_READY)
        dates_ready = page.evaluate(DATES_READY)
        raise WriteFailed(
            f"the edit form at {page.url} never finished loading after "
            f"{timeout // 1000}s (save button ready: {save_ready}; date row "
            f"present: {dates_ready}). Nothing was typed or saved. "
            f"Snapshot: {snapshot}"
        ) from None


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
    # NOT a sleep. See SAVE_READY: the form's own submit button reports when it
    # is done initialising, and typing before then is how dates get silently
    # reverted and the wrong values saved.
    wait_for_form(page)

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
    #: What each field should read back as, keyed by its Canvas label. Checked
    #: against the live DOM before anything is saved.
    wanted: dict[str, tuple[str, str]] = {}

    for field, (date_text, time_text) in changes.items():
        label = FIELD_LABELS[field]

        # An empty date cell in a column the sheet carries means "clear this".
        # `push.compare` has already established that the field currently holds
        # something, so this is a real removal, not a no-op.
        if not date_text:
            clear_field(page, label)
            wanted[label] = ("", "")
            written.append(
                Written(assignment_id=assignment_id, field=field,
                        wanted="", typed="(cleared)", confirmed="")
            )
            continue

        moment = to_profile_time(date_text, time_text, source_tz, profile_tz)
        form_date, form_time = format_for_form(moment)

        date_index, time_index = find_field(page, label)
        for index, value in ((date_index, form_date), (time_index, form_time)):
            box = inputs.nth(index)
            box.click()
            box.fill("")
            box.type(value, delay=25)
            # Escape closes the picker popup without discarding what was typed;
            # blur is what makes the widget parse and commit it.
            #
            # **Enter is deliberately not used.** It commits the picker only
            # when the picker has focus -- otherwise it submits the form.
            # Proved live 2026-08-22: two assignments were saved by a stray
            # Enter *after* this code had decided not to save them, so the
            # refusal refused nothing. Both happened to be fully typed; a mid-
            # row Enter would have written half a row.
            box.press("Escape")
            box.evaluate("element => element.blur()")
            page.wait_for_timeout(150)

        wanted[label] = (form_date, form_time)

        written.append(
            Written(
                assignment_id=assignment_id,
                field=field,
                wanted=f"{date_text} {time_text}".strip(),
                typed=f"{form_date} {form_time}",
                confirmed="",
            )
        )

    # **Read the form back before saving it.** A React form that re-initialises
    # after the values were typed shows no error, saves cleanly, and writes the
    # dates that were already there -- which is exactly what happened to four
    # assignments on 2026-08-22. Only the post-write check caught it, and by
    # then a wrong save had already been made. Checking here turns a silent
    # wrong write into a refusal that writes nothing.
    drifted = []
    for label, (form_date, form_time) in wanted.items():
        current = page.evaluate(_FIELD_VALUES, label)
        if current is None:
            drifted.append(f"{label}: its inputs are no longer on the page")
        elif (current["date"], current["time"]) != (form_date, form_time):
            drifted.append(
                f"{label}: typed {form_date!r} {form_time!r}, form now holds "
                f"{current['date']!r} {current['time']!r}"
            )
    if drifted:
        snapshot = save_debug_snapshot(page, f"reverted-{assignment_id}")
        raise WriteRefused(
            f"assignment {assignment_id}: the form did not keep what was typed, "
            f"so saving it would write the wrong dates -- {'; '.join(drifted)}. "
            f"Nothing was saved. Snapshot: {snapshot}"
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
