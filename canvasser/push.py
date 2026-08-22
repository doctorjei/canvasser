"""Comparing an edited datesheet against what Canvas currently holds.

This module is the *diff*. It answers "what would change?" and nothing more --
no navigation to an edit form, no writes. That separation is deliberate: the
diff is the thing that gets run over and over while a sheet is being edited,
and it must never be able to touch the course.

The first test this exists to serve is the **no-op round trip**: pull a course,
push it back unedited, and see zero changes. Anything that survives that is a
formatting bug in the sheet, not an edit -- exactly the class of error that
would otherwise be discovered by writing wrong dates into a live class.

## What counts as a change

Only the six editable columns. `title` and `assign_to` are carried for human
orientation and are deliberately ignored here: renaming a row in a spreadsheet
must never retarget or trigger a write.

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
