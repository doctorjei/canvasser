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
from dataclasses import asdict, dataclass, fields
from pathlib import Path

#: Written into every file so a later `push` can reject a sheet it does not
#: understand instead of misreading columns that moved.
SCHEMA_VERSION = "2"

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


def write_sheet(rows: list[AssignmentRow], path: Path) -> Path:
    """Write the CSV, newest pull wins.

    `newline=""` is required by the csv module on every platform; without it you
    get blank lines between records that spreadsheets happily import as garbage.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write(f"# canvasser datesheet v{SCHEMA_VERSION}\n")
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return path


def read_sheet(path: Path) -> list[AssignmentRow]:
    """Read a CSV back, tolerating the comment line and spreadsheet re-saves."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        lines = [line for line in handle if not line.startswith("#")]

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
    return rows


def _duplicate_keys(rows: list[AssignmentRow]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    repeated: list[tuple[str, str]] = []
    for row in rows:
        if row.key in seen:
            repeated.append(row.key)
        seen.add(row.key)
    return repeated
