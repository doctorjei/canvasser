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
from urllib.parse import urlparse

from playwright.sync_api import (Error as PlaywrightError, Page,
                                 TimeoutError as PlaywrightTimeout)

from .browser import save_debug_snapshot
from .config import Config
from .assignments import ENV_PROBE
from .dateparse import DateFormatError, resolve, zone_of
# **The same function the diff parses boolean cells with**, not a second one.
# Two implementations of "what does this cell say" would eventually disagree,
# and the one on the write path is the one that decides what a real setting is
# left as -- the lesson `verify_settings` learned by formatting a value its own
# way. `push` does not import `writer`, so this direction carries no cycle.
from .push import parse_flag

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
#: Canvas renders a rejected save in more than one shape, and matching only the
#: first one means a real refusal is read as silence.
#:
#: `formFieldMessage` is InstUI's wrapper and was all this matched until
#: 2026-09-10, when a live create was rejected with **"Please choose at least
#: one submission type"** and this returned nothing -- so the run fell through
#: and reported the assignment as possibly CREATED. That message is an InstUI
#: `Text` carrying `color="danger"` and a hashed class, with no
#: `formFieldMessage` anywhere above it. `.error_text`/`.errorBox` are the
#: classic Rails spellings, kept for the parts of the form that are not InstUI.
#:
#: **Still paired with "did we stay on the form"** by every caller, because
#: InstUI renders ordinary hints through these components too.
_FIELD_MESSAGES = """() => {
    const seen = new Set();
    const sel = '[class*="formFieldMessage"], [color="danger"],'
              + ' .error_text, .errorBox';
    document.querySelectorAll(sel).forEach(e => {
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


#: How many DISTINCT "Assign to" cards this assignment carries.
#:
#: **The two lists overlap, and this used to add them.** Canvas puts the same
#: overrides in `ENV.ASSIGNMENT.assignment_overrides` and in
#: `ENV.ASSIGNMENT_OVERRIDES`, so summing counts each one twice: an assignment
#: with a single override reported "2 override(s)" the first time this refusal
#: ever met real data (2026-09-10 -- no course had had one until one was made
#: for the purpose).
#:
#: The decision was never wrong, since any non-zero count refuses. Two other
#: things were: the message told the user a number that was not true, and it
#: **disagreed with the sheet's own `override_count`**, which reads only
#: `ENV.ASSIGNMENT_OVERRIDES` and said 1. Two implementations of "how many
#: overrides are there", differing in the one place a person compares them.
#:
#: Deduplicated by id. An entry with no id cannot be matched against another
#: list, so it is counted where it is found -- erring toward over-counting,
#: which errs toward refusing, which is the safe direction here.
_OVERRIDE_COUNT = f"""() => {{
    const lists = [{_SUBJECT}.assignment_overrides,
                   window.ENV?.ASSIGNMENT_OVERRIDES];
    const ids = new Set();
    let unidentified = 0;
    for (const list of lists) {{
        if (!Array.isArray(list)) continue;
        for (const card of list) {{
            if (!card) continue;
            if (card.id != null) ids.add(String(card.id));
            else unidentified++;
        }}
    }}
    return ids.size + unidentified;
}}"""


def read_overrides(page: Page) -> int:
    return page.evaluate(_OVERRIDE_COUNT)


def refuse_if_discussion(page: Page, assignment_id: str) -> None:
    """Refuse a graded discussion before the readiness gate can time out on it.

    **Reading one is supported; writing one is not** (built 2026-09-11).
    `/assignments/<id>/edit` redirects to `/discussion_topics/<t>/edit`, a React
    discussions app that shares no control with the form every other write here
    drives -- so there is nothing on the page for `_apply_field_values` or the
    date widget to find.

    Without this the failure is merely *slow and obscure*: `wait_for_form`
    hunts for a Save button that is not there for its full 90s, then raises
    about readiness, which sends the reader looking at the wrong thing. It also
    fires on BOTH sheets -- the datesheet carries no `kind` column at all, so
    this choke point is the only place that catches a date write.

    **The rule is not restated here.** `kind` comes from
    `assignments.ENV_PROBE`, the same probe `pull` reads the sheet with, so
    "what counts as a discussion" has one implementation. Two spellings of one
    question is the shape that has cost this project three bugs.

    A probe that cannot run does **not** refuse: nothing has been written at
    that point, and `wait_for_form` is still ahead to catch a page with no form
    on it. Failing closed here would mean refusing real assignments whenever an
    evaluate happened to race a navigation.
    """
    try:
        env = page.evaluate(ENV_PROBE) or {}
    except PlaywrightError:
        return
    if env.get("kind") != "discussion":
        return
    raise WriteRefused(
        f"assignment {assignment_id} is a GRADED DISCUSSION. Canvas edits "
        f"those on its discussions app ({page.url}), which shares no control "
        f"with the assignment form this tool drives, so nothing can be written "
        f"there -- not dates and not settings. `pull` reads it fine; change it "
        f"in Canvas by hand. Refusing."
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
    refuse_if_discussion(page, assignment_id)
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
    kind: str                         # "text", "select" or "checkbox"
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
    # **Addressed by name and type, because THE ID DIFFERS BETWEEN THE TWO
    # FORMS** -- found by the first live create, 2026-09-10:
    #
    #                   edit form                        create form
    #     id            #assignment_peer_reviews_checkbox #assignment_peer_reviews
    #     hidden twin   peer_reviews_hidden              peer_reviews (SAME name)
    #     widget        InstUI, facade over the input    classic Rails checkbox
    #
    # This was `#assignment_peer_reviews_checkbox`, on the recorded reasoning
    # that the id was safe *because* the hidden companion had a different name.
    # That reasoning was true and **true only of the edit form**: on the create
    # form the id does not exist at all, so the locator matched nothing and the
    # first live create was refused. "The create form IS the edit form" holds
    # for points, grading type and the submission controls; it stops here.
    #
    # `input[type=checkbox][name=...]` is the one handle both forms share -- and
    # it is the pattern the sub-type boxes already use, for the very situation
    # the create form turns out to have: a box and a hidden partner sharing one
    # name, told apart by type. More than one match is refused rather than
    # chosen, since on a form carrying both shapes there would be no way to say
    # which is meant.
    "peer_reviews": FormField(
        column="peer_reviews",
        selector='input[type=checkbox][name="peer_reviews"]',
        label="Require Peer Reviews",
        name="peer_reviews",
        kind="checkbox",
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
    const all = document.querySelectorAll(selector);
    const el = all[0];
    if (!el) return null;
    const label = el.id
        ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const style = window.getComputedStyle(el);
    return {
        // How many controls the selector matched. A `#id` can only ever match
        // one; an attribute selector can match several, and choosing between
        // them is the guess this project refuses everywhere else.
        count: all.length,
        // The element's OWN id, which is not derivable from the selector once
        // that selector stops being `#id` -- and `_write_checkbox` needs it to
        // find the label it must click.
        id: el.id || '',
        value: el.value ?? '',
        // **A checkbox's `value` is not its state** -- it reads `on` whether
        // ticked or not, so reading `value` here would call every write a
        // success. State comes from `checked`, and only from the checkbox
        // itself: Rails pairs each box with a hidden input that always says
        // off, which is why the selector must not be able to match one.
        checked: el.type === 'checkbox' ? el.checked : null,
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


def _write_checkbox(
    page: Page, assignment_id: str, field: FormField, want: bool, found: dict,
) -> None:
    """Tick or untick one checkbox, whatever is drawn on top of it.

    **An InstUI checkbox cannot be clicked** (live 2026-09-09). Its real
    `<input>` is present, visible and enabled -- and a sibling
    `checkboxFacade` span inside the wrapping `<label>` is painted over it, so
    every click lands on the facade instead. Playwright retried for the full
    30s and raised, and `peer_reviews` was the field that proved it:

        <span class="css-...-checkboxFacade__facade"> ... intercepts pointer events

    **The submission sub-type boxes, on the same form, take a plain
    `set_checked` and always have.** Those are classic Rails inputs; this one is
    a React component that only looks like its neighbours in the DOM. So this is
    the project's standing rule arriving on a control that had already been
    reconnoitred and called simple: **its attributes were simple and operating
    it is not.** Reading a control's markup says what it *is*, not how it takes
    input.

    **The facade is not an obstacle to route around** -- it is the thing a
    person clicks, and the label is what carries it. So the label is clicked,
    which fires the event chain React is listening for. `force=True` on the
    input was the other candidate and is worse: it bypasses the actionability
    checks that are the only evidence the control is operable, and a value set
    behind a component's back is one Canvas may never see.

    **Clicking toggles, so the state is read first and clicked only when it
    differs.** That keeps `set_checked`'s guarantee -- a box already in the
    wanted state is left alone, never turned off -- while operating the control
    the way the page intends.
    """
    if bool(found.get("checked")) == want:
        return  # already right; clicking here would turn it off
    # `label[for=...]` rather than the input: the facade lives inside the label,
    # and clicking the label is what a person does.
    #
    # **Found from the ELEMENT, not from the selector string.** This built
    # `label[for="{selector minus a leading #}"]`, which silently assumed every
    # checkbox is addressed by id -- and produced
    # `label[for="input[type=checkbox][name=..."]`, not a valid selector at all,
    # the moment `peer_reviews` moved to a name+type lookup to work on both
    # forms (2026-09-10). Deriving a second selector from the text of the first
    # is the kind of shortcut that holds until the first one changes shape.
    label = None
    element_id = (found or {}).get("id") or ""
    if element_id:
        by_for = page.locator(f'label[for="{element_id}"]')
        if by_for.count() == 1:
            label = by_for
    if label is None:
        # Both real forms wrap the input in its label, so this covers the case
        # where the probe's id is unavailable; a non-wrapping `label[for=...]`
        # is handled above.
        wrapping = page.locator(field.selector).locator("xpath=ancestor::label[1]")
        if wrapping.count() == 1:
            label = wrapping
    target = label if label is not None else page.locator(field.selector)
    try:
        target.click()
    except PlaywrightError as exc:
        # A control that cannot be operated is a WriteFailed with a snapshot,
        # not a traceback out of main(). An unhandled Playwright error here
        # stops the whole run mid-course, which is the failure this project has
        # already paid for once -- and `cmd_push_info` catches only the two
        # write exceptions, by design, so that a genuine bug is not swallowed.
        snapshot = save_debug_snapshot(page, f"{field.column}-stuck-{assignment_id}")
        raise WriteFailed(
            f"assignment {assignment_id}: could not operate the "
            f"{field.label!r} checkbox -- {exc}. Nothing was saved. "
            f"Snapshot: {snapshot}"
        ) from exc


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


def _apply_field_values(
    page: Page, subject: str, changes: dict[str, str]
) -> list[Written]:
    """Set every named settings field on a form that is already open and ready.

    **Shared by `apply_settings` and `create_assignment`**, which is the whole
    point of it being a function. The create form IS the edit form for
    assignments -- same ids, same five sub-type checkboxes, same InstUI attempts
    pair including its per-load random id (recon 2026-09-10) -- so a second copy
    of this loop would be a second place for a field to be added to one and not
    the other. That seam is not hypothetical: three parallel copies of the
    *comparison* rule are what made a correct `submission_types` write report
    MISMATCH on a live course.

    `subject` is used only for messages and snapshot labels, so a create can
    pass something that is not an id yet.

    It does **not** save. The caller owns the save and the checks around it,
    because those genuinely differ: an accepted edit navigates off `/edit`, an
    accepted create off `/new`.
    """
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
            _write_allowed_attempts(page, subject, value)
            page.wait_for_timeout(150)
            landed = page.evaluate(_ATTEMPTS_PROBE)
            got = ("-1" if landed["select_value"] == "unlimited"
                   else (landed["box_value"] or ""))
            if not _same_number(got, value):
                snapshot = save_debug_snapshot(
                    page, f"attempts-reverted-{subject}")
                raise WriteRefused(
                    f"assignment {subject}: set Allowed Attempts to "
                    f"{value!r} but the form now holds {got!r}. Nothing was "
                    f"saved. Snapshot: {snapshot}"
                )
            written.append(
                Written(assignment_id=subject, field=column,
                        wanted=value, typed=got, confirmed=""))
            continue

        if column == "submission_types":
            # Its own shape: a select plus five checkboxes, so it does not fit
            # the one-selector FormField table. Same three gates all the same.
            _write_submission_types(page, subject, value)
            page.wait_for_timeout(150)
            landed = page.evaluate(_SUBMISSION_PROBE, ONLINE_TYPE_BOXES)
            got = ",".join((landed or {}).get("types") or [])
            if set(got.split(",")) != {p.strip() for p in value.split(",") if p.strip()}:
                snapshot = save_debug_snapshot(
                    page, f"submission-reverted-{subject}")
                raise WriteRefused(
                    f"assignment {subject}: set Submission Type to "
                    f"{value!r} but the form now holds {got!r}. Nothing was "
                    f"saved. Snapshot: {snapshot}"
                )
            written.append(
                Written(assignment_id=subject, field=column,
                        wanted=value, typed=got, confirmed=""))
            continue

        field = FORM_FIELDS.get(column)
        if field is None:
            raise WriteRefused(
                f"{column!r} is not a writable field in this build. Refusing "
                f"rather than guessing which control it means."
            )

        found = page.evaluate(_FIELD_PROBE, field.selector)
        # **Three different failures, three different messages.** These were
        # one line reading "no visible <label> control", and on 2026-09-10 it
        # reported a control that was not *there* -- the peer-review id differs
        # between the create and edit forms -- as one that was not *visible*.
        # It sent the reader looking at what Canvas was hiding rather than at
        # the selector, which is the opposite of an actionable error.
        if not found:
            snapshot = save_debug_snapshot(page, f"no-{column}-{subject}")
            raise WriteFailed(
                f"nothing on {page.url} matches {field.selector} (the "
                f"{field.label!r} control). Either Canvas's form has changed, "
                f"or this control differs on this kind of form -- both need "
                f"re-checking before any write. Snapshot: {snapshot}"
            )
        if found["count"] > 1:
            snapshot = save_debug_snapshot(page, f"ambiguous-{column}-{subject}")
            raise WriteRefused(
                f"{field.selector} matches {found['count']} controls on "
                f"{page.url}. Refusing rather than choosing which one the "
                f"{field.label!r} setting means. Snapshot: {snapshot}"
            )
        if not found["visible"]:
            snapshot = save_debug_snapshot(page, f"hidden-{column}-{subject}")
            raise WriteFailed(
                f"the {field.label!r} control ({field.selector}) is on "
                f"{page.url} but is not visible, so anything set on it may be "
                f"discarded on save. Canvas usually hides a control because "
                f"another setting has not been made yet. Snapshot: {snapshot}"
            )
        # The id existing is not the same claim as the id still meaning this.
        if (found["label"].strip().lower() != field.label.lower()
                or found["name"] != field.name):
            snapshot = save_debug_snapshot(page, f"{column}-label-{subject}")
            raise WriteFailed(
                f"{field.selector} on {page.url} is labelled "
                f"{found['label']!r} (name={found['name']!r}), not "
                f"{field.label!r}. Refusing to write into a control that may "
                f"no longer be the one meant. Snapshot: {snapshot}"
            )

        if field.kind == "checkbox":
            want = parse_flag(value)
            if want is None:
                raise WriteRefused(
                    f"assignment {subject}: {field.column}={value!r} does "
                    f"not name a yes or a no. Refusing rather than guessing "
                    f"which way to leave a real setting."
                )
            _write_checkbox(page, subject, field, want, found)
        elif field.kind == "select":
            # Validated against the page's OWN options, not only the table
            # above: Canvas could add or drop one, and `select_option` on a
            # value that is not there raises deep in Playwright rather than
            # saying what was wrong.
            options = found.get("options") or ()
            if value not in options:
                raise WriteRefused(
                    f"assignment {subject}: {field.label!r} has no option "
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
        if field.kind == "checkbox":
            # Read from `checked`, not `value`: see `_FIELD_PROBE`. The value
            # recorded is the sheet's own vocabulary, which is what `pull`
            # writes and what the post-write check compares against.
            landed = "true" if (after_typing or {}).get("checked") else "false"
            same = parse_flag(landed) is parse_flag(value)
        else:
            landed = (after_typing or {}).get("value")
            same = (_same_number(landed or "", value) if field.kind == "text"
                    else landed == value)
        if not same:
            snapshot = save_debug_snapshot(page, f"{column}-reverted-{subject}")
            raise WriteRefused(
                f"assignment {subject}: set {field.label!r} to {value!r} "
                f"but the form now holds {landed!r}. Nothing was saved. "
                f"Snapshot: {snapshot}"
            )
        written.append(
            Written(assignment_id=subject, field=column,
                    wanted=value, typed=landed if field.kind == "checkbox" else value,
                    confirmed="")
        )
    return written


def apply_settings(
    page: Page,
    config: Config,
    course_id: str,
    assignment_id: str,
    changes: dict[str, str],
    kind: str = "",
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

    `kind` is the LIVE kind of the thing being edited (`assignment` or `quiz`)
    and is needed only for `title`, whose control is the one field on this form
    that differs between the two. It defaults to empty so every existing caller
    is unaffected; a `title` change without it refuses rather than guessing.
    """
    _open_editor(page, config, course_id, assignment_id)
    refuse_if_discussion(page, assignment_id)
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

    # **`title` does not go through `_apply_field_values`, and that is not a
    # style choice.** That loop is keyed by infosheet column and holds exactly
    # one control per column; title is the only field whose control depends on
    # the KIND of thing being edited. An assignment's box is `#assignment_name`
    # (name `name`, label `Assignment Name *`); a quiz's is `#quiz_title`
    # (name `quiz[title]`, label `Quiz Title *`). They share nothing -- unlike
    # the date widget, which is byte-identical on both forms, and unlike points
    # or grading type, which do not exist on a quiz page at all.
    #
    # Written FIRST, so a kind mismatch refuses before any other field has been
    # typed. Nothing is saved either way, but failing on the cheapest possible
    # state is the habit that keeps a half-configured form from ever existing.
    rest = dict(changes)
    new_title = rest.pop("title", None)
    written: list[Written] = []
    if new_title is not None:
        landed = _write_title(page, assignment_id, kind, new_title)
        written.append(
            Written(assignment_id=assignment_id, field="title",
                    wanted=new_title, typed=landed, confirmed="")
        )
    written += _apply_field_values(page, assignment_id, rest)

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


