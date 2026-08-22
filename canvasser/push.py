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
"""

from __future__ import annotations

from dataclasses import dataclass

from .datesheet import AssignmentRow, Sheet

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


def check_course(sheet: Sheet, course_id: str) -> None:
    """Refuse a sheet that came from a different course.

    Without this the assignment ids simply would not match and the diff would
    report "everything missing" -- a far worse failure than being told the
    sheet belongs elsewhere, because it looks like data loss.
    """
    if sheet.course_id and sheet.course_id != course_id:
        raise ValueError(
            f"{sheet.path} was pulled from course {sheet.course_id}, but this "
            f"run targets {course_id}. Refusing: pushing a sheet into the wrong "
            f"course would write one class's dates onto another."
        )


def check_timezone(sheet: Sheet, course_tz: str) -> None:
    """Refuse a sheet whose zone no longer matches the course's.

    The sheet's times are bare wall clocks; `iana=` in its header is the only
    thing that says what they mean. If the course's timezone has been changed
    since the pull, every value in the file now denotes a *different instant*
    than it did when written -- and nothing about the file looks wrong. A
    course moved from New York to Los Angeles would silently shift every
    deadline by three hours.

    Refusing is right rather than converting: which the user meant -- the same
    wall clock in the new zone, or the same instant -- is a question only they
    can answer, and quietly picking one would be a guess about real deadlines.
    """
    if sheet.iana and course_tz and sheet.iana != course_tz:
        raise ValueError(
            f"{sheet.path} records times in {sheet.iana}, but the course is now "
            f"in {course_tz}. Every time in the sheet would mean a different "
            f"instant than when it was pulled. Re-run `canvasser pull` to "
            f"regenerate the sheet in the course's current timezone, then "
            f"re-apply your edits."
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
