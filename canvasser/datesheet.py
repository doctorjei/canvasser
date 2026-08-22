"""The CSV that carries assignment dates out to a spreadsheet and back.

Scope (schema v3): **the three date fields Canvas puts on an assignment** --
`unlock_at` ("Available from"), `due_at` ("Due"), and `lock_at` ("Until").
Still no points, submission types, or grading settings.

Row identity is `(assignment_id, override_id)`, never the title:

  editable   due_date, due_time, open_date, open_time, close_date, close_time
  read-only  assignment_id, override_id, title, assign_to

`override_id` empty means the assignment's own base row ("Everyone" / "Everyone
else"); a value means that specific override. Read-only columns are still
written, because a sheet without titles is unusable by a human -- they are just
not honored on the way back in. Editing a title in the spreadsheet must never
retarget a write.

## Why dates and times are split, and the offset is gone

Through v2 each value was a single course-time string carrying its own offset
(`2026-11-30 23:59:59 -0500`). That is unambiguous but miserable to edit: a
spreadsheet cannot bulk-shift a date without the user retyping the offset, and
the offset itself changes across a DST boundary.

v3 declares the zone **once, in the header**, and gives every value two plain
columns -- a date and a time. Editing "push everything back a week" becomes a
column operation.

**The cost, stated plainly:** a wall-clock time with no offset is ambiguous for
exactly one hour a year, when clocks go back and 01:30 happens twice; and for
one hour it is impossible, when they spring forward and 02:30 never occurs.
Deadlines land at 23:59, so this is theoretical here -- but `push` must resolve
these through the header's zone, and should refuse rather than guess if it ever
meets a nonexistent local time.

## Header

Three rows, all CSV-parseable so a spreadsheet round-trips them intact:

    # canvasser datesheet v3,,course=580777,,timezone=Eastern Time (US & Canada),,,,,iana=America/New_York
    Assignment Details,,,,due_at,,unlock_at,,lock_at,
    assignment_id,override_id,title,assign_to,due_date,due_time,...

**Row 1 is laid out on the same column grid as the data**, one item per cell
with gaps between, so a spreadsheet shows them spread across the top rather
than crushed into column A. Position is presentational only -- the values are
still `key=value`, so a reader finds them by name and a shifted column cannot
silently change their meaning.

Row 1 records the schema version, the source course, and the zone every time in
the file is expressed in -- **twice, in its own cell each**. `timezone` is
Canvas's familiar name, because that is the one a person reads. `iana` is the
identifier, and it is the authoritative one: a familiar name cannot tell a
program how to resolve a wall clock across a DST change, and `America/New_York`
can. Both are captured in the same pull, so they cannot drift apart.

**The familiar name is deliberately the generic form** -- "Eastern Time", never
EST or EDT. EST is standard time, EDT is daylight; a semester sheet spans both,
so either variant would be wrong for half its rows.

Row 2 groups the pairs under the Canvas field each belongs to. Row 3 is the
real column header.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, fields
from pathlib import Path

from .dateparse import normalize_date, normalize_time

#: Written into every file so a later `push` can reject a sheet it does not
#: understand instead of misreading columns that moved. v3 split every datetime
#: into date+time and added the open/close pairs, so a v2 sheet is not merely
#: older -- its columns mean different things.
SCHEMA_VERSION = "3.1"

#: Row 1, first cell. The rest of the row is `key=value` cells parsed separately
#: -- a single regex cannot hold them, because `timezone_name` is free text with
#: spaces and parentheses ("Eastern Time (US & Canada)").
HEADER = re.compile(r"#\s*canvasser datesheet v(?P<version>[^\s,]+)")

#: Canvas's own field names, for row 2. `unlock_at`/`lock_at` are what the API
#: calls the boxes the UI labels "Available from" and "Until".
FIELD_GROUPS = (("unlock_at", 4), ("due_at", 6), ("lock_at", 8))

EDITABLE_COLUMNS = (
    "open_date", "open_time", "due_date", "due_time", "close_date", "close_time",
)


class SheetError(ValueError):
    """A datesheet cannot be used as given, and the user must change something.

    Distinct from a bare `ValueError` so the CLI can print the message plainly
    instead of a traceback: every one of these is a sentence addressed to the
    person editing the file, not a defect report.
    """


@dataclass(frozen=True)
class AssignmentRow:
    """One assign-to target's dates.

    Date pairs run in **chronological order -- open, due, close** (user's
    call, 2026-08-21): the order the student meets them, and the order they
    read in a spreadsheet.
    """

    assignment_id: str
    override_id: str = ""
    title: str = ""
    assign_to: str = ""
    open_date: str = ""
    open_time: str = ""
    due_date: str = ""
    due_time: str = ""
    close_date: str = ""
    close_time: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """What push must match on. Stable across title and date edits."""
        return (self.assignment_id, self.override_id)

    @property
    def is_base_row(self) -> bool:
        """True for the assignment's own dates rather than an override."""
        return not self.override_id

    @property
    def due(self) -> str:
        return " ".join(p for p in (self.due_date, self.due_time) if p)

    @property
    def has_any_date(self) -> bool:
        return any(getattr(self, column) for column in EDITABLE_COLUMNS)