class CreateUnrecorded(Exception):
    """An assignment was created and its id could not be captured.

    **Deliberately not a `WriteFailed`.** `cmd_push_info` catches the two write
    exceptions and moves to the next row, which is right when nothing was
    written -- and catastrophic here: the assignment exists in Canvas, its sheet
    row still says `NEW`, and the next push would create it a second time. This
    must abort the run and say so.
    """


#: The title control. **On both forms and identical on neither** (recon
#: 2026-09-09), which is why this is a kind-aware table rather than one entry:
#:
#:              assignment            quiz
#:     id       #assignment_name      #quiz_title
#:     name     name                  quiz[title]
#:     label    Assignment Name *     Quiz Title *
#:
#: Compare the two extremes already known -- the date widget is byte-identical
#: on both forms, and points/grading/submission do not exist on a quiz at all.
#: Title is the case in between. Only the assignment entry is reachable today,
#: since quiz creation is not built; the quiz entry is here because the lookup
#: has to be kind-aware the moment it is, and a table with one row invites being
#: written as a constant.
TITLE_FIELDS = {
    "assignment": FormField(column="title", selector="#assignment_name",
                            label="Assignment Name", name="name", kind="text"),
    "quiz": FormField(column="title", selector="#quiz_title",
                      label="Quiz Title", name="quiz[title]", kind="text"),
}


