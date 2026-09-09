"""Writing back into Canvas's assignment edit form.

This is the only module that changes anything. Two entry points share one form
and one discipline: **`apply_changes`** sets the three dates, **`apply_settings`**
sets the non-date fields (points, grading type). Both open the form once, set
every value they were given, and save once.

Everything in it is shaped by five hazards found on the real form. **Every one
of them fails silently** -- none throws, none navigates anywhere unusual, and
four of the five produced a run that reported success while the dates were
wrong or absent.

A sixth applies to the settings path specifically: **never infer one control's
shape from another's.** Points is a classic Rails text input with a stable
semantic id; the grading control is a `<select>` Canvas labels "Display Grade
as"; the date pickers are InstUI widgets with ids that rotate between loads.
See `FORM_FIELDS`.

## 1. The form speaks the USER's timezone, not the course's

Observed live: the course runs `America/New_York`, the profile is `Asia/Tokyo`,
and the form renders `2026-02-01 23:59:59` course-time as `Feb 2, 2026` /
`1:59 PM`. Typing a course-time value straight in moves every deadline by 14
hours. Values are converted to the profile zone before typing, and
`ENV.TIMEZONE` *on the page being written* is the authority -- not a setting,
not a guess. Changing the profile timezone to dodge this is not an option: it
is global and would reinterpret every other course the user teaches.

## 2. Saving submits EVERY date card

The form posts all of its "Assign to" cards, not just the one touched.
Rebuilding form state from a CSV would delete any override the sheet does not
know about -- a student's accommodation date. So: **load the form and modify it
in place, never construct it**, and **refuse outright to write an assignment
that has overrides** until that path is built and tested somewhere it can be.
A write that "probably works" is not good enough when the failure silently
removes an accommodation.

## 3. The form is not ready when it looks ready

The submit button reads **"Loading..."** and is disabled until every async
panel loads -- Turnitin's is slow -- and **the Assign-To card mounts after that
button goes live**. A fixed 2.5s wait produced two failures: no Save button
found, or the panel finishing *after* the dates were typed so React
re-initialised the form and the save wrote the old values, looking healthy.
`FORM_READY` requires both halves. Readiness is a state, never a duration.

## 4. Enter submits the form

It commits the date picker only when the picker has focus. Otherwise it
submits, which on 2026-08-22 saved two assignments *after* this code had
decided not to save them -- the refusal refused nothing. Values commit via
`Escape` then an explicit `blur()`.

## 5. Canvas validates server-side and answers only on the page

A rejected save is indistinguishable from a successful one. Confirmed rules, in
Canvas's wording: `"Until date cannot be before due date"`, `"Due date cannot
be before term start"`. `_FIELD_MESSAGES` reads them back, requiring *both*
"still on /edit" and "messages present" -- a successful save navigates away,
and InstUI renders hints through the same component as errors.

## What follows from all five: verify three times

Read the typed values back out of the DOM **before** saving; read Canvas's
field messages **immediately after**; and re-read `ENV` **after that**. Each
catches something the others cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from playwright.sync_api import (Error as PlaywrightError, Page,
                                 TimeoutError as PlaywrightTimeout)

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


#: Field-level validation messages, deduplicated.
#:
#: InstUI nests the same text through several elements
#: (`formFieldMessages` > `formFieldMessages__message` > `formFieldMessage`),
#: so the live page shows one complaint three times. Observed 2026-08-22:
#: "Until date cannot be before due date", x3.
_FIELD_MESSAGES = """() => {
    const seen = new Set();
    document.querySelectorAll('[class*="formFieldMessage"]').forEach(e => {
        if (e.offsetParent === null) return;
        const text = (e.textContent || '').replace(/\\s+/g, ' ').trim();
        if (text) seen.add(text);
    });
    return [...seen];
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

    # **A rejected save looks exactly like a successful one from here.** Canvas
    # answers a validation failure by staying on the form and rendering a
    # field message; nothing throws, nothing navigates. Four rows were reported
    # as written on 2026-08-22 when Canvas had refused all four with "Until
    # date cannot be before due date", and only the post-write ENV comparison
    # noticed. Reading Canvas's own words back is both earlier and clearer.
    #
    # The two signals are required together: a successful save leaves the edit
    # page, and a failed one leaves messages behind. Either alone gives false
    # positives -- InstUI renders hints through the same component.
    complaints = _settle_after_save(page)
    if complaints:
        snapshot = save_debug_snapshot(page, f"rejected-{assignment_id}")
        raise WriteFailed(
            f"Canvas refused the save for assignment {assignment_id}: "
            f"{'; '.join(complaints)}. Nothing was changed. "
            f"Snapshot: {snapshot}"
        )
    return written


#: The non-date settings fields, as confirmed by live recon (2026-08-30).
#:
#: **These are classic Rails form controls with stable, semantic ids** -- a
#: different thing from the InstUI date pickers, whose `Selectable___1` ids are
#: render-order counters and differ between two loads of the same page. So
#: these may be addressed directly.
#:
#: Each entry still records the label and `name` it expects, and both are
#: verified before anything is typed: **"the id still exists" and "the id still
#: means what it meant" are different claims**, and only the second makes a
#: write safe. Canvas reusing an id for a different control is exactly the kind
#: of change that would otherwise write a number into the wrong box.
@dataclass(frozen=True)
class FormField:
    column: str                       # the infosheet column
    selector: str
    label: str                        # what the <label> must read
    name: str                         # what the name attribute must be
    kind: str                         # "text" or "select"
    #: For a select, the exact option VALUES Canvas accepts. Not the visible
    #: text: the option reading "Points" has value `points`, and ENV reports
    #: `points`. Typing the label into a value field is a silent no-op.
    values: tuple[str, ...] = ()


FORM_FIELDS = {
    "points_possible": FormField(
        column="points_possible",
        selector="#assignment_points_possible",
        label="Points",
        name="points_possible",
        kind="text",
    ),
    "grading_type": FormField(
        column="grading_type",
        selector="#assignment_grading_type",
        # NOT "Grading Type" -- Canvas labels this control "Display Grade as",
        # which is what the recon found and what a person sees on the form.
        label="Display Grade as",
        name="grading_type",
        kind="select",
        values=("points", "percent", "letter_grade", "gpa_scale", "pass_fail",
                "not_graded"),
    ),
}

#: ENV value -> the checkbox's `name` attribute (recon 2026-09-09).
#:
#: **Addressed by name, never by an id built from the value.** Four of these
#: look regular and the fifth does not: `student_annotation`'s box is
#: `assignment_annotated_document`, so `assignment_student_annotation` -- the
#: id anyone would construct -- matches nothing at all. A locator that finds
#: nothing on the field the user asked to change is precisely the silent no-op
#: this project keeps meeting.
#:
#: Each box is also paired with a Rails hidden input of the same name holding
#: `0`, so **state is read from the checkbox**; the hidden one always says off.
ONLINE_TYPE_BOXES = {
    "online_text_entry": "online_submission_types[online_text_entry]",
    "online_url": "online_submission_types[online_url]",
    "online_upload": "online_submission_types[online_upload]",
    "media_recording": "online_submission_types[media_recording]",
    "student_annotation": "online_submission_types[student_annotation]",
}

SUBMISSION_SELECT = "#assignment_submission_type"

#: Reads the whole submission-type state back as ENV would report it: the bare
#: mode when it is `none`/`on_paper`, otherwise the ticked sub-types. Returned
#: sorted so a comparison cannot fail on ordering alone.
_SUBMISSION_PROBE = """(boxes) => {
    const select = document.querySelector('#assignment_submission_type');
    if (!select) return null;
    const label = document.querySelector('label[for="assignment_submission_type"]');
    const mode = select.value;
    let types = [];
    if (mode === 'online') {
        for (const [value, name] of Object.entries(boxes)) {
            const box = document.querySelector(
                `input[type=checkbox][name="${CSS.escape(name)}"]`);
            if (box && box.checked) types.push(value);
        }
    } else {
        types = [mode];
    }
    return {
        mode: mode,
        label: (label ? label.textContent : '').trim(),
        name: select.getAttribute('name') || '',
        options: Array.from(select.options).map(o => o.value),
        types: types.sort(),
        // Canvas hides the sub-type block until Online is chosen, so "did the
        // boxes appear" is part of readiness, not a detail.
        boxes_present: Object.values(boxes).filter(n => document.querySelector(
            `input[type=checkbox][name="${CSS.escape(n)}"]`)).length,
    };
}"""


#: Allowed Attempts is an **InstUI pair, not a Rails input** (recon
#: 2026-09-09) -- the opposite of `points_possible`, which sits inches away on
#: the same form and *is* addressable by a stable semantic id:
#:
#:   * the Unlimited/Limited `<select>` has **no `name` at all** and a
#:     RANDOMLY GENERATED id (`x4ziu7qhl` on the observed load), so neither
#:     handle survives a second page load;
#:   * the count box has a render-order counter id (`NumberInput___0`) but a
#:     stable `name="allowed_attempts"`.
#:
#: So the select is found by **the only content that identifies it** -- its
#: option values -- cross-checked against its label, and the count box by name.
#: This is the date-field discipline, applied on a form where two neighbouring
#: fields do not need it.
_ATTEMPTS_PROBE = """() => {
    const selects = Array.from(document.querySelectorAll('select')).filter(s => {
        const values = Array.from(s.options).map(o => o.value).sort();
        return values.length === 2
            && values[0] === 'limited' && values[1] === 'unlimited';
    });
    const box = document.querySelector('input[name="allowed_attempts"]');
    const labelOf = (el) => {
        if (!el) return '';
        if (el.id) {
            const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (l) return l.textContent.trim();
        }
        const wrap = el.closest('label');
        return wrap ? wrap.textContent.trim() : '';
    };
    const seen = (el) => !!(el && (el.offsetParent || el.getClientRects().length));
    return {
        // More than one match is not a thing to choose between: the same rule
        // as the ambiguous Save button, which returns nothing rather than guess.
        select_count: selects.length,
        select_label: labelOf(selects[0]),
        select_value: selects[0] ? selects[0].value : null,
        select_visible: seen(selects[0]),
        box_present: !!box,
        box_label: labelOf(box),
        box_value: box ? box.value : null,
        box_visible: seen(box),
    };
}"""

#: Selecting this takes the assignment out of the gradebook entirely and hides
#: its points and dates. Reported loudly rather than refused -- it is a real
#: thing a person may want -- but it is not a change to make by accident.
NOT_GRADED = "not_graded"

_FIELD_PROBE = """(selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const label = el.id
        ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const style = window.getComputedStyle(el);
    return {
        value: el.value ?? '',
        label: (label ? label.textContent : '').trim(),
        name: el.getAttribute('name') || '',
        tag: el.tagName.toLowerCase(),
        visible: !!(el.offsetParent || style.position === 'fixed'),
        options: el.tagName.toLowerCase() === 'select'
            ? Array.from(el.options).map(o => o.value) : null,
    };
}"""


def _same_number(a: str, b: str) -> bool:
    """Numeric equality for a points box, tolerating formatting.

    Canvas may echo `8.34` as `8.340`. Comparing as text would call a correct
    write a failure -- the mirror of the seconds bug, where comparing too
    precisely created a diff that could never be satisfied.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def _write_submission_types(page: Page, assignment_id: str, value: str) -> None:
    """Set the Submission Type select and its sub-type checkboxes.

    **One sheet cell, two kinds of control** -- unlike every field written so
    far. The cell names either a whole mode (`none`, `on_paper`) or the online
    sub-types, and choosing `online` is what makes the boxes meaningful.

    Ordered deliberately: the select goes first, because the sub-type block is
    **not in the DOM at all** while the mode is `none` (recon 2026-09-09). So
    the boxes are waited for as a *state* after the mode changes, never typed
    at blind or after a fixed sleep -- the mistake `FORM_READY` exists for.
    """
    wanted = sorted(p.strip() for p in value.split(",") if p.strip())
    mode = wanted[0] if wanted and wanted[0] in ("none", "on_paper") else "online"

    found = page.evaluate(_SUBMISSION_PROBE, ONLINE_TYPE_BOXES)
    if not found:
        snapshot = save_debug_snapshot(page, f"no-submission-{assignment_id}")
        raise WriteFailed(
            f"no Submission Type control ({SUBMISSION_SELECT}) on {page.url}. "
            f"Canvas's assignment form has changed and this must be re-checked "
            f"before any write. Snapshot: {snapshot}"
        )
    # The id existing and the id still meaning this are different claims.
    if found["label"].strip().lower() != "submission type" or found["name"] != "submission_type":
        snapshot = save_debug_snapshot(page, f"submission-label-{assignment_id}")
        raise WriteFailed(
            f"{SUBMISSION_SELECT} is labelled {found['label']!r} "
            f"(name={found['name']!r}), not 'Submission Type'. Refusing to "
            f"write into a control that may no longer be the one meant. "
            f"Snapshot: {snapshot}"
        )
    if mode not in (found.get("options") or ()):
        raise WriteRefused(
            f"assignment {assignment_id}: Submission Type has no option "
            f"{mode!r}. Canvas accepts {', '.join(found['options'])}. These are "
            f"the option VALUES, not the words on screen."
        )

    page.locator(SUBMISSION_SELECT).select_option(mode)

    if mode == "online":
        # Wait for the block to exist, rather than assuming the select's change
        # handler has already run. A fixed wait is never a readiness check.
        page.wait_for_function(
            """(boxes) => Object.values(boxes).every(n => document.querySelector(
                   `input[type=checkbox][name="${CSS.escape(n)}"]`))""",
            arg=ONLINE_TYPE_BOXES,
            timeout=10_000,
        )
        for env_value, box_name in ONLINE_TYPE_BOXES.items():
            box = page.locator(f'input[type=checkbox][name="{box_name}"]')
            should = env_value in wanted
            # `set_checked` rather than `click`: clicking toggles, so a box
            # already in the wanted state would be turned off by it.
            if box.is_checked() != should:
                box.set_checked(should)


def _write_allowed_attempts(page: Page, assignment_id: str, value: str) -> None:
    """Set Unlimited/Limited and, when limited, the count.

    `-1` is Canvas's own encoding for unlimited and is what the sheet carries,
    so it maps to the *select*, not to the number box -- typing `-1` into a
    count box would be writing a value the control does not mean.
    """
    wanted = int(float(value.strip()))
    mode = "unlimited" if wanted == -1 else "limited"

    found = page.evaluate(_ATTEMPTS_PROBE)
    if found["select_count"] > 1:
        snapshot = save_debug_snapshot(page, f"attempts-ambiguous-{assignment_id}")
        raise WriteFailed(
            f"{found['select_count']} Unlimited/Limited selects on {page.url}; "
            f"this control has no id or name to tell them apart, so refusing "
            f"rather than choosing one. Snapshot: {snapshot}"
        )
    if not found["select_count"] or not found["box_present"]:
        snapshot = save_debug_snapshot(page, f"no-attempts-{assignment_id}")
        raise WriteFailed(
            f"no Allowed Attempts control on {page.url}. Canvas's assignment "
            f"form has changed and this must be re-checked before any write. "
            f"Snapshot: {snapshot}"
        )
    # The control exists in the DOM even when Canvas has hidden it -- an
    # assignment set to "No Submission" has nothing to limit attempts of.
    # Writing into a hidden control is how a value gets set and then discarded
    # on save, which reads as success.
    if not found["select_visible"]:
        raise WriteRefused(
            f"assignment {assignment_id}: Canvas hides Allowed Attempts unless "
            f"the assignment accepts submissions. Set submission_types in the "
            f"same row (or in Canvas) before limiting attempts."
        )
    # Labels are checked as a prefix: this one is a WRAPPING label, so its text
    # is the caption run together with the option words --
    # "Allowed AttemptsUnlimitedLimited".
    if not found["select_label"].strip().lower().startswith("allowed attempts"):
        snapshot = save_debug_snapshot(page, f"attempts-label-{assignment_id}")
        raise WriteFailed(
            f"the Unlimited/Limited select on {page.url} is labelled "
            f"{found['select_label']!r}, not 'Allowed Attempts'. Refusing to "
            f"write into a control that may no longer be the one meant. "
            f"Snapshot: {snapshot}"
        )

    page.locator("select").filter(
        has=page.locator("option[value='unlimited']")).first.select_option(mode)

    if mode == "limited":
        # The count box only matters once Limited is chosen, and Canvas reveals
        # it in response -- so it is waited for as a state, never slept on.
        box = page.locator('input[name="allowed_attempts"]')
        box.wait_for(state="visible", timeout=10_000)
        box.click()
        box.fill("")
        box.type(str(wanted), delay=25)
        # Enter is never pressed on this form: it submits rather than
        # committing the field (proved live 2026-08-22).
        box.evaluate("element => element.blur()")


def apply_settings(
    page: Page,
    config: Config,
    course_id: str,
    assignment_id: str,
    changes: dict[str, str],
) -> list[Written]:
    """Set an assignment's non-date settings and save. **This writes.**

    `changes` maps infosheet column -> wanted value. **Every field is set on
    one form load and committed by one save**, exactly as `apply_changes` does
    with the three dates. Saving once per field would mean two page loads, two
    saves, and a window where the assignment holds half the edit.

    Structurally identical to `apply_changes`, deliberately: the gates below
    were each learned by being wrong on a live course, and a second write path
    that skipped any of them would re-learn them the same expensive way.

    1. the form must be genuinely ready (`FORM_READY`), never a fixed sleep;
    2. every typed value is read back out of the DOM *before* saving;
    3. Canvas's own field messages are read after saving.

    `verify_settings` then re-reads ENV, which is the fourth and final check.
    """
    _open_editor(page, config, course_id, assignment_id)
    wait_for_form(page)

    # **The override refusal carries across, and the reason is not obvious.**
    # `points_possible` is per-assignment, so the value itself has nothing to
    # do with any accommodation -- but saving this form submits every "Assign
    # to" date card, so writing points to an assignment carrying overrides can
    # still delete a student's accommodation date. Same hazard, reached from a
    # direction that looks unrelated.
    overrides = read_overrides(page)
    if overrides:
        raise WriteRefused(
            f"assignment {assignment_id} has {overrides} override(s). Saving "
            f"this form submits every date card, so writing its settings could "
            f"delete a student's accommodation date. Refusing."
        )

    written: list[Written] = []
    # **Submission type is set before allowed attempts, and that is a
    # dependency rather than a preference.** Canvas hides the attempts pair
    # entirely while the assignment accepts no submissions, so a row that turns
    # an assignment online *and* limits its attempts only works in that order.
    # Sorted explicitly: relying on the caller's dict order would make this
    # correct by accident, and the accident is one refactor from being untrue.
    order = {"submission_types": 0, "allowed_attempts": 1}
    for column, value in sorted(changes.items(), key=lambda kv: order.get(kv[0], 0)):
        if column == "allowed_attempts":
            _write_allowed_attempts(page, assignment_id, value)
            page.wait_for_timeout(150)
            landed = page.evaluate(_ATTEMPTS_PROBE)
            got = ("-1" if landed["select_value"] == "unlimited"
                   else (landed["box_value"] or ""))
            if not _same_number(got, value):
                snapshot = save_debug_snapshot(
                    page, f"attempts-reverted-{assignment_id}")
                raise WriteRefused(
                    f"assignment {assignment_id}: set Allowed Attempts to "
                    f"{value!r} but the form now holds {got!r}. Nothing was "
                    f"saved. Snapshot: {snapshot}"
                )
            written.append(
                Written(assignment_id=assignment_id, field=column,
                        wanted=value, typed=got, confirmed=""))
            continue

        if column == "submission_types":
            # Its own shape: a select plus five checkboxes, so it does not fit
            # the one-selector FormField table. Same three gates all the same.
            _write_submission_types(page, assignment_id, value)
            page.wait_for_timeout(150)
            landed = page.evaluate(_SUBMISSION_PROBE, ONLINE_TYPE_BOXES)
            got = ",".join((landed or {}).get("types") or [])
            if set(got.split(",")) != {p.strip() for p in value.split(",") if p.strip()}:
                snapshot = save_debug_snapshot(
                    page, f"submission-reverted-{assignment_id}")
                raise WriteRefused(
                    f"assignment {assignment_id}: set Submission Type to "
                    f"{value!r} but the form now holds {got!r}. Nothing was "
                    f"saved. Snapshot: {snapshot}"
                )
            written.append(
                Written(assignment_id=assignment_id, field=column,
                        wanted=value, typed=got, confirmed=""))
            continue

        field = FORM_FIELDS.get(column)
        if field is None:
            raise WriteRefused(
                f"{column!r} is not a writable field in this build. Refusing "
                f"rather than guessing which control it means."
            )

        found = page.evaluate(_FIELD_PROBE, field.selector)
        if not found or not found["visible"]:
            snapshot = save_debug_snapshot(page, f"no-{column}-{assignment_id}")
            raise WriteFailed(
                f"no visible {field.label!r} control ({field.selector}) on "
                f"{page.url}. Canvas's assignment form has changed and this "
                f"must be re-checked before any write. Snapshot: {snapshot}"
            )
        # The id existing is not the same claim as the id still meaning this.
        if (found["label"].strip().lower() != field.label.lower()
                or found["name"] != field.name):
            snapshot = save_debug_snapshot(page, f"{column}-label-{assignment_id}")
            raise WriteFailed(
                f"{field.selector} on {page.url} is labelled "
                f"{found['label']!r} (name={found['name']!r}), not "
                f"{field.label!r}. Refusing to write into a control that may "
                f"no longer be the one meant. Snapshot: {snapshot}"
            )

        if field.kind == "select":
            # Validated against the page's OWN options, not only the table
            # above: Canvas could add or drop one, and `select_option` on a
            # value that is not there raises deep in Playwright rather than
            # saying what was wrong.
            options = found.get("options") or ()
            if value not in options:
                raise WriteRefused(
                    f"assignment {assignment_id}: {field.label!r} has no option "
                    f"{value!r}. Canvas accepts {', '.join(options)}. Note "
                    f"these are the option VALUES, not the words on screen."
                )
            page.locator(field.selector).select_option(value)
        else:
            box = page.locator(field.selector)
            box.click()
            box.fill("")
            box.type(value, delay=25)
            # **Enter is never pressed on this form.** It submits rather than
            # committing the field -- proved live 2026-08-22, when a stray
            # Enter saved two assignments after the code had decided not to.
            box.evaluate("element => element.blur()")
        page.wait_for_timeout(150)

        # Gate 2: read it back before saving. A form that re-initialises after
        # the value was set saves cleanly and writes the OLD one.
        after_typing = page.evaluate(_FIELD_PROBE, field.selector)
        landed = (after_typing or {}).get("value")
        same = (_same_number(landed or "", value) if field.kind == "text"
                else landed == value)
        if not same:
            snapshot = save_debug_snapshot(page, f"{column}-reverted-{assignment_id}")
            raise WriteRefused(
                f"assignment {assignment_id}: set {field.label!r} to {value!r} "
                f"but the form now holds {landed!r}. Nothing was saved. "
                f"Snapshot: {snapshot}"
            )
        written.append(
            Written(assignment_id=assignment_id, field=column,
                    wanted=value, typed=value, confirmed="")
        )

    save = _save_button(page)
    if save is None:
        snapshot = save_debug_snapshot(page, f"no-save-button-{assignment_id}")
        raise WriteFailed(f"no Save button found on {page.url}. Snapshot: {snapshot}")
    save.click()

    # Gate 3: Canvas reports a rejected save only on the page. Both signals are
    # required together -- a successful save navigates away, and InstUI renders
    # ordinary hints through the same component as errors.
    #
    # Shares `_settle_after_save` with the date path deliberately: that helper
    # exists because a fixed post-save wait raced the navigation and crashed a
    # live push mid-course, and a second copy of the old pattern here would
    # wait to do the same thing on the settings path.
    complaints = _settle_after_save(page)
    if complaints:
        snapshot = save_debug_snapshot(page, f"settings-rejected-{assignment_id}")
        raise WriteFailed(
            f"Canvas refused the save for assignment {assignment_id}: "
            f"{'; '.join(complaints)}. Nothing was changed. "
            f"Snapshot: {snapshot}"
        )

    return written


def verify_settings(
    page: Page, config: Config, course_id: str, assignment_id: str,
    columns: tuple[str, ...],
) -> dict[str, str]:
    """Re-read settings straight from ENV after a save.

    **The Canvas UI is not evidence** -- a stale index render showed an old
    date for minutes after a write whose stored value was correct throughout.
    ENV is what the page itself was built from.
    """
    # **Formatted by the SAME function the sheet is built with.** `str()` was
    # used here, which is identical for a number or a string and wrong for a
    # list: `submission_types` came back `"['online_upload']"` and every write
    # of it reported MISMATCH while Canvas held exactly what was asked for
    # (live, 2026-09-09). Two implementations of "how an ENV value becomes a
    # cell" will eventually disagree, and the one on the verify path is the one
    # that decides whether a correct write is reported as a failure.
    from .assignments import _cell

    _open_editor(page, config, course_id, assignment_id)
    page.wait_for_function(SUBJECT_READY, timeout=30_000)
    subject = page.evaluate(f"() => {_SUBJECT}")
    return {column: _cell((subject or {}).get(column)) for column in columns}


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


def _settle_after_save(page: Page) -> list[str]:
    """Wait for the save to resolve, and return Canvas's field messages.

    **A state, never a duration.** The previous version clicked Save, waited a
    flat 2 seconds, then read `page.url` and evaluated in the page if it still
    said `/edit`. That raced the navigation: on a slow save the URL still read
    `/edit` at the 2s mark, the branch was entered, and Canvas navigated away
    *during* `page.evaluate`, which killed the run with

        Page.evaluate: Execution context was destroyed, most likely because
        of a navigation

    -- an unhandled exception, so `main()` let it propagate and the push
    stopped mid-course with some assignments written and some not. That is the
    "unexplained exit" first seen 2026-08-24 and finally caught on 2026-09-09,
    on a live push that had written 9 of 27 rows.

    Two outcomes are waited for by name:

    * navigation off `/edit` -- Canvas accepted the save;
    * a field message appearing -- Canvas rejected it and said why.

    Whichever happens first ends the wait, so a fast save is not slowed and a
    rejection is not missed. **The evaluate is still guarded**, because no
    amount of waiting closes the race completely: the navigation can always
    land in the microsecond after the check. A destroyed context there is not
    an error -- it is the page leaving, which is the success signal.
    """
    try:
        page.wait_for_function(
            f"() => !location.pathname.endsWith('/edit') || ({_FIELD_MESSAGES})().length",
            timeout=30_000,
        )
    except PlaywrightTimeout:
        # Neither happened: Canvas is simply slow, or the save silently did
        # nothing. Fall through and let the checks below decide -- ENV is
        # re-read afterwards regardless, so a stall cannot pass as a success.
        pass
    if "/edit" not in page.url:
        return []
    try:
        return page.evaluate(_FIELD_MESSAGES)
    except PlaywrightError as exc:
        if "Execution context was destroyed" in str(exc) or "navigation" in str(exc):
            return []
        raise


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
