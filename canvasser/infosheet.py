"""The CSV that carries an assignment's non-date settings (schema v1).

**A separate file from the datesheet, deliberately** (user, 2026-08-25):
*"Dates are fundamentally unique... we should have a separate sheet for the rest
of this info."* `pull` writes `dates-<course>.csv` and `info-<course>.csv` side
by side.

## Why the split, and the one rule that follows from it

A date is the one assignment field Canvas lets you simply **not have**. So on
the datesheet an empty cell *clears* the date, and that is meaningful.

**Nothing on this sheet can be unset.** An assignment is always in exactly one
group, always has a publish state, always has a submission type, and a blank
points box is coerced by Canvas to `0` -- which would be a silent grade change
wearing an absence's clothing, not an absence. So here:

    an empty cell means LEAVE THIS FIELD ALONE -- identical to deleting
    the column, and identical to deleting the whole row.

There is no "clear" marker because there is nothing for it to mark. If a
genuinely unsettable field ever lands here, it gets an explicit marker rather
than a silent reinterpretation of blank.

## What this sheet does NOT inherit from the datesheet

**Override rows, and `override_id`.** These settings are per *assignment*, not
per date card -- points and publish state are not things an accommodation can
differ on. So row identity is `assignment_id` alone, and the override hazard
that dominates `datesheet.py`/`writer.py` does not exist here at all. This is
worth stating rather than leaving implicit: the datesheet's identity rules are
the obvious thing to copy across out of symmetry, and copying them would import
a problem this sheet does not have.

## Header

Three rows, matching the datesheet's shape so a spreadsheet round-trips them
and a person recognises the layout:

    # canvasser infosheet v1,,course=580777,,,,,,
    Assignment Details,,,Grading,,,Submission,,Availability
    assignment_id,title,assignment_group,points_possible,...

Row 1 records the schema version and the source course, so a file cannot be
applied to the wrong class. **No timezone**: nothing here is a time.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, fields
from pathlib import Path

from .datesheet import HEADER as _DATESHEET_HEADER, SheetError

#: Bumped only when a column's *meaning* changes. Read compares major only, for
#: the same reason the datesheet does: adding a column is backward compatible,
#: because an absent column already means "leave this field alone".
SCHEMA_VERSION = "1"

#: Row 1, first cell. Distinct from the datesheet's marker on purpose -- this is
#: what lets `push` tell the two files apart before it parses a single row.
HEADER = re.compile(r"#\s*canvasser infosheet v(?P<version>[^\s,]+)")


@dataclass(frozen=True)
class InfoRow:
    """One assignment's non-date settings.

    Field order is the column order, and it runs read-only identity first, then
    grading, then submission, then availability -- the order the edit form
    presents them, so a person reading the sheet can follow the page.
    """

    assignment_id: str
    title: str = ""
    #: `assignment` or `quiz`. Read-only, and load-bearing for the reader:
    #: **a classic quiz's page carries no grading_type, submission_types or
    #: peer_reviews at all** (confirmed by live recon, 2026-08-25), so those
    #: cells are empty on every quiz row. Without this column that emptiness
    #: looks like a scraping failure instead of a fact about quizzes.
    kind: str = ""
    #: Read-only, like `title`: reported so the sheet is legible, never honored
    #: on the way back in. Canvas has no stable name-based lookup for groups.
    assignment_group: str = ""
    points_possible: str = ""
    grading_type: str = ""
    submission_types: str = ""
    allowed_attempts: str = ""
    published: str = ""
    peer_reviews: str = ""
    #: How many "Assign to" cards this assignment carries beyond the base one.
    #: **Read-only, and the reason this column exists at all:** today an
    #: override is discovered only when `push` refuses the row, mid-run. Here
    #: it is visible before anyone starts editing.
    override_count: str = ""

    @property
    def key(self) -> str:
        """What a future push would match on. No override_id: see module docs."""
        return self.assignment_id


COLUMNS = tuple(f.name for f in fields(InfoRow))

#: Columns a future `push` could write. Everything else is reported for the
#: reader's benefit and ignored on the way back in -- editing a title or a
#: group name in the spreadsheet must never retarget or rename anything.
EDITABLE_COLUMNS = (
    "points_possible", "grading_type", "submission_types",
    "allowed_attempts", "published", "peer_reviews",
)

#: Row 2 groupings, mirroring how the edit form is laid out. Indexes are
#: derived from COLUMNS rather than written as literals: the `kind` column was
#: added after this row was first laid out, and hand-counted indexes would have
#: silently slid one column left.
FIELD_GROUPS = tuple(
    (label, COLUMNS.index(column))
    for label, column in (
        ("Grading", "points_possible"),
        ("Submission", "submission_types"),
        ("Availability", "published"),
    )
)

#: Row 1 metadata sits on the same column grid as the data, one item per cell,
#: so a spreadsheet spreads it across the top instead of crushing it into
#: column A. Presentational only -- values stay `key=value` and are found by
#: name, so a shifted column cannot change what they mean.
STAMP_COURSE = 2


def _stamp_row(course_id: str | None) -> list[str]:
    cells = [""] * len(COLUMNS)
    cells[0] = f"# canvasser infosheet v{SCHEMA_VERSION}"
    if course_id:
        cells[STAMP_COURSE] = f"course={course_id}"
    return cells


def _group_row() -> list[str]:
    cells = [""] * len(COLUMNS)
    cells[0] = "Assignment Details"
    for name, index in FIELD_GROUPS:
        cells[index] = name
    return cells


def write_sheet(
    rows: list[InfoRow], path: Path, course_id: str | None = None
) -> Path:
    """Write the CSV, newest pull wins.

    `newline=""` is required by the csv module on every platform; without it
    you get blank lines between records that spreadsheets import as garbage.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_stamp_row(course_id))
        writer.writerow(_group_row())
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([getattr(row, column) for column in COLUMNS])
    return path