def _label_matches(found: str, wanted: str) -> bool:
    """Whether a form label is the one meant, ignoring its required marker.

    Canvas renders the title label as `Assignment Name *`. An equality check
    against `Assignment Name` refuses every write -- the same shape as the
    `allowed_attempts` wrapping label, which reads
    `Allowed AttemptsUnlimitedLimited` and had to be matched as a prefix.
    """
    return found.strip().rstrip("*").strip().lower() == wanted.strip().lower()


def _write_title(page: Page, subject: str, kind: str, title: str) -> str:
    """Type the assignment's title, verifying the control first.

    Not routed through `_apply_field_values`: that table is keyed by infosheet
    column and holds one control per column, and title is the first field whose
    control **depends on the kind of thing being edited**.
    """
    field = TITLE_FIELDS.get(kind)
    if field is None:
        raise WriteRefused(
            f"no title control is known for kind={kind!r}. Refusing rather "
            f"than guessing which box to type a name into."
        )
    found = page.evaluate(_FIELD_PROBE, field.selector)
    if not found:
        snapshot = save_debug_snapshot(page, f"no-title-{subject}")
        raise WriteFailed(
            f"nothing on {page.url} matches {field.selector} (the "
            f"{field.label!r} control). This lookup is kind-aware -- the two "
            f"forms share nothing here -- so check that kind={kind!r} is "
            f"right for this page. Snapshot: {snapshot}"
        )
    if not found["visible"]:
        snapshot = save_debug_snapshot(page, f"hidden-title-{subject}")
        raise WriteFailed(
            f"the {field.label!r} control ({field.selector}) is on {page.url} "
            f"but is not visible. Snapshot: {snapshot}"
        )
    # The id existing and the id still meaning this are different claims.
    if not _label_matches(found["label"], field.label) or found["name"] != field.name:
        snapshot = save_debug_snapshot(page, f"title-label-{subject}")
        raise WriteFailed(
            f"{field.selector} on {page.url} is labelled {found['label']!r} "
            f"(name={found['name']!r}), not {field.label!r}. Refusing to type "
            f"a title into a control that may no longer be the one meant. "
            f"Snapshot: {snapshot}"
        )

    box = page.locator(field.selector)
    box.click()
    box.fill("")
    box.type(title, delay=25)
    # **Enter is never pressed on this form**, here as everywhere: it submits
    # rather than committing the field.
    box.evaluate("element => element.blur()")
    page.wait_for_timeout(150)

    # **This read-back is also what enforces the form's own length limit**, and
    # deliberately so. Only the quiz form declares one (`maxlength=254`), so a
    # length pre-check at diff time would have to invent a limit for
    # assignments and would refuse a legal write -- the same reasoning that
    # leaves term bounds unchecked. The browser truncates an over-long value at
    # the attribute, so the box then holds something other than what was asked
    # for and this refuses, naming both. No separate check, and nothing
    # guessed: the form's own declaration is the authority.
    landed = (page.evaluate(_FIELD_PROBE, field.selector) or {}).get("value")
    if landed != title:
        snapshot = save_debug_snapshot(page, f"title-reverted-{subject}")
        raise WriteRefused(
            f"set {field.label!r} to {title!r} but the form now holds "
            f"{landed!r}. Nothing was saved. Snapshot: {snapshot}"
        )
    # Returned so a caller can record what the DOM actually held rather than
    # what it asked for, as every other field's `Written` record does.
    return landed


