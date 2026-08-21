"""Choosing which course to operate on.

Mirrors the credential precedence shape deliberately -- one resolution idea in
this tool, not two:

    1. explicit CLI value    `pull 574855`  or  `pull "EGS1006 - Wednesday"`
    2. explicit file         --course-file PATH   (first non-comment line)
    3. environment variable  CANVASSER_COURSE
    4. interactive prompt    a numbered list of current courses

There is deliberately **no default course**. A wrong course is not a harmless
mistake once `push` exists -- it writes dates to somebody's real class -- so the
tool asks rather than assumes.

A value may be a numeric id or a name fragment. Name matching exists because
nobody remembers `574855`, but it is strict about ambiguity: several matches
means asking, never guessing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .credentials import ConfigError, _read_from_tty, tty_available

COURSE_VAR = "CANVASSER_COURSE"


class CourseSelectionError(ConfigError):
    """Raised when the course to act on cannot be determined."""


def read_course_file(path: Path) -> str:
    """Read a course id/name from a file's first meaningful line."""
    if not path.exists():
        raise CourseSelectionError(f"--course-file {path} does not exist.")
    if path.is_dir():
        raise CourseSelectionError(f"--course-file {path} is a directory.")
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise CourseSelectionError(f"--course-file {path} contains no course id or name.")


def resolve_course_value(
    *,
    course: str | None = None,
    course_file: Path | None = None,
) -> tuple[str, str] | None:
    """Return (value, source) from the non-interactive sources, or None.

    Returning None rather than raising lets the caller decide whether to prompt,
    which it can only do once it has fetched the course list.
    """
    if course:
        return course, "command line"
    if course_file is not None:
        return read_course_file(course_file), f"--course-file ({course_file})"
    if os.environ.get(COURSE_VAR):
        return os.environ[COURSE_VAR], f"${COURSE_VAR}"
    return None


def match_courses(courses: list, value: str) -> list:
    """Find courses by exact id, else by name/term substring.

    Exact id wins outright: a user who typed an id meant that course, and a name
    search that also matched it should not turn a certainty into an ambiguity.
    """
    by_id = [c for c in courses if c.id == value.strip()]
    if by_id:
        return by_id
    return [c for c in courses if c.matches(value)]


def describe(course) -> str:
    flag = "" if course.published else "  (unpublished)"
    return f"{course.id}  {course.term}  {course.name}{flag}"


def choose_interactively(courses: list) -> object:
    """Prompt with a numbered list. Requires a controlling terminal.

    Uses /dev/tty rather than stdin for the same reason the credential prompt
    does: stdin may be a pipe while a perfectly good terminal exists.
    """
    if not tty_available():
        raise CourseSelectionError(
            "No course specified and there is no controlling terminal to prompt "
            "on.\nProvide one with:\n"
            "  <course_id or name fragment>    positional argument\n"
            "  --course-file PATH              first non-comment line is used\n"
            f"  ${COURSE_VAR}                    environment variable\n"
            "Run `canvasser courses --teaching` to see the available ids."
        )
    if not courses:
        raise CourseSelectionError("No courses available to choose from.")

    print("\nSelect a course:\n", file=sys.stderr)
    for index, course in enumerate(courses, start=1):
        print(f"  {index:>3}. {describe(course)}", file=sys.stderr)

    while True:
        raw = _read_from_tty(
            f"\n  Number (1-{len(courses)}), or a name fragment: "
        ).strip()
        if not raw:
            raise CourseSelectionError("No course selected.")
        if raw.isdigit() and 1 <= int(raw) <= len(courses):
            return courses[int(raw) - 1]

        # Typing a name here is a convenience, not a second guessing layer: it
        # must land on exactly one course or we ask again.
        narrowed = match_courses(courses, raw)
        if len(narrowed) == 1:
            return narrowed[0]
        if not narrowed:
            print(f"  No course matches {raw!r}.", file=sys.stderr)
        else:
            print(f"  {len(narrowed)} courses match {raw!r}:", file=sys.stderr)
            for course in narrowed[:10]:
                print(f"      {describe(course)}", file=sys.stderr)


def select_course(
    courses: list,
    *,
    course: str | None = None,
    course_file: Path | None = None,
    allow_prompt: bool = True,
) -> tuple[object, str]:
    """Resolve to exactly one course. Returns (course, source)."""
    resolved = resolve_course_value(course=course, course_file=course_file)

    if resolved is None:
        if not allow_prompt:
            raise CourseSelectionError(
                "No course specified and prompting is disabled (--no-prompt)."
            )
        return choose_interactively(courses), "prompt"

    value, source = resolved
    matches = match_courses(courses, value)

    if not matches:
        raise CourseSelectionError(
            f"No current course matches {value!r} (from {source}).\n"
            f"Run `canvasser courses` to list them, or `courses --archived` if it "
            f"is a past enrollment."
        )
    if len(matches) > 1:
        listed = "\n".join(f"    {describe(c)}" for c in matches[:10])
        more = "" if len(matches) <= 10 else f"\n    ... and {len(matches) - 10} more"
        raise CourseSelectionError(
            f"{len(matches)} courses match {value!r} (from {source}). Be more "
            f"specific, or use the id:\n{listed}{more}"
        )
    return matches[0], source