@dataclass(frozen=True)
class InfoSheet:
    """A parsed infosheet: its rows plus where they came from."""

    rows: list[InfoRow]
    course_id: str | None
    version: str | None
    #: Which columns the file actually carried. An absent column means "leave
    #: this field alone" -- and so does an empty cell, which is the difference
    #: from the datesheet. Kept anyway so a reader can report what the sheet
    #: was in a position to change.
    columns: tuple[str, ...]
    path: Path

    def specifies(self, column: str) -> bool:
        return column in self.columns

    @property
    def editable_present(self) -> tuple[str, ...]:
        return tuple(c for c in EDITABLE_COLUMNS if c in self.columns)

    def by_key(self) -> dict[str, InfoRow]:
        return {row.key: row for row in self.rows}


def read_sheet(path: Path) -> InfoSheet:
    """Read an infosheet back, tolerating the preamble and spreadsheet re-saves."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        records = list(csv.reader(handle))

    course_id = version = None
    for record in records:
        if record and record[0].lstrip().startswith("#"):
            found = HEADER.match(record[0].strip())
            if found:
                version = found.group("version")
                meta = {}
                for cell in record[1:]:
                    key, sep, value = cell.strip().partition("=")
                    if sep:
                        meta[key.strip()] = value.strip()
                course_id = meta.get("course")
            break

    # Located by searching for the column name, not by counting preamble rows:
    # a spreadsheet may add or drop a blank line on re-save, and reading a
    # fixed row as the header would misname every column.
    start = next(
        (i for i, record in enumerate(records) if "assignment_id" in record), None
    )
    if start is None:
        raise SheetError(
            f"{path} has no column header row -- expected a line containing "
            f"'assignment_id'. Re-run `canvasser pull` to regenerate it."
        )

    if version and _major(version) != _major(SCHEMA_VERSION):
        raise SheetError(
            f"{path} is infosheet v{version}; this build writes and reads "
            f"v{SCHEMA_VERSION}. Re-run `canvasser pull` to regenerate it."
        )

    header = records[start]
    present = tuple(c for c in COLUMNS if c in header)

    rows: list[InfoRow] = []
    for line, record in enumerate(records[start + 1:], start=start + 2):
        if not any(cell.strip() for cell in record):
            continue  # A trailing blank line a spreadsheet left behind.
        values = dict(zip(header, record))
        data = {c: (values.get(c) or "").strip() for c in present}
        if not data.get("assignment_id"):
            raise SheetError(
                f"{path} line {line} has no assignment_id. Rows are matched on "
                f"it alone -- a row without one cannot be matched to anything, "
                f"and guessing would target the wrong assignment."
            )
        rows.append(InfoRow(**data))

    duplicates = _duplicate_keys(rows)
    if duplicates:
        raise SheetError(
            f"{path} has repeated assignment_id(s): {duplicates}. Each must "
            f"appear once -- otherwise there is no telling which row wins."
        )
    return InfoSheet(
        rows=rows,
        course_id=course_id,
        version=version,
        columns=present,
        path=path,
    )


def _major(version: str) -> str:
    return (version or "").split(".", 1)[0].strip()


#: What `identify` returns. `None` means row 1 named neither sheet -- which is
#: legal, since the user may delete the preamble entirely.
DATES, INFO = "dates", "info"


def identify(path: Path) -> str | None:
    """Which kind of sheet is this? Read from row 1, never from the filename.

    **Two CSVs with near-identical names now sit side by side**, so
    `push info-580777.csv` is a matter of time. Without this the infosheet
    would parse as a datesheet with every date column absent -- which reads as
    "leave every date alone", i.e. a silent no-op that looks like a clean run.

    The filename is deliberately not consulted. A user may rename a file, and a
    sheet that says what it is in its own first row is the honest source.
    Returns `None` when neither marker is present, which the caller must treat
    as "assume the command's default", not as an error: deleting the preamble
    is explicitly allowed.
    """
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for record in csv.reader(handle):
                if not record or not record[0].lstrip().startswith("#"):
                    continue
                first = record[0].strip()
                if HEADER.match(first):
                    return INFO
                if _DATESHEET_HEADER.match(first):
                    return DATES
                return None
    except OSError:
        return None
    return None


def _duplicate_keys(rows: list[InfoRow]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for row in rows:
        if row.key in seen:
            repeated.append(row.key)
        seen.add(row.key)
    return repeated