#: The assignment-group `<select>`, which **carries its own name->id map**
#: (recon 2026-09-10): every option is `value=<group id>`, `text=<group name>`.
#: That is what makes `assignment_group` writable on a create at all -- the
#: sheet carries the NAME and Canvas wants the ID, the same value-vs-label trap
#: as `grading_type`, and here the page hands over the translation.
#:
#: Located by `name`, not by a constructed id: the name is what the recon
#: recorded, and the options are cross-checked for being numeric so a select
#: that merely shares the name cannot be typed into.
_GROUP_PROBE = """() => {
    const select = document.querySelector('select[name="assignment_group_id"]');
    if (!select) return null;
    const label = select.id
        ? document.querySelector(`label[for="${CSS.escape(select.id)}"]`) : null;
    const wrap = select.closest('label');
    return {
        label: ((label ? label.textContent : (wrap ? wrap.textContent : '')) || '').trim(),
        visible: !!(select.offsetParent),
        options: Array.from(select.options).map(o => ({
            value: o.value, text: (o.textContent || '').trim()})),
    };
}"""

#: The `[ Create Group ]` option. Selecting it would make a group as a
#: side-effect of creating an assignment -- a decision to take deliberately,
#: not to discover, so a sheet naming an unknown group is refused instead.
CREATE_GROUP_OPTION = "new"


