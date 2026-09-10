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

## Creating: the `NEW` row

An `assignment_id` of **`NEW`** means *create this assignment*, and `push --commit` writes
the id Canvas assigns straight back into that cell (user's design, 2026-09-09). That
write-back is what makes creation re-runnable: after the first push the row holds a real
id, so every later push is an ordinary edit rather than a second assignment.

Consequences that live in this module:

* **`NEW` rows are exempt from the duplicate-key check.** Five creates are five rows all
  reading `NEW`; the check exists because two rows naming *one* assignment leave no way to
  say which wins, and that ambiguity does not arise here.
* **`InfoSheet.lines` records where each row was read from**, so `claim_new_row` can put an
  id back in the exact cell it came from. Matching by position among the `NEW` rows would
  be wrong the moment one create fails: that row stays `NEW` and would collect the next id.
* **Two normally read-only columns open up** on such a row -- `CREATE_ONLY_COLUMNS`
  (`assignment_group`, `kind`) -- because editing never needs to set them and creating
  always does.
* **`find_lock` exists because the user's workflow is a spreadsheet**, and a spreadsheet
  holds the file open. See its docstring.

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

#: The `assignment_id` cell that means **create this assignment** (user,
#: 2026-09-09: *"That spreadsheet should be both read from and written to."*).
#:
#: **Why a marker and not a blank.** Creation is the first non-idempotent thing
#: this tool does: a blank-id row pushed twice would create the assignment
#: twice. `push` therefore writes the real id back into this cell the instant
#: the create succeeds, so the second push is an ordinary edit. `NEW` cannot
#: collide with a Canvas id, which is always numeric -- which is exactly what a
#: blank could not promise, since a blank is also what a half-edited row looks
#: like.
#:
#: **Matched case-insensitively**, though `pull` and the docs say `NEW`. There
#: is no numeric id spelled "new", so nothing is ambiguous; and the alternative
#: is that a row typed `new` reads as an assignment with id "new", which is
#: reported as missing from the course -- true, unhelpful, and a puzzle.
NEW = "NEW"


def is_new(assignment_id: str) -> bool:
    """Whether this id cell asks for a new assignment rather than naming one."""
    return (assignment_id or "").strip().upper() == NEW


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

    @property
    def is_new(self) -> bool:
        """Whether this row asks for an assignment to be created. See `NEW`."""
        return is_new(self.assignment_id)


COLUMNS = tuple(f.name for f in fields(InfoRow))

#: Columns `push` may write. Everything else is reported for the reader's
#: benefit and ignored on the way back in.
#:
#: **`title` is editable but gated behind `push --rename`** (user, 2026-09-09:
#: *"it should be editable, unless there is a specific issue with it"*, then
#: *"we could require a flag to write it"*). Renaming assignments in bulk is a
#: real course-setup operation, which is the workflow this tool serves. The
#: flag exists because the title is also the column a reader *navigates* by, so
#: an edit meant to make the sheet legible should not quietly rename what
#: students see. Nothing here retargets anything: row identity is
#: `assignment_id`, so a renamed cell still writes to the row it came from.
EDITABLE_COLUMNS = (
    "title", "points_possible", "grading_type", "submission_types",
    "allowed_attempts", "published", "peer_reviews",
)

#: Editable, but only when the caller asks for it explicitly.
RENAME_GATED = ("title",)

#: Columns that are read-only on an existing row but **must** be settable on a
#: `NEW` one, for the same underlying reason in both cases: *editing never needs
#: to set them, creating always does.*
#:
#: `assignment_group` because an assignment is always in exactly one group, and
#: `kind` because assignment and quiz are different create endpoints -- a `NEW`
#: row has to declare which it is rather than be guessed at. Note this is the
#: one place the sheet's own read-only marking has an exception, which is why it
#: is a named constant rather than a condition spelled out at each use.
CREATE_ONLY_COLUMNS = ("assignment_group", "kind")

#: `title` is NOT rename-gated on a `NEW` row. `--rename` exists because a title
#: edit can be made incidentally, while tidying a sheet for legibility, and must
#: not quietly rename what students see. A new assignment has no previous title
#: to protect and cannot be created without one, so the gate would only stand
#: between the user and the row they explicitly asked for.
CREATABLE_COLUMNS = ("title",) + CREATE_ONLY_COLUMNS + (
    "points_possible", "grading_type", "submission_types",
    "allowed_attempts", "peer_reviews",
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
    #: The 1-based line each row was read from, parallel to `rows`.
    #:
    #: **Carried beside the rows rather than on `InfoRow`**, because `InfoRow`'s
    #: fields ARE the columns (`COLUMNS` is derived from them), so a field added
    #: there would add a column to everybody's CSV.
    #:
    #: This exists so `claim_new_row` can put a created assignment's id back in
    #: the exact cell it came from. Matching by position among the `NEW` rows
    #: would be wrong the moment one create fails: the failed row stays `NEW`,
    #: and the next id would land on it.
    lines: tuple[int, ...] = ()

    def specifies(self, column: str) -> bool:
        return column in self.columns

    @property
    def new_rows(self) -> list[tuple[int, InfoRow]]:
        """The rows asking to be created, each with the line it came from."""
        return [(line, row) for line, row in zip(self.lines, self.rows)
                if row.is_new]

    @property
    def editable_present(self) -> tuple[str, ...]:
        return tuple(c for c in EDITABLE_COLUMNS if c in self.columns)

    def by_key(self) -> dict[str, InfoRow]:
        return {row.key: row for row in self.rows}


def read_sheet(path: Path) -> InfoSheet:
    """Read an infosheet back, tolerating the preamble and spreadsheet re-saves."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        records: list[list[str]] = []
        # `line_num` counts lines consumed from the file, so it stays correct
        # when a quoted cell spans several -- which a spreadsheet will produce
        # from a description or a multi-line title. Recorded per record because
        # `claim_new_row` edits one line in place and must not guess which.
        ends_at: list[int] = []
        for record in reader:
            records.append(record)
            ends_at.append(reader.line_num)

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
    lines: list[int] = []
    for offset, record in enumerate(records[start + 1:], start=start + 1):
        line = ends_at[offset]
        if not any(cell.strip() for cell in record):
            continue  # A trailing blank line a spreadsheet left behind.
        values = dict(zip(header, record))
        data = {c: (values.get(c) or "").strip() for c in present}
        if not data.get("assignment_id"):
            raise SheetError(
                f"{path} line {line} has no assignment_id. Rows are matched on "
                f"it alone -- a row without one cannot be matched to anything, "
                f"and guessing would target the wrong assignment. To create a "
                f"new assignment, put {NEW} in the cell rather than leaving it "
                f"empty."
            )
        rows.append(InfoRow(**data))
        lines.append(line)

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
        lines=tuple(lines),
    )


def _major(version: str) -> str:
    return (version or "").split(".", 1)[0].strip()


class WriteBackFailed(Exception):
    """The created assignment's id could not be recorded in the sheet.

    Always serious. The assignment **exists in Canvas** by the time this can be
    raised, and the row still says `NEW` -- so a second push would create it
    again. The caller must stop rather than continue.
    """


def _first_field(text: str) -> tuple[str, str]:
    """Split a raw CSV line into its first field and the rest, quotes honoured.

    Hand-rolled rather than run through `csv`, because the point is to leave
    every *other* byte of the line exactly as it was: re-emitting the row
    through `csv.writer` would re-decide its quoting, and a sheet the user has
    open would then show a diff nobody asked for.
    """
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return text[:index], text[index:]
    return text, ""


def claim_new_row(path: Path, line: int, assignment_id: str) -> None:
    """Replace one `NEW` cell with a real assignment id, changing nothing else.

    **Surgical on purpose.** The sheet's contract lets a user delete any column
    and any row, so rebuilding the file from the parsed model would hand those
    columns back and silently undo their edit. Only the first field of the named
    line is touched; every other byte, including quoting and line endings, is
    written back as it was read.

    Raises `WriteBackFailed` when the line is not where it was, or no longer
    says `NEW`. **That refusal is the point, not an inconvenience:** it is what a
    spreadsheet re-saving the file underneath us looks like, and writing an id
    into whatever now occupies that line would put a real assignment's id on the
    wrong row.
    """
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise WriteBackFailed(f"{path} could not be re-read: {exc}") from exc

    lines = text.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        raise WriteBackFailed(
            f"{path} no longer has a line {line} (it has {len(lines)}). The "
            f"file changed while the push was running; assignment "
            f"{assignment_id} was created but its id is NOT recorded."
        )

    head, rest = _first_field(lines[line - 1])
    if not is_new(head.strip().strip('"')):
        raise WriteBackFailed(
            f"{path} line {line} now reads {head.strip()!r}, not {NEW}. The "
            f"file changed while the push was running; assignment "
            f"{assignment_id} was created but its id is NOT recorded."
        )

    lines[line - 1] = assignment_id + rest
    try:
        with path.open("w", newline="", encoding="utf-8") as handle:
            handle.write("".join(lines))
    except OSError as exc:
        raise WriteBackFailed(
            f"{path} could not be written: {exc}. Assignment {assignment_id} "
            f"was created but its id is NOT recorded."
        ) from exc


#: How the editors a person actually opens a CSV in announce that they hold it.
#: `<name>` is the sheet's own filename; `<stem>` is it without the extension.
LOCK_PATTERNS = (
    (".~lock.{name}#", "LibreOffice"),
    ("~${name}", "Excel"),
    ("~${stem}.xlsx", "Excel"),
    (".{name}.swp", "vim"),
)


def find_lock(path: Path) -> tuple[Path, str] | None:
    """An editor's lock file for this sheet, and which editor left it.

    **Checked before any assignment is created**, because the write-back that
    follows a create is `push`'s first write to a file the user owns. The user's
    stated workflow is a spreadsheet, and a spreadsheet holds the file open: if
    they are still in it when the id lands, their next save puts `NEW` back and
    the following push creates a **duplicate assignment**. That is the exact
    failure the `NEW` marker exists to prevent, arriving through the back door.

    A lock file is evidence, not proof -- a crashed editor leaves a stale one,
    and an editor nobody listed here leaves none. So this is one of the two
    guards the user chose (2026-09-10); the other is re-reading the sheet after
    each write-back, which catches what this misses.
    """
    for pattern, editor in LOCK_PATTERNS:
        candidate = path.parent / pattern.format(name=path.name, stem=path.stem)
        if candidate.exists():
            return candidate, editor
    return None


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
        # **`NEW` rows are exempt, and must be.** Creating five assignments is
        # five rows all reading NEW; the duplicate check exists because two rows
        # naming one assignment leave no way to tell which wins, and two rows
        # asking for a new assignment have no such ambiguity -- they are two
        # different assignments.
        if row.is_new:
            continue
        if row.key in seen:
            repeated.append(row.key)
        seen.add(row.key)
    return repeated