COLUMNS = tuple(f.name for f in fields(AssignmentRow))


#: Where each piece of row-1 metadata sits, to the user's layout: spread across
#: the same grid as the data rows with gaps between, so a spreadsheet shows one
#: item per cell instead of everything crushed into column A. Presentational
#: only -- every cell is still `key=value`, so a reader finds them by name.
STAMP_COURSE, STAMP_TIMEZONE, STAMP_IANA = 2, 3, 7


def _stamp_row(
    course_id: str | None, timezone: str | None, iana: str | None
) -> list[str]:
    """Row 1: version, course, and both timezone forms, on the column grid."""
    cells = [""] * len(COLUMNS)
    cells[0] = f"# canvasser datesheet v{SCHEMA_VERSION}"
    if course_id:
        cells[STAMP_COURSE] = f"course={course_id}"
    if timezone:
        cells[STAMP_TIMEZONE] = f"timezone={timezone}"
    if iana:
        cells[STAMP_IANA] = f"iana={iana}"
    return cells


def _group_row() -> list[str]:
    """Row 2: the Canvas field each date/time pair belongs to."""
    cells = [""] * len(COLUMNS)
    cells[0] = "Assignment Details"
    for name, index in FIELD_GROUPS:
        cells[index] = name
    return cells


def write_sheet(
    rows: list[AssignmentRow],
    path: Path,
    course_id: str | None = None,
    timezone: str | None = None,
    iana: str | None = None,
) -> Path:
    """Write the CSV, newest pull wins.

    Both timezone forms go in row 1, in their own cells: `timezone` is Canvas's
    familiar name, which is what a person reads, and `iana` is the id `push`
    resolves wall-clock times through. They are written together from the same
    page load, so they cannot drift apart -- which is why this stores both
    rather than converting between them later.

    `newline=""` is required by the csv module on every platform; without it you
    get blank lines between records that spreadsheets happily import as garbage.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_stamp_row(course_id, timezone, iana))
        writer.writerow(_group_row())
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([getattr(row, column) for column in COLUMNS])
    return path


@dataclass(frozen=True)
class Sheet:
    """A parsed datesheet: its rows plus where they came from."""

    rows: list[AssignmentRow]
    course_id: str | None
    version: str | None
    #: Canvas's familiar name ("Eastern Time (US & Canada)"). For people.
    timezone: str | None
    #: IANA id ("America/New_York"). **Authoritative** -- push must resolve
    #: wall-clock times through this, never through the familiar name. May be
    #: absent if the user deleted row 1, in which case the course's own zone
    #: is used: the sheet was pulled in course time either way.
    iana: str | None
    #: Cells where US and European day/month readings were both valid and the
    #: US one was assumed. Never silent -- the caller shows these to the user.
    warnings: tuple[str, ...]
    #: Which columns the file actually carried. Load-bearing: an **absent**
    #: date column means "leave this field alone", while a **present but
    #: empty** cell means "clear it". Collapsing the two would let deleting a
    #: column wipe every date in it.
    columns: tuple[str, ...]
    path: Path

    def specifies(self, column: str) -> bool:
        return column in self.columns

    @property
    def editable_present(self) -> tuple[str, ...]:
        return tuple(c for c in EDITABLE_COLUMNS if c in self.columns)

    def by_key(self) -> dict[tuple[str, str], AssignmentRow]:
        return {row.key: row for row in self.rows}


def read_sheet(path: Path) -> Sheet:
    """Read a CSV back, tolerating the preamble and spreadsheet re-saves."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        records = list(csv.reader(handle))

    course_id = version = timezone = iana = None
    for record in records:
        if record and record[0].lstrip().startswith("#"):
            found = HEADER.match(record[0].strip())
            if found:
                version = found.group("version")
                # Remaining cells are key=value. Split on the first `=` only:
                # a value may contain one, and the label contains spaces and
                # parentheses that no whitespace-delimited pattern would keep.
                meta = {}
                for cell in record[1:]:
                    key, sep, value = cell.strip().partition("=")
                    if sep:
                        meta[key.strip()] = value.strip()
                course_id = meta.get("course")
                timezone = meta.get("timezone")
                iana = meta.get("iana")
            break

    # Find the real column header by looking for it, rather than counting
    # preamble lines -- a spreadsheet may add or drop a blank row on re-save,
    # and silently reading row 2 as the header would misname every column.
    start = next(
        (i for i, record in enumerate(records) if "assignment_id" in record), None
    )
    if start is None:
        raise SheetError(
            f"{path} has no column header row -- expected a line containing "
            f"'assignment_id'. Re-run `canvasser pull` to regenerate it."
        )

    # Checked before parsing rows: across a major version the columns mean
    # something else, so complaining about their contents would mislead.
    #
    # Only the MAJOR part has to match. A minor bump means the same columns
    # carrying the same meanings -- v3.1 only reordered the date pairs -- and
    # rows are matched by column *name*, never by position, so a v3 sheet reads
    # correctly here. Refusing it would force a nine-minute re-pull to fix a
    # difference that changes nothing.
    if version and _major(version) != _major(SCHEMA_VERSION):
        raise SheetError(
            f"{path} is datesheet v{version}; this build writes and reads "
            f"v{SCHEMA_VERSION}. Re-run `canvasser pull` to regenerate it."
        )

    # Only `assignment_id` and whatever the user wants to change need to be
    # present; every other column may be deleted (user, 2026-08-21). Unknown
    # columns are ignored rather than rejected, so a spreadsheet's scratch
    # column does not break the file.
    header = records[start]
    present = tuple(c for c in COLUMNS if c in header)

    warnings: list[str] = []
    rows: list[AssignmentRow] = []
    for line, record in enumerate(records[start + 1:], start=start + 2):
        if not any(cell.strip() for cell in record):
            continue  # A trailing blank line a spreadsheet left behind.
        values = dict(zip(header, record))
        data = {c: (values.get(c) or "").strip() for c in present}
        # Normalise whatever the spreadsheet did to the date and time cells, so
        # everything downstream compares canonical strings and a cosmetic
        # reformat cannot masquerade as an edit.
        for column in present:
            if column.endswith("_date"):
                data[column] = normalize_date(
                    data[column],
                    where=f"{path.name} line {line}, {column}",
                    warnings=warnings,
                )
            elif column.endswith("_time"):
                data[column] = normalize_time(
                    data[column], where=f"{path.name} line {line}, {column}"
                )
        if not data.get("assignment_id"):
            raise SheetError(
                f"{path} line {line} has no assignment_id. Row identity is "
                f"(assignment_id, override_id) -- a row without one cannot be "
                f"matched to anything, and guessing would target the wrong "
                f"assignment."
            )
        rows.append(AssignmentRow(**data))

    duplicates = _duplicate_keys(rows)
    if duplicates:
        raise SheetError(
            f"{path} has repeated row key(s): {duplicates}. Each "
            f"(assignment_id, override_id) must appear once -- otherwise push "
            f"cannot tell which row wins."
        )
    return Sheet(
        rows=rows,
        course_id=course_id,
        version=version,
        timezone=timezone,
        iana=iana,
        columns=present,
        warnings=tuple(warnings),
        path=path,
    )


def _major(version: str) -> str:
    return (version or "").split(".", 1)[0].strip()


def _duplicate_keys(rows: list[AssignmentRow]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    repeated: list[tuple[str, str]] = []
    for row in rows:
        if row.key in seen:
            repeated.append(row.key)
        seen.add(row.key)
    return repeated
