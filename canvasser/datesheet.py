"""The CSV that carries assignment due dates out to a spreadsheet and back.

Scope: **due date only** -- no points, submission types, or unlock/lock dates.
Originally also "first assign-to group only", but live data killed that: in the
user's own course, 11 of 17 assignments have no base due date at all and every
real date lives in 12 per-section overrides. A first-group-only sheet would have
been a page of blanks. So each assign-to target now gets its own row.

Row identity is `(assignment_id, override_id)`, never the title:

  editable   due_at
  read-only  assignment_id, override_id, title, assign_to

`override_id` empty means the assignment's own base row ("Everyone" / "Everyone
else"); a value means that specific override. Read-only columns are still
written, because a sheet without titles is unusable by a human -- they are just
not honored on the way back in. Editing a title in the spreadsheet must never
retarget a write.
"""

from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

#: Written into every file so a later `push` can reject a sheet it does not
#: understand instead of misreading columns that moved.
SCHEMA_VERSION = "2"

#: The header comment also records the course the sheet came from. Without it,
#: nothing stops a sheet pulled from one section being pushed into another --
#: the assignment ids simply would not match, and "no rows matched" is a far
#: worse failure than "this sheet belongs to a different course".
HEADER = re.compile(r"#\s*canvasser datesheet v(?P<version>\S+)(?:\s+course=(?P<course>\d+))?")

EDITABLE_COLUMNS = ("due_at",)


@dataclass(frozen=True)
class AssignmentRow:
    assignment_id: str
    override_id: str
    title: str
    assign_to: str
    due_at: str

    @property
    def key(self) -> tuple[str, str]:
        """What push must match on. Stable across title and date edits."""
        return (self.assignment_id, self.override_id)

    @property
    def is_base_row(self) -> bool:
        """True for the assignment's own dates rather than an override."""
        return not self.override_id


COLUMNS = tuple(f.name for f in fields(AssignmentRow))


def write_sheet(
    rows: list[AssignmentRow], path: Path, course_id: str | None = None
) -> Path:
    """Write the CSV, newest pull wins.

    `newline=""` is required by the csv module on every platform; without it you
    get blank lines between records that spreadsheets happily import as garbage.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        stamp = f"# canvasser datesheet v{SCHEMA_VERSION}"
        if course_id:
            stamp += f" course={course_id}"
        handle.write(stamp + "\n")
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return path


@dataclass(frozen=True)
class Sheet:
    """A parsed datesheet: its rows plus where they came from."""

    rows: list[AssignmentRow]
    course_id: str | None
    version: str | None
    path: Path

    def by_key(self) -> dict[tuple[str, str], AssignmentRow]:
        return {row.key: row for row in self.rows}


def read_sheet(path: Path) -> Sheet:
    """Read a CSV back, tolerating the comment line and spreadsheet re-saves."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        raw = handle.readlines()

    course_id = version = None
    for line in raw:
        found = HEADER.match(line.strip())
        if found:
            version = found.group("version")
            course_id = found.group("course")
            break

    lines = [line for line in raw if not line.lstrip().startswith("#")]

    rows: list[AssignmentRow] = []
    for record in csv.DictReader(lines):
        missing = [c for c in COLUMNS if c not in record]
        if missing:
            raise ValueError(
                f"{path} is missing column(s) {', '.join(missing)}. "
                f"Expected: {', '.join(COLUMNS)}"
            )
        rows.append(
            AssignmentRow(
                assignment_id=(record["assignment_id"] or "").strip(),
                override_id=(record["override_id"] or "").strip(),
                title=(record["title"] or "").strip(),
                assign_to=(record["assign_to"] or "").strip(),
                due_at=(record["due_at"] or "").strip(),
            )
        )

    duplicates = _duplicate_keys(rows)
    if duplicates:
        raise ValueError(
            f"{path} has repeated row key(s): {duplicates}. Each "
            f"(assignment_id, override_id) must appear once -- otherwise push "
            f"cannot tell which row wins."
        )
    if version and version != SCHEMA_VERSION:
        raise ValueError(
            f"{path} is datesheet v{version}; this build writes and reads "
            f"v{SCHEMA_VERSION}. Re-run `canvasser pull` to regenerate it."
        )
    return Sheet(rows=rows, course_id=course_id, version=version, path=path)


def _duplicate_keys(rows: list[AssignmentRow]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    repeated: list[tuple[str, str]] = []
    for row in rows:
        if row.key in seen:
            repeated.append(row.key)
        seen.add(row.key)
    return repeated
