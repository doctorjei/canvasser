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

from .courses import Scope, apply_scope
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


class AmbiguousCourse(CourseSelectionError):
    """Several courses matched. Listing them and stopping is the whole point.

    Distinct from a plain selection failure because the caller must not treat
    it as "keep looking": a wider scope can only add more matches, and the user
    asked to be shown the collision rather than have one silently picked.
    """


def search_course(
    all_courses: list, value: str, scope: Scope
) -> tuple[object, list[str]]:
    """Resolve `value` to one course, widening scope only if it finds nothing.

    Returns (course, notes) where `notes` describes any scope expansion, for
    the caller to report *before* it acts on the result.

    Two rules from the user, in order:

    1. **An exact course id always wins, at any scope.** Being told that
       574855 does not exist because it happens to be unstarred is absurd, and
       ids are unique so this can never be ambiguous.
    2. **A name searches the current scope first**, and only widens when that
       finds nothing -- publish state, then enrollment, then favourites. Each
       widening is cumulative and is reported.

    Ambiguity terminates at whatever scope produced it. Widening past a
    collision would only grow it, and picking one would be a guess about which
    real class to touch.
    """
    value = value.strip()

    # Rule 1, before any scope reasoning: ids are exact and unique.
    exact = [c for c in all_courses if c.id == value]
    if exact:
        course = exact[0]
        in_scope = any(c.id == course.id for c in apply_scope(all_courses, scope))
        notes = (
            []
            if in_scope
            else [f"course id {course.id} is outside the current scope "
                  f"({scope.describe()}); matched it anyway"]
        )
        return course, notes

    # Rule 2: widen one axis at a time, in the user's stated order.
    ladder: list[tuple[Scope, str | None]] = [(scope, None)]
    current = scope
    for is_widest, relax, label in (
        ("publish_is_widest", Scope.relax_publish, "publish state"),
        ("enrollment_is_widest", Scope.relax_enrollment, "enrollment (current and past)"),
        ("favorites_is_widest", Scope.relax_favorites,
         "favorites (now including unstarred courses)"),
    ):
        # Skip an axis already wide open -- re-searching an identical set would
        # report an "expansion" that changed nothing. Test the *meaning*, not
        # the object: on the symmetric axes "both flags set" and "neither set"
        # are different Scopes that select exactly the same courses.
        if getattr(current, is_widest):
            continue
        current = relax(current)
        ladder.append((current, label))

    widened_by: list[str] = []
    for step_scope, label in ladder:
        if label is not None:
            widened_by.append(label)
        matches = match_courses(apply_scope(all_courses, step_scope), value)

        if len(matches) == 1:
            notes = []
            if widened_by:
                notes.append(
                    f"no match in the current scope ({scope.describe()}); "
                    f"widened {' then '.join(widened_by)}"
                )
            return matches[0], notes

        if len(matches) > 1:
            where = (
                "the current scope"
                if not widened_by
                else f"scope widened by {' then '.join(widened_by)}"
            )
            listed = "\n".join(f"    {describe(c)}" for c in matches[:10])
            more = "" if len(matches) <= 10 else f"\n    ... and {len(matches) - 10} more"
            raise AmbiguousCourse(
                f"{len(matches)} courses match {value!r} in {where}.\n"
                f"Name a course id, or narrow the text:\n{listed}{more}"
            )

    raise CourseSelectionError(
        f"No course matches {value!r}, at any scope -- the search widened from "
        f"{scope.describe()} all the way out to every course on the account.\n"
        f"Run `canvasser courses --all` to see them, or use a course id."
    )


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
    all_courses: list,
    *,
    course: str | None = None,
    course_file: Path | None = None,
    scope: Scope | None = None,
    allow_prompt: bool = True,
) -> tuple[object, str, list[str]]:
    """Resolve to exactly one course. Returns (course, source, notes).

    `all_courses` is the *unfiltered* list; scope is applied here rather than
    by the caller, because the search has to be able to widen it. The
    interactive picker still sees only the scoped set -- narrowing is the
    entire point of a picker.
    """
    scope = scope or Scope()
    resolved = resolve_course_value(course=course, course_file=course_file)

    if resolved is None:
        if not allow_prompt:
            raise CourseSelectionError(
                "No course specified and prompting is disabled (--no-prompt)."
            )
        return choose_interactively(apply_scope(all_courses, scope)), "prompt", []

    value, source = resolved
    try:
        course_obj, notes = search_course(all_courses, value, scope)
    except CourseSelectionError as exc:
        # Name the source so a stale $CANVASSER_COURSE or --course-file is
        # obvious rather than looking like a bad argument.
        raise type(exc)(f"{exc}\n(course requested via {source})") from None
    return course_obj, source, notes
