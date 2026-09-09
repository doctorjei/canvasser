"""Comparing an edited sheet against what Canvas currently holds.

This module is the *diff*, for **both sheets**: `compare` for the datesheet and
`compare_info` for the infosheet. It answers "what would change?" and nothing
more -- no navigation to an edit form, no writes. That separation is
deliberate: the diff is the thing that gets run over and over while a sheet is
being edited, and it must never be able to touch the course.

**The two diffs share a shape and almost no rules.** Row identity is a pair
here and a single id there; there is no timezone to align on the infosheet; and
an empty cell means "clear it" for a date but "leave it alone" for a setting.
The infosheet half lives at the bottom of this file, under its own banner.
Everything down to that banner is dates.

The first test this exists to serve is the **no-op round trip**: pull a course,
push it back unedited, and see zero changes. Anything that survives that is a
formatting bug in the sheet, not an edit -- exactly the class of error that
would otherwise be discovered by writing wrong dates into a live class.

## What counts as a change (datesheet)

Only the six editable date columns. `title` and `assign_to` are carried for
human orientation and are deliberately ignored here: renaming a row in a
spreadsheet must never retarget or trigger a write.

Comparison is on the **rendered strings**, not parsed instants, because that is
what the round trip has to be stable in. `23:59` and `23:59:00` mean the same
moment but are not the same cell, and a diff that called them equal would hide
a real formatting drift in `pull`.

That only works if both sides are in the same zone, which is what
`align_timezone` guarantees before anything is compared: a sheet whose declared
zone differs from the course's is converted into course time first. That zone
declares what the sheet's wall clocks *mean*, so a disagreement is a
conversion, not an error.

The zone comes from `iana=` when present, and otherwise from the friendly
`timezone=` label -- see `sheet_zone`.

## What else this module refuses before a page is ever loaded

`check_order` applies Canvas's own date-ordering rule to the *result* of the
push -- the mixture of sheet values and whatever Canvas already holds. Canvas
enforces it server-side and reports it only on the page, so catching it here
saves an edit-page load per row for a save that cannot succeed, and names the
cell to fix instead of reporting a bare mismatch afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .dateparse import convert
from .datesheet import AssignmentRow, Sheet, SheetError
from .infosheet import (EDITABLE_COLUMNS as INFO_EDITABLE, RENAME_GATED,
                        InfoRow, InfoSheet)
from .timezones import resolve_friendly

#: The three date pairs, grouped for reporting. Canvas's field name first,
#: because that is what the sheet's row 2 labels them and what a later write
#: will actually set.
FIELD_PAIRS = (
    ("unlock_at", "open_date", "open_time"),
    ("due_at", "due_date", "due_time"),
    ("lock_at", "close_date", "close_time"),
)


@dataclass(frozen=True)
class FieldChange:
    field: str          # Canvas's name: unlock_at / due_at / lock_at
    before: str         # what Canvas holds now
    after: str          # what the sheet asks for

    @property
    def is_clear(self) -> bool:
        """The edit empties a date Canvas currently has."""
        return bool(self.before) and not self.after

    @property
    def is_set(self) -> bool:
        """The edit fills a date Canvas currently leaves empty."""
        return not self.before and bool(self.after)


@dataclass(frozen=True)
class RowDiff:
    key: tuple[str, str]
    title: str
    assign_to: str
    changes: list[FieldChange]


@dataclass(frozen=True)
class Diff:
    """Everything that differs between a sheet and the live course."""

    changed: list[RowDiff]
    #: In the sheet but no longer on Canvas -- the assignment or override was
    #: deleted after the pull. Worth saying loudly: a write keyed on it would
    #: silently do nothing, so the user would think an edit landed when it did
    #: not.
    missing: list[AssignmentRow]
    #: On Canvas but absent from the sheet. **Normal, not a problem.** Deleting
    #: rows you do not want to touch is a supported way to narrow a push, so
    #: these are reported as "left alone" rather than as something wrong.
    untouched: list[AssignmentRow]
    compared: int

    @property
    def is_empty(self) -> bool:
        """Whether a push would write anything.

        Only `changed` counts. Rows absent from the sheet are deliberately
        untouched, and rows the sheet has that Canvas has lost cannot be
        written -- neither is a pending change, and treating them as one made a
        deliberately-narrowed sheet look like it had edits waiting.
        """
        return not self.changed

    @property
    def field_count(self) -> int:
        return sum(len(row.changes) for row in self.changed)


def _joined(row: AssignmentRow, date_column: str, time_column: str) -> str:
    """A date pair as one human string, for reporting. Empty stays empty."""
    date = getattr(row, date_column)
    time = getattr(row, time_column)
    return " ".join(part for part in (date, time) if part)


def to_minute(value: str) -> str:
    """Drop the seconds. **Seconds are not settable, so they are not compared.**

    Proved live: an assignment at `22:59:59` was asked for `22:59:00`; Canvas
    took the minute and kept its own `:59`, leaving `22:59:59`. The edit form's
    time box has minute granularity -- there is nowhere to type a second.

    Comparing at second precision therefore creates a diff that can never be
    satisfied: the sheet says one thing, Canvas insists on another, and every
    subsequent push rewrites and fails again. Truncating here makes the
    comparison describe what is actually achievable.

    `pull` still records the true seconds, so the sheet stays faithful to
    Canvas and a no-op round trip is still byte-identical.
    """
    head, _, tail = value.partition(" ")
    if not tail:
        return value
    return f"{head} {tail[:5]}" if len(tail) > 5 else value


def compare(sheet: Sheet, current: list[AssignmentRow]) -> Diff:
    """Diff an edited sheet against freshly pulled rows.

    `current` must come from a pull of the same course, done *now* -- the whole
    point is to compare against what Canvas holds at the moment of writing, not
    against what it held when the sheet was made.
    """
    live = {row.key: row for row in current}
    seen: set[tuple[str, str]] = set()

    changed: list[RowDiff] = []
    missing: list[AssignmentRow] = []

    for wanted in sheet.rows:
        seen.add(wanted.key)
        have = live.get(wanted.key)
        if have is None:
            missing.append(wanted)
            continue

        changes = []
        for field, date_column, time_column in FIELD_PAIRS:
            # A column the sheet does not carry is not an instruction to clear
            # the field -- it is an instruction to leave it alone. Only a
            # column that is present, with an empty cell, means "clear it".
            # Collapsing the two would let deleting a column wipe every date
            # in it, which is exactly the silent destruction push exists to
            # avoid.
            if not (sheet.specifies(date_column) or sheet.specifies(time_column)):
                continue
            before = _joined(have, date_column, time_column)
            after = _joined(wanted, date_column, time_column)
            if to_minute(before) != to_minute(after):
                changes.append(FieldChange(field=field, before=before, after=after))
        if changes:
            changed.append(
                RowDiff(
                    key=wanted.key,
                    # Canvas's title wins over the sheet's, for two reasons:
                    # the sheet may not carry a title column at all now that
                    # only assignment_id is required, and if it does carry one
                    # that the user renamed, the live name is the one that
                    # identifies what is about to change.
                    title=have.title or wanted.title or f"assignment {wanted.key[0]}",
                    assign_to=have.assign_to or wanted.assign_to,
                    changes=changes,
                )
            )

    untouched = [row for key, row in live.items() if key not in seen]
    return Diff(
        changed=changed,
        missing=missing,
        untouched=untouched,
        compared=len(sheet.rows),
    )


#: Canvas's ordering rule for the three dates, and the wording it rejects with.
#: Equal values are fine -- a course really does set due and until to the same
#: instant -- so only strictly-before is an error.
DATE_ORDER = (
    ("unlock_at", "due_at", "Due date cannot be before unlock date"),
    ("due_at", "lock_at", "Until date cannot be before due date"),
    ("unlock_at", "lock_at", "Until date cannot be before unlock date"),
)


def _effective(row: AssignmentRow, live: AssignmentRow | None, sheet: Sheet) -> dict:
    """What each date would be *after* the push, as a comparable string.

    The sheet may name only some of the three. A column it does not carry
    keeps whatever Canvas holds, so ordering has to be judged on the mixture --
    checking the sheet alone would miss a new due date that lands after an
    existing until date, which is the commonest way to trip this.
    """
    out = {}
    for field, date_column, time_column in FIELD_PAIRS:
        if sheet.specifies(date_column) or sheet.specifies(time_column):
            source = row
        elif live is not None:
            source = live
        else:
            continue
        out[field] = to_minute(_joined(source, date_column, time_column))
    return out


def check_order(sheet: Sheet, current: list[AssignmentRow]) -> list[str]:
    """Rows Canvas will refuse because their dates are out of order.

    **Found the hard way, 2026-08-22.** Four rows of a full-class push reported
    a bare "MISMATCH" after the write; the cause was that each asked for an
    until date *before* its due date. Canvas rejected every one with "Until
    date cannot be before due date", re-rendered the form unchanged, and the
    post-write check dutifully reported that nothing had changed -- without
    ever mentioning the error Canvas was displaying.

    Catching it here costs nothing: it is arithmetic on values already in hand,
    it names the cell to fix, and it saves loading an edit page per row to
    attempt a save that cannot succeed.
    """
    live = {row.key: row for row in current}
    problems = []
    for row in sheet.rows:
        dates = _effective(row, live.get(row.key), sheet)
        for earlier, later, complaint in DATE_ORDER:
            first, second = dates.get(earlier), dates.get(later)
            if first and second and second < first:
                problems.append(
                    f"{row.assignment_id}: {later} {second} is before "
                    f"{earlier} {first} -- Canvas refuses this "
                    f"({complaint!r})"
                )
    return problems


def check_course(sheet: Sheet, course_id: str) -> None:
    """Refuse a sheet that came from a different course.

    Without this the assignment ids simply would not match and the diff would
    report "everything missing" -- a far worse failure than being told the
    sheet belongs elsewhere, because it looks like data loss.
    """
    if sheet.course_id and sheet.course_id != course_id:
        raise SheetError(
            f"{sheet.path} was pulled from course {sheet.course_id}, but this "
            f"run targets {course_id}. Refusing: pushing a sheet into the wrong "
            f"course would write one class's dates onto another."
        )


@dataclass(frozen=True)
class Realignment:
    """A record of converting a sheet's times into the course's zone."""

    source: str      # the zone the sheet's times were read as
    target: str      # the course's own zone
    cells: int       # how many date/time pairs moved
    example: str     # one of them, both ways round, for the user to sanity-check
    #: How `source` was determined -- "iana=" or the friendly label it came
    #: from. Shown because a zone derived from a label is a step further from
    #: what the file literally said, and the user should be able to see that.
    via: str = "iana="

    def describe(self) -> str:
        return (
            f"Sheet times are in {self.source} (from {self.via}); the course "
            f"runs in {self.target}.\n"
            f"  Converting {self.cells} time(s) to course time -- the instant "
            f"is preserved, so the wall clock moves.\n"
            f"  e.g. {self.example}"
        )


def sheet_zone(sheet: Sheet) -> tuple[str | None, str, tuple[str, ...]]:
    """Which zone the sheet's wall clocks are in, and how that was decided.

    Returns `(iana, via, warnings)`. `iana` is None when the sheet says nothing
    usable, which the header contract defines as "already course-local".

    **`iana=` wins, but the friendly label is a real fallback, not decoration**
    (user, 2026-08-22). Canvas is Rails, so `timezone=Eastern Time (US &
    Canada)` is a `ActiveSupport::TimeZone` name and maps deterministically to
    an IANA zone. An earlier version ignored it on the grounds that a friendly
    name "cannot resolve a wall clock across a DST change" -- true of `EST` or
    `-05:00`, both of which name an *observance* or a fixed offset, but false
    of the year-round label `pull` actually writes.

    An unreadable label is reported rather than silently ignored: someone who
    typed a zone into the header meant it to be used, and quietly treating
    those times as course-local is the failure they would never spot.
    """
    if sheet.iana:
        return sheet.iana, "iana=", ()
    if not sheet.timezone:
        return None, "", ()

    resolved = resolve_friendly(sheet.timezone)
    if resolved:
        return resolved, f"timezone={sheet.timezone!r}", ()
    return None, "", (
        f"{sheet.path} has no `iana=` and its `timezone={sheet.timezone}` is "
        f"not a timezone this build recognises, so its times are being read as "
        f"course-local. If they are not, add `iana=<zone>` to row 1 or re-pull.",
    )


def align_timezone(
    sheet: Sheet, course_tz: str
) -> tuple[Sheet, Realignment | None, tuple[str, ...]]:
    """Re-express a sheet's times in the course's zone, if they are not already.

    The sheet's zone says what its wall clocks are written in -- nothing more.
    It is not a claim about the course, so a disagreement is not an error to
    refuse; it is a conversion to perform. A sheet edited in Tokyo saying
    `12:59` and a course in New York holding `23:59` the previous day describe
    the **same deadline**, and it is the instant a student is held to.

    Everything downstream -- the diff, the write, and the post-write check --
    then speaks course time, exactly as it does for a sheet pulled from this
    course. Converting once here rather than at each of those three points is
    what keeps them from disagreeing.

    **A sheet that declares no zone at all means course time**, per the header
    contract: it was pulled in course time and the user deleted row 1.
    """
    source, via, warnings = sheet_zone(sheet)
    if not (source and course_tz) or source == course_tz:
        return sheet, None, warnings

    unanchored: list[str] = []
    rows: list[AssignmentRow] = []
    cells = 0
    example = ""

    for row in sheet.rows:
        moved: dict[str, str] = {}
        for _, date_column, time_column in FIELD_PAIRS:
            date = getattr(row, date_column)
            time = getattr(row, time_column)
            if not date:
                # A time with no date has nothing to anchor it to a day, so
                # there is no instant to convert. Left as it is; the write path
                # refuses a field with no date anyway.
                continue
            if not time:
                # Converting would have to assume a time, and a different
                # assumed time lands on a different DATE -- the assumption
                # would silently change the day the user typed.
                unanchored.append(
                    f"assignment {row.assignment_id} {date_column}={date}"
                )
                continue
            where = f"{sheet.path.name}, assignment {row.assignment_id}, {date_column}"
            new_date, new_time = convert(
                date, time, source, course_tz, where=where
            )
            moved[date_column] = new_date
            moved[time_column] = new_time
            cells += 1
            if not example:
                example = (
                    f"{date} {time} {source}  ->  "
                    f"{new_date} {new_time} {course_tz}"
                )
        rows.append(replace(row, **moved) if moved else row)

    if unanchored:
        raise SheetError(
            f"{sheet.path} records times in {source} but the course runs "
            f"in {course_tz}, so every time has to be converted -- and "
            f"{len(unanchored)} cell(s) have a date with no time to convert: "
            f"{', '.join(unanchored[:5])}"
            + (f", and {len(unanchored) - 5} more" if len(unanchored) > 5 else "")
            + ". Assuming a time would change the date itself. Fill the time "
            "in, or set the sheet's `iana=` to the course's own zone if the "
            "dates were already written in course time."
        )

    # The returned sheet is course-local, so it must say so: `iana` becomes the
    # course's zone and the familiar label is dropped rather than left behind
    # naming a zone the values are no longer in. **This holds even when nothing
    # was convertible** -- "after align_timezone the sheet is in course time"
    # is the one invariant the diff and the write path both rely on, and an
    # invariant with an exception in it is not one.
    aligned = replace(sheet, rows=rows, iana=course_tz, timezone=None)
    if not cells:
        # The zones differ but the sheet holds nothing to convert. Announcing
        # "converting 0 times" with no example to show would be noise.
        return aligned, None, warnings
    return aligned, Realignment(
        source=source, target=course_tz, cells=cells, example=example, via=via,
    ), warnings


# ---------------------------------------------------------------------------
# The infosheet diff.
#
# Same shape as the date diff above and deliberately NOT the same code: the two
# sheets agree on almost nothing that matters. Row identity is
# `assignment_id` alone rather than a pair, there is no timezone to align, and
# **an empty cell means "leave alone" rather than "clear"** -- so the one rule
# the date path most needs (present-but-empty is an instruction) is precisely
# the rule that must not appear here.
# ---------------------------------------------------------------------------

#: What the write path can set. **Grown one widget at a time on purpose**
#: (user, 2026-08-30): each control on the form is a different shape and has to
#: be reconnoitred before it is coded, so adding a column here without looking
#: at the real page is how a silent no-op gets shipped.
#:
#: Everything else on the infosheet is reported as not-yet-writable rather than
#: silently ignored -- `pull` populates those columns, so a user will edit one
#: eventually, and a no-op that looks like a success is the failure mode this
#: project keeps meeting.
WRITABLE_INFO_FIELDS = (
    "title", "points_possible", "grading_type", "submission_types",
    "allowed_attempts", "peer_reviews",
)


#: What a boolean cell may say. `pull` writes `true`/`false` -- but a
#: spreadsheet that recognises those as booleans **re-saves them as `TRUE` and
#: `FALSE`**, so a case-sensitive comparison would report an edit nobody made,
#: write it, and report it again forever. That is `same_points` and
#: `same_attempts`'s lesson arriving on a third field.
#:
#: `yes`/`no` and `1`/`0` are accepted because they are what a person types into
#: a column of `true`s. Nothing else is guessed at: `t`, `y` and a bare `x` all
#: read as an intention this cannot confirm.
FLAG_TRUE = ("true", "yes", "1")
FLAG_FALSE = ("false", "no", "0")


def parse_flag(cell: str) -> bool | None:
    """A boolean cell as a bool, or None when it does not name one."""
    text = (cell or "").strip().lower()
    if text in FLAG_TRUE:
        return True
    if text in FLAG_FALSE:
        return False
    return None


def same_flag(before: str, after: str) -> bool:
    """Whether two boolean cells mean the same thing.

    Case- and spelling-insensitive across the accepted forms, for the reason
    recorded on `FLAG_TRUE`. Falls back to string equality so an unparseable
    cell is still *reported* rather than swallowed.
    """
    a, b = parse_flag(before), parse_flag(after)
    if a is None or b is None:
        return (before or "").strip() == (after or "").strip()
    return a is b


def check_peer_reviews(cell: str) -> str | None:
    """Why this peer-review cell cannot be written, or None if it can.

    Checked before a page is loaded, so a bad cell is named in the dry run
    rather than costing an edit-page load per row to discover.
    """
    if parse_flag(cell) is None:
        return (f"peer_reviews={cell!r} does not name a yes or a no. Canvas "
                f"accepts {', '.join(FLAG_TRUE)} or {', '.join(FLAG_FALSE)}; "
                f"`pull` writes true/false")
    return None


def same_attempts(before: str, after: str) -> bool:
    """Whether two attempts cells mean the same count.

    Compared as integers: a spreadsheet will rewrite `3` as `3.0`, and a text
    comparison would report an edit nobody made, write it, and report it again
    forever -- the trap `same_points` and `to_minute` each exist for. Falls
    back to string equality so a non-numeric cell is still *reported* rather
    than swallowed.
    """
    a, b = (before or "").strip(), (after or "").strip()
    if a == b:
        return True
    try:
        return int(float(a)) == int(float(b))
    except ValueError:
        return False


#: Canvas's own encoding for "no limit", written raw into the sheet because
#: rendering it as the word "unlimited" would invent vocabulary the sheet then
#: has to parse back (user-confirmed 2026-08-30).
UNLIMITED_ATTEMPTS = "-1"


def check_allowed_attempts(cell: str) -> str | None:
    """Why this attempts cell cannot be written, or None if it can.

    Checked before a page is loaded, so a bad cell is named in the dry run
    rather than costing an edit-page load per row to discover.
    """
    text = (cell or "").strip()
    try:
        number = int(float(text))
    except ValueError:
        return (f"allowed_attempts={cell!r} is not a whole number. Canvas "
                f"accepts a positive count, or {UNLIMITED_ATTEMPTS} for "
                f"unlimited")
    if str(number) != text and float(text) != number:
        return (f"allowed_attempts={cell!r} is not a whole number -- an "
                f"assignment cannot be attempted a fraction of a time")
    if number == 0:
        # Canvas's own control cannot express this: "Limited" with a count of
        # zero is not offered, and it would mean an assignment nobody can
        # submit -- which is what unpublishing is for.
        return ("allowed_attempts=0 would mean an assignment that cannot be "
                "attempted at all. Canvas's control does not offer it; "
                f"use {UNLIMITED_ATTEMPTS} for unlimited, or a positive count")
    if number < -1:
        return (f"allowed_attempts={cell!r} is negative. Only "
                f"{UNLIMITED_ATTEMPTS} has a meaning (unlimited)")
    return None

#: The `Submission Type` select's own values (recon 2026-09-09). As with
#: `grading_type`, these are the option VALUES and not the visible words --
#: "No Submission", "Online", "On Paper", "External Tool".
SUBMISSION_MODES = ("none", "on_paper", "online", "external_tool")

#: The five online sub-types, which is what ENV reports once `online` is chosen.
#: A cell naming any of these means the mode is `online`; the mode itself is
#: never written in the sheet, because ENV never reports it that way.
ONLINE_SUBMISSION_TYPES = (
    "online_text_entry", "online_url", "online_upload", "media_recording",
    "student_annotation",
)

#: Expressible in the sheet and writable. `online` is absent deliberately: ENV
#: reports the chosen sub-types, never the bare mode, so a cell reading
#: `online` would describe a state Canvas cannot be left in -- picking Online
#: with no box ticked is not a thing the form saves.
WRITABLE_SUBMISSION_TYPES = ("none", "on_paper") + ONLINE_SUBMISSION_TYPES

#: **Refused, and not for lack of a control.** Selecting External Tool requires
#: a tool URL, and the infosheet has no column carrying one -- so a write would
#: produce an assignment configured for an external tool with no tool attached.
#: Refused at diff time, naming the reason, rather than half-configuring a real
#: assignment (user, 2026-09-09: "that's good for now").
UNWRITABLE_SUBMISSION_TYPES = ("external_tool",)


def same_submission_types(before: str, after: str) -> bool:
    """Whether two submission-type cells name the same set.

    **Order-insensitive.** Canvas reports its own order and a person types
    theirs, so `online_upload,online_text_entry` and the reverse are the same
    assignment. Comparing as text would report an edit nobody made, write it,
    and report it again forever -- the unsatisfiable-diff trap that `to_minute`
    and `same_points` each exist to avoid.
    """
    return _type_set(before) == _type_set(after)


def _type_set(cell: str) -> frozenset[str]:
    return frozenset(p.strip() for p in (cell or "").split(",") if p.strip())


def check_submission_types(cell: str) -> str | None:
    """Why this cell cannot be written, or None if it can.

    Checked before a page is loaded, so a bad cell is named in the dry run
    rather than costing an edit-page load per row to discover.
    """
    wanted = _type_set(cell)
    unknown = sorted(wanted - set(SUBMISSION_MODES) - set(ONLINE_SUBMISSION_TYPES))
    if unknown:
        return (f"submission_types={cell!r} contains {', '.join(unknown)}, "
                f"which Canvas does not report. Accepted: "
                f"{', '.join(WRITABLE_SUBMISSION_TYPES)}")
    refused = sorted(wanted & set(UNWRITABLE_SUBMISSION_TYPES))
    if refused:
        return (f"submission_types={cell!r} selects {', '.join(refused)}, which "
                f"needs a tool URL the infosheet has no column for. Writing it "
                f"would leave an external-tool assignment with no tool. Set it "
                f"in Canvas instead")
    if "online" in wanted:
        return (f"submission_types={cell!r} names the bare mode 'online'. "
                f"Canvas reports the chosen sub-types instead, so name them: "
                f"{', '.join(ONLINE_SUBMISSION_TYPES)}")
    # `none` and `on_paper` are whole states, not ingredients. Combining either
    # with anything describes an assignment Canvas cannot be in, and picking a
    # winner would be a guess about which half the user meant.
    exclusive = sorted(wanted & {"none", "on_paper"})
    if exclusive and len(wanted) > 1:
        return (f"submission_types={cell!r} combines {exclusive[0]!r} with "
                f"other types. It is a complete state on its own")
    return None

#: The values Canvas's "Display Grade as" control actually accepts. **These are
#: the option VALUES, not the words on screen** -- the option reading "Points"
#: has value `points`, "Complete/Incomplete" has `pass_fail`, and ENV reports
#: the value. Typing a label into a value field is a silent no-op that looks
#: exactly like a successful write.
#:
#: Duplicated from `writer.FORM_FIELDS` on purpose: this check runs at diff
#: time, before a page is ever loaded, so a typo is named in the dry run
#: instead of costing an edit-page load per row to discover. `writer` still
#: validates against the page's own options, because this list can go stale and
#: the page cannot.
GRADING_TYPES = (
    "points", "percent", "letter_grade", "gpa_scale", "pass_fail", "not_graded",
)

#: Taking an assignment out of the gradebook. Not refused -- it is a real thing
#: to want -- but it hides the points and dates and is not an accident anyone
#: should make quietly.
NOT_GRADED = "not_graded"


def check_grading_type(cell: str) -> str | None:
    """Why this grading-type cell cannot be written, or None if it can.

    Refused here rather than typed and rejected on the page. The likeliest
    mistake is writing the label a person sees -- "Points",
    "Complete/Incomplete" -- where Canvas wants the option value, and that is
    worth naming precisely: it is a silent no-op otherwise.
    """
    if (cell or "").strip() not in GRADING_TYPES:
        return (f"grading_type={cell!r} is not one of "
                f"{', '.join(GRADING_TYPES)} (these are the option values, "
                f"not the words shown on the form)")
    return None


@dataclass(frozen=True)
class InfoRowDiff:
    assignment_id: str
    title: str
    changes: list[FieldChange]
    #: Edits to columns the write path cannot yet apply. Carried separately so
    #: they can be *reported* without being attempted.
    unsupported: list[FieldChange]
    #: Cells this build understands the column for but cannot use the value of
    #: -- a `grading_type` of "Points" (the label) rather than `points` (the
    #: value), say. Named at diff time so the dry run says which cell to fix,
    #: rather than costing an edit-page load each to find out.
    invalid: list[str]
    #: Edits this build CAN write but was not asked to. Today that is `title`,
    #: behind `push --rename`. Deliberately its own category rather than folded
    #: into `unsupported`: "not writable yet" and "writable, say so" are
    #: different facts about the tool, and reporting the second as the first
    #: would tell the reader to wait for a feature that already exists.
    gated: list[FieldChange]
    #: True when Canvas says this assignment already has graded submissions.
    #: A points change then re-scales every student's percentage -- 8.34 out of
    #: 8.33 is over 100%. The user chose warn-and-write over refusing
    #: (2026-08-30), so this rides along to be said loudly rather than to block.
    graded: bool = False


@dataclass(frozen=True)
class InfoDiff:
    changed: list[InfoRowDiff]
    missing: list[InfoRow]
    untouched: list[InfoRow]
    compared: int

    @property
    def is_empty(self) -> bool:
        """Whether a push would write anything.

        Rows whose only edits are unsupported do NOT count as writable -- but
        they are still reported. See `has_unsupported`.
        """
        return not any(row.changes for row in self.changed)

    @property
    def has_unsupported(self) -> bool:
        return any(row.unsupported for row in self.changed)

    @property
    def has_invalid(self) -> bool:
        return any(row.invalid for row in self.changed)

    @property
    def has_gated(self) -> bool:
        return any(row.gated for row in self.changed)

    @property
    def field_count(self) -> int:
        return sum(len(row.changes) for row in self.changed)


def same_points(before: str, after: str) -> bool:
    """Whether two points cells mean the same number.

    Compared numerically, not as strings: a spreadsheet will happily rewrite
    `8.34` as `8.340` or `8.3400000000001`, and a string comparison would
    report an edit the user never made -- then write it, and report it again on
    the next push, forever. This is the same lesson as `to_minute`, which
    exists because comparing seconds created a diff that could never be
    satisfied.

    Falls back to string equality when either side is not a number, so a
    non-numeric cell is still *reported* rather than silently swallowed.
    """
    a, b = (before or "").strip(), (after or "").strip()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


#: Per-column comparison, in ONE place. Each of these columns has text that can
#: differ while its meaning does not -- `8.340` for `8.34`, a reordered
#: submission-type list, `3.0` for `3`, `TRUE` for `true` -- and comparing such
#: a cell as text reports an edit nobody made, writes it, and reports it again
#: forever.
#:
#: **A table rather than a chain of `if`s, because the chain had three copies**
#: -- the diff, the post-write check, and the check that decides *which* failure
#: is reported -- and a field added to two of the three is exactly the seam that
#: made a correct `submission_types` write report MISMATCH on 2026-09-09. One
#: table means the next column is one entry, not three edits that must agree.
INFO_COMPARISONS = {
    "points_possible": same_points,
    "submission_types": same_submission_types,
    "allowed_attempts": same_attempts,
    "peer_reviews": same_flag,
}


def same_value(column: str, before: str, after: str) -> bool:
    """Whether two cells of `column` mean the same thing.

    Plain text equality for columns with no special rule, which is correct for
    them: a title or a grading-type value means itself.
    """
    rule = INFO_COMPARISONS.get(column)
    return rule(before, after) if rule else before == after


#: Why a cell cannot be written, checked at diff time so the dry run names the
#: cell to fix instead of costing an edit-page load per row to find out. Same
#: reasoning as `INFO_COMPARISONS`: one table, so a new column is one entry.
INFO_CELL_CHECKS = {
    "submission_types": check_submission_types,
    "allowed_attempts": check_allowed_attempts,
    "peer_reviews": check_peer_reviews,
    "grading_type": check_grading_type,
}


def compare_info(
    sheet: InfoSheet,
    current: list[InfoRow],
    graded: frozenset[str] = frozenset(),
    allow_rename: bool = False,
) -> InfoDiff:
    """Diff an edited infosheet against freshly read rows.

    `current` must come from a read of the same course done *now*, for the same
    reason the date diff insists on it: the write has to be judged against what
    Canvas holds at the moment of writing.

    `graded` is the set of assignment ids Canvas reports as already having
    graded submissions. **Passed in rather than carried as a sheet column on
    purpose:** it is a fact about the course at the moment of writing, not
    something the user edits, and putting it in the CSV would both invite an
    edit that means nothing and change a schema the user has already signed
    off on.
    """
    live = {row.key: row for row in current}
    seen: set[str] = set()

    changed: list[InfoRowDiff] = []
    missing: list[InfoRow] = []

    for wanted in sheet.rows:
        seen.add(wanted.key)
        have = live.get(wanted.key)
        if have is None:
            missing.append(wanted)
            continue

        changes: list[FieldChange] = []
        unsupported: list[FieldChange] = []
        gated: list[FieldChange] = []
        invalid: list[str] = []
        for column in INFO_EDITABLE:
            if not sheet.specifies(column):
                continue
            after = (getattr(wanted, column) or "").strip()
            # **An empty cell means "leave alone" here.** Nothing on this sheet
            # can be unset -- an assignment always has points, a publish state
            # and a group -- so a blank is never an instruction. This is the
            # one place the datesheet's rule must NOT be copied.
            if not after:
                continue
            before = (getattr(have, column) or "").strip()
            # Compared by the column's own rule -- numerically for points, as a
            # set for submission types, as an integer for attempts, as a boolean
            # for peer review. One table, shared with the post-write check.
            if same_value(column, before, after):
                continue
            change = FieldChange(field=column, before=before, after=after)
            check = INFO_CELL_CHECKS.get(column)
            if check:
                reason = check(after)
                if reason:
                    invalid.append(reason)
                    continue
            if column in RENAME_GATED and not allow_rename:
                # Writable, but not without being asked. Reported so the run
                # says what it declined to do and how to ask for it -- silence
                # here would read as "no change", which is the failure mode
                # this project keeps meeting.
                gated.append(change)
            elif column in WRITABLE_INFO_FIELDS:
                changes.append(change)
            else:
                unsupported.append(change)

        if changes or unsupported or gated or invalid:
            changed.append(
                InfoRowDiff(
                    assignment_id=wanted.key,
                    # Canvas's title wins, as in the date diff: the sheet may
                    # not carry one, and a renamed cell must not relabel what
                    # is about to change.
                    title=have.title or wanted.title or f"assignment {wanted.key}",
                    changes=changes,
                    unsupported=unsupported,
                    gated=gated,
                    invalid=invalid,
                    graded=wanted.key in graded,
                )
            )

    untouched = [row for key, row in live.items() if key not in seen]
    return InfoDiff(
        changed=changed,
        missing=missing,
        untouched=untouched,
        compared=len(sheet.rows),
    )


def check_info_course(sheet: InfoSheet, course_id: str) -> None:
    """Refuse an infosheet that names a different course. See `check_course`."""
    if sheet.course_id and course_id and sheet.course_id != course_id:
        raise SheetError(
            f"{sheet.path} was pulled from course {sheet.course_id}, but this "
            f"push targets {course_id}. Refusing -- writing one course's "
            f"settings into another is not a recoverable mistake."
        )


def describe_scope(sheet: Sheet) -> str:
    """Which fields this sheet is able to change, for the run's preamble.

    Worth printing every time: a sheet with the close columns deleted simply
    cannot touch lock dates, and saying so up front is cheaper than a user
    wondering why their edit "did not take".
    """
    present = {c.split("_")[0] for c in sheet.editable_present}
    names = [n for k, n in (("open", "open"), ("due", "due"), ("close", "close"))
             if k in present]
    if not names:
        return "no editable date columns -- this sheet cannot change anything"
    return ", ".join(names)