def _write_group(page: Page, subject: str, name: str) -> None:
    """Put the new assignment in a named group, resolving the name on the page."""
    found = page.evaluate(_GROUP_PROBE)
    if not found or not found["visible"]:
        snapshot = save_debug_snapshot(page, f"no-group-{subject}")
        raise WriteFailed(
            f"no visible assignment-group control on {page.url}. Canvas's "
            f"create form has changed and this must be re-checked before any "
            f"write. Snapshot: {snapshot}"
        )

    real = [o for o in found["options"] if o["value"] != CREATE_GROUP_OPTION]
    wanted = name.strip().casefold()
    matches = [o for o in real if o["text"].casefold() == wanted]
    if len(matches) > 1:
        # Two groups with one name is Canvas's to allow and not ours to pick
        # between -- the same rule as the ambiguous Save button.
        raise WriteRefused(
            f"assignment_group={name!r} matches {len(matches)} groups in this "
            f"course. Refusing rather than choosing; rename one in Canvas."
        )
    if not matches:
        raise WriteRefused(
            f"assignment_group={name!r} is not a group in this course. "
            f"Canvas has: {', '.join(o['text'] for o in real) or '(none)'}. "
            f"Create the group in Canvas first -- this build will not make one "
            f"as a side effect of creating an assignment."
        )

    page.locator('select[name="assignment_group_id"]').select_option(matches[0]["value"])
    page.wait_for_timeout(150)
    after = page.evaluate(_GROUP_PROBE)
    chosen = next((o for o in (after or {}).get("options", [])
                   if o["value"] == matches[0]["value"]), None)
    if chosen is None:
        raise WriteRefused(
            f"the assignment-group control lost the option for {name!r} after "
            f"it was chosen. Nothing was saved."
        )


#: Where Canvas lands after it accepts a new assignment: the assignment's own
#: page, which is the only place the id it just minted appears.
_CREATED_PATH = re.compile(r"/assignments/(?P<id>\d+)")


def create_assignment(
    page: Page,
    config: Config,
    course_id: str,
    kind: str,
    title: str,
    values: dict[str, str],
    publish: bool = False,
) -> tuple[str, list[Written], dict[str, str]]:
    """Create one assignment. Returns `(new id, what was set, what was inherited)`.

    **This writes.**

    `publish` is not in `values` because it is not a control on the form: it
    decides whether the form is submitted with "Save" or with "Save & Publish".
    Defaults to False, so an omitted column creates an assignment students
    cannot see -- the recoverable direction, since publishing later is a click
    and unpublishing something students have already seen is not.

    **The first operation here that is not an edit**, and the first that is not
    idempotent -- which is why the caller must record the returned id in the
    sheet before doing anything else. See `infosheet.claim_new_row`.

    The same discipline as the two edit paths, because each gate was learned by
    being wrong on a live course: the form must be genuinely ready, every value
    is read back out of the DOM before saving, and Canvas's own field messages
    are read after. There is **no override refusal**, and that is not an
    oversight: a form that has never been saved has no date cards to submit, so
    the hazard those refusals exist for does not exist yet.
    """
    page.goto(f"{config.base_url}/courses/{course_id}/assignments/new",
              wait_until="domcontentloaded")

    # **Canvas pre-fills this form from the LAST assignment created in this
    # browser, and that has to be cleared or the sheet is not the source of
    # truth.** Measured 2026-09-10: `localStorage` holds
    # `_<user>_course_<course>_new_assignment_settings`, e.g.
    # `{"grading_type":"percent","submission_type":"external_tool",
    #   "points_possible":10,...}`, and the form loads from it.
    #
    # Proved by comparing the same session against a FRESH browser profile: it
    # showed Canvas's real defaults (`0` / `points` / `online`) where the
    # working profile showed a previous create's values. It is Canvas's own
    # convenience feature -- sensible for a person making five similar
    # assignments by hand -- and wrong here, because this tool keeps ONE
    # long-lived profile for its session cookies, so the state persists across
    # runs and across courses' sheets.
    #
    # Left alone, a `NEW` row that omits `points_possible` does not get 0: it
    # silently gets whatever was created last. Clearing costs one reload and
    # makes an omitted column mean exactly what the documentation says it
    # means. Nothing a person uses is affected -- this profile is headless and
    # lives in the state directory.
    forgotten = page.evaluate(_FORGET_REMEMBERED, course_id)
    if forgotten:
        page.reload(wait_until="domcontentloaded")

    # The create form is the edit form, so it has the same two-part readiness:
    # the submit button goes live before the Assign-To date card mounts.
    wait_for_form(page)

    _write_title(page, "new", kind, title)
    group = values.get("assignment_group")
    if group:
        _write_group(page, "new", group)

    # `kind` chose the endpoint and `assignment_group` has its own control;
    # everything else is an ordinary settings field on the same form.
    rest = {c: v for c, v in values.items()
            if c not in ("title", "kind", "assignment_group")}
    written = _apply_field_values(page, "new", rest)

    # **What the form holds for everything the sheet did NOT set.**
    #
    # Measured 2026-09-10, and it is not what these notes assumed: the create
    # form arrives **pre-filled from the persistent browser profile**, not at
    # Canvas's documented defaults. The same session against a *fresh* profile
    # shows points `0` / `points` / `online`; against the profile this tool has
    # been using it showed `10` / `percent` / `external_tool` -- the values a
    # previous create left behind.
    #
    # So "the sheet may omit a field and Canvas will default it" is FALSE here.
    # An omitted field takes whatever the profile last held, which is a silent
    # wrong value on a brand-new assignment. The values are captured before the
    # save so the caller can report them: this cannot be refused (they are
    # legal values, and the user may well want them) and it must not be
    # invisible.
    inherited = _unset_field_values(page, set(rest))
    return _finish_create(page, title, written, inherited, publish=publish)


#: Drops Canvas's remembered "new assignment settings" for one course.
#:
#: Matched on the SUFFIX, not built as a whole key: the real name embeds the
#: Canvas user id (`_386071_course_169156_new_assignment_settings`), which is
#: not otherwise needed here and would be one more thing to read correctly.
#: Scoped to the course being written, so another course's remembered settings
#: are left alone -- this is Canvas's feature and only the part that would
#: contaminate *this* create is removed.
_FORGET_REMEMBERED = """(courseId) => {
    const suffix = `_course_${courseId}_new_assignment_settings`;
    const hit = Object.keys(localStorage).filter(k => k.endsWith(suffix));
    hit.forEach(k => localStorage.removeItem(k));
    return hit;
}"""

#: What the create form holds for the fields a `NEW` row left blank, so they can
#: be reported rather than assumed. Keyed by infosheet column.
_INHERITED_PROBE = """() => {
    const v = (s) => { const e = document.querySelector(s); return e ? e.value : null; };
    const attempts = Array.from(document.querySelectorAll('select')).filter(s => {
        const o = Array.from(s.options).map(x => x.value).sort();
        return o.length === 2 && o[0] === 'limited' && o[1] === 'unlimited'; });
    return {
        points_possible: v('#assignment_points_possible'),
        grading_type: v('#assignment_grading_type'),
        submission_types: v('#assignment_submission_type'),
        allowed_attempts: attempts.length && attempts[0].value === 'unlimited'
            ? '-1' : v('input[name="allowed_attempts"]'),
    };
}"""


def _unset_field_values(page: Page, specified: set[str]) -> dict[str, str]:
    """The form's current values for columns the sheet did not name.

    Read *before* saving, so the report describes what is about to be created
    rather than what was found afterwards -- and so a caller could refuse on it
    later without a second page load, if that is ever wanted.
    """
    try:
        holding = page.evaluate(_INHERITED_PROBE) or {}
    except PlaywrightError:
        return {}
    return {column: value for column, value in holding.items()
            if column not in specified and value not in (None, "")}


def _finish_create(
    page: Page, title: str, written: list[Written], inherited: dict[str, str],
    publish: bool = False,
) -> tuple[str, list[Written], dict[str, str]]:
    """Save the create form, and return the new id with what was set.

    **`publish` chooses the button, and that is the whole mechanism.** There is
    no publish control on an assignment form -- the 2026-09-09 recon looked for
    one and found zero matching inputs, selects *and* buttons -- so publishing
    is not a field this could set. Canvas offers a second submit instead.
    """

    # Looked up BEFORE anything is clicked, so a missing publish button costs a
    # page load and not a half-made assignment. Falling back to the plain Save
    # would be worse than failing: it creates the assignment unpublished while
    # reporting success, which is the "no-op that looks like a write" this
    # project keeps meeting -- except here it leaves a real object behind, and
    # deleting is unbuilt.
    wanted = "Save & Publish" if publish else "Save"
    save = _save_and_publish_button(page) if publish else _save_button(page)
    if save is None:
        snapshot = save_debug_snapshot(page, "no-save-button-new")
        raise WriteFailed(
            f"no unambiguous {wanted!r} button found on {page.url}. Nothing "
            f"was created."
            + (" Canvas offers 'Save & Publish' only while an assignment is "
               "unpublished; if this course or form does not show it, create "
               "the row with published blank and publish it in Canvas."
               if publish else "")
            + f" Snapshot: {snapshot}"
        )
    save.click()

    # `/new`, not `/edit`: this form's own path. Passing the default would end
    # the wait instantly and read a rejected create as a successful one.
    complaints = _settle_after_save(page, on_form="/new")
    if complaints:
        snapshot = save_debug_snapshot(page, "create-rejected")
        raise WriteFailed(
            f"Canvas refused to create {title!r}: {'; '.join(complaints)}. "
            f"Nothing was created. Snapshot: {snapshot}"
        )

    # **Still on the create form means the save did not happen.** Canvas
    # navigates away when it accepts one, so this is a rejection whose message
    # was not recognised -- and it must NOT fall through to `_created_id`,
    # which would find no id and report the assignment as probably CREATED.
    #
    # That is exactly what happened on 2026-09-10: Canvas rejected a title-only
    # create with "Please choose at least one submission type", the probe did
    # not match that shape, and the run halted claiming an object existed when
    # none did. The scariest message this tool can print, printed wrongly.
    if urlparse(page.url).path.endswith("/new"):
        snapshot = save_debug_snapshot(page, "create-not-accepted")
        raise WriteFailed(
            f"Canvas did not accept {title!r}: after saving, the browser is "
            f"still on the create form, so nothing was created. Canvas "
            f"rejects a save without saying why in a shape this build "
            f"recognises -- read the snapshot to see the form's own message. "
            f"Snapshot: {snapshot}"
        )

    # The third element is what the columns the sheet left BLANK actually
    # ended up as. With the remembered settings cleared these are Canvas's own
    # defaults -- but they are reported rather than assumed, because that
    # assumption is exactly what was wrong before, and a report is also the
    # cheapest evidence that the clearing worked.
    return _created_id(page, title), written, inherited


def _created_id(page: Page, title: str) -> str:
    """The id Canvas minted, from the page it landed on.

    **The one place in this codebase where failing to read something is worse
    than the write failing.** By the time this runs the assignment exists; if
    its id cannot be captured, the sheet row still says `NEW` and the next push
    creates a duplicate. So both available sources are tried, and the error says
    plainly what state Canvas is in.
    """
    found = _CREATED_PATH.search(urlparse(page.url).path)
    if found:
        return found.group("id")

    # ENV is the fallback rather than the primary because a create can land on
    # a page whose ENV has not populated yet, while the URL is settled the
    # moment the navigation completes.
    try:
        from_env = page.evaluate(
            "() => { const a = window.ENV && (window.ENV.ASSIGNMENT || window.ENV.QUIZ);"
            " return a && a.id != null ? String(a.id) : ''; }")
    except PlaywrightError:
        from_env = ""
    if from_env:
        return from_env

    snapshot = save_debug_snapshot(page, "created-id-unknown")
    raise CreateUnrecorded(
        f"{title!r} appears to have been CREATED in Canvas, but its new id "
        f"could not be read from {page.url}. The sheet still says NEW for that "
        f"row, so pushing again would create it a SECOND time. Re-run "
        f"`canvasser pull` to get the real id before pushing this sheet again. "
        f"Snapshot: {snapshot}"
    )


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
    from .assignments import ENV_PROBE, _cell

    _open_editor(page, config, course_id, assignment_id)
    page.wait_for_function(SUBJECT_READY, timeout=30_000)
    # **Read through the SAME probe the sheet is built from, not off the raw
    # ENV object**, because the two do not share every key. `title` is the one
    # that bites: an assignment carries `ENV.ASSIGNMENT.name` and a quiz
    # carries `ENV.QUIZ.title`, so the sheet reads it as
    # `subject.name || subject.title` -- and asking the raw object for `title`
    # returns nothing at all for an assignment. A correct rename would then be
    # reported as MISMATCH against an empty string, and `cmd_push_info` would
    # tell the user Canvas "holds '', which is neither the old nor the new
    # value" about a write that had landed perfectly.
    #
    # That is the 2026-09-09 `submission_types` seam exactly -- two
    # implementations of "how an ENV value becomes a cell", one on the read
    # path and one on the verify path, agreeing until they did not. `_cell` was
    # already shared for the same reason; this shares the key resolution too,
    # so there is one implementation rather than two that must be kept level.
    subject = page.evaluate(ENV_PROBE) or {}
    return {column: _cell(subject.get(column)) for column in columns}


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


def _settle_after_save(page: Page, on_form: str = "/edit") -> list[str]:
    """Wait for the save to resolve, and return Canvas's field messages.

    `on_form` is the path suffix that means *we are still on the form*. It is a
    parameter because **the create form is at `/assignments/new`, not `/edit`**,
    and the default would make this return instantly there: `/new` does not end
    with `/edit`, so the wait would be satisfied before the save resolved and a
    Canvas rejection would be read as a clean create. That is this function's
    own bug -- a check that races the navigation -- reappearing on the one path
    that was not written yet.

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
            f"(suffix) => !location.pathname.endsWith(suffix) "
            f"|| ({_FIELD_MESSAGES})().length",
            arg=on_form,
            timeout=30_000,
        )
    except PlaywrightTimeout:
        # Neither happened: Canvas is simply slow, or the save silently did
        # nothing. Fall through and let the checks below decide -- ENV is
        # re-read afterwards regardless, so a stall cannot pass as a success.
        pass
    # Compared on the parsed path, matching the wait above. A substring test
    # over the whole URL would disagree with it for a query string mentioning
    # the suffix, and the two must reach the same answer.
    if not urlparse(page.url).path.endswith(on_form):
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


def _save_and_publish_button(page: Page):
    """The form's one visible "Save & Publish" control, or None.

    **Measured, not assumed** (`archives/recon-scripts/publish_button_recon.py`,
    2026-09-10, Temple's TEMPLATE course): a `<button class="btn btn-default
    save_and_publish">` with **no id**, `type="button"`, sitting inside the form
    beside the ordinary Save. Present on `/assignments/new` **and** on the edit
    form of an assignment that is still unpublished.

    That probe exists because the note claiming this button was on "the create
    form" came from a probe of `/assignments/new?quiz_lti` -- a different page,
    by these notes' own insistence -- and was about to be coded against the
    plain create form. It is the `peer_reviews` mistake in its exact shape: a
    control seen on one form, assumed on another. It happened to be true here.
    It was still worth one page load to know rather than to hope.

    **Matched on the button's text, with the class as corroboration only.** The
    class is the more specific handle and the more brittle one -- Canvas
    restyles -- while the words are what a person clicks and what the
    accessible name reports. This mirrors `_save_button` deliberately: two
    lookups on the same page that disagreed about how to find a button would be
    two implementations of one question, which is the shape that has cost this
    project three bugs.

    **More than one match returns None rather than choosing.** Same rule as the
    Save button, and it matters more here: picking wrong publishes something to
    students.
    """
    found = page.locator("button:visible, input[type=submit]:visible").filter(
        has_text=re.compile(r"^\s*save\s*&\s*publish\s*$", re.I)
    )
    count = found.count()
    if count == 1:
        return found.first
    if count > 1:
        by_role = page.get_by_role("button", name="Save & Publish", exact=True)
        return by_role.first if by_role.count() == 1 else None
    return None
