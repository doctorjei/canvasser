"""Command-line entry point.

    canvasser status              # is the stored session still authenticated?
    canvasser login               # authenticate (prompts Duo if needed)
    canvasser courses --teaching  # list courses with their ids
    canvasser pull <course_id>    # assignment due dates -> CSV

Credentials come from the first source that has them: --secrets-file, the
environment, the vault file, then a prompt. There is no password flag -- argv is
not private -- so use a file, an env var, or sshpass for scripted runs. Run with
-v to see which source was used (never the value itself).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .auth import LoginError, ensure_logged_in, is_logged_in, quiesce
from .browser import open_page
from .assignments import pull_course
from .courses import Scope, apply_scope, fetch_courses
from .datesheet import write_sheet
from .selection import COURSE_VAR, CourseSelectionError, select_course
from .config import Config, ConfigError, ENV_FILE, load_config
from .credentials import PASSWORD_VAR, USERNAME_VAR
from .duo import APPROVERS, ApprovalError


def config_from_args(args: argparse.Namespace) -> "Config":
    """Build a Config from the credential flags common to every subcommand.

    Prompting is suppressed automatically when there is no terminal, so an
    unattended run fails with an actionable message instead of hanging forever
    on a prompt nobody will ever see.
    """
    config = load_config(
        username=args.username,
        secrets_file=Path(args.secrets_file) if args.secrets_file else None,
        allow_prompt=not args.no_prompt,
    )
    if args.verbose:
        print(f"  Credentials: {config.credential_sources}", file=sys.stderr)
    return config


def cmd_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    with open_page(headless=not args.headed) as page:
        alive = is_logged_in(page, config)
    print("Session: ALIVE (authenticated)" if alive else "Session: DEAD (login required)")
    return 0 if alive else 1


def cmd_login(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(headless=not args.headed) as page:
        performed = ensure_logged_in(page, config, approver)
    print("Logged in." if performed else "Already logged in; nothing to do.")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    """Read the course list off the Courses page, as a person would see it."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        courses = apply_scope(fetch_courses(page, config), scope_from_args(args))

    if args.filter:
        courses = [c for c in courses if c.matches(args.filter)]
    if args.teaching:
        courses = [c for c in courses if c.role.lower() == "teacher"]

    if not courses:
        print("No courses matched.")
        return 1

    print_course_table(courses)
    return 0


#: Column widths, to the user's spec. Every gap is two spaces; the total is 79,
#: so the table fits an 80-column terminal without wrapping.
#:
#: Term is 13 because "Summer C 2026" is exactly that long. "Development Term"
#: (16) still truncates -- covering it would cost three characters from Course
#: Name, and those shells rarely appear in the default (favorites) view.
COLUMNS = (("ID Num", 6), ("Fav", 3), ("Pub", 3), ("Course Name", 32),
           ("Term", 13), ("Role(s)", 12))
GAP = "  "

BOLD_UNDERLINE_WHITE = "\033[1;4;97m"
NORMAL_WHITE = "\033[37m"
#: Publish state carries real consequence -- an unpublished course is invisible
#: to students -- so it gets the loudest treatment in the table.
BRIGHT_BOLD_GREEN = "\033[1;92m"
BRIGHT_BOLD_RED = "\033[1;91m"
RESET = "\033[0m"


def _colors_enabled() -> bool:
    """Colour only a real terminal, and honour NO_COLOR.

    Escape codes piped into a file or a grep are noise, and this output is
    plausibly something the user will pipe.
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _fit(text: str, width: int, centre: bool = False) -> str:
    """Pad to width, truncating with an ellipsis rather than overflowing."""
    text = text or ""
    if len(text) > width:
        # rstrip first, or a cut landing on a space leaves "Development …".
        text = text[: width - 1].rstrip() + "…"
    return text.center(width) if centre else text.ljust(width)


def print_course_table(courses: list) -> None:
    header = GAP.join(_fit(label, width) for label, width in COLUMNS)
    colour = _colors_enabled()
    print(f"{BOLD_UNDERLINE_WHITE if colour else ''}{header}{RESET if colour else ''}")

    for course in courses:
        mark = "✓" if course.published else "✗"
        state = BRIGHT_BOLD_GREEN if course.published else BRIGHT_BOLD_RED
        pub = _fit(mark, 3, centre=True)
        if colour:
            # Return to the row colour after the mark, or the rest of the line
            # would fall back to the terminal default.
            pub = f"{state}{pub}{RESET}{NORMAL_WHITE}"

        row = GAP.join(
            (
                _fit(course.id, 6),
                _fit("★" if course.favorite else "", 3, centre=True),
                pub,
                _fit(course.name, 32),
                _fit(course.term, 13),
                _fit(course.role, 12),
            )
        )
        print(f"{NORMAL_WHITE if colour else ''}{row}{RESET if colour else ''}")

    plural = "course" if len(courses) == 1 else "courses"
    print(f"\n[{len(courses)} {plural}]")


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull assignment due dates for one course into a CSV."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()

    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)

        # Scope governs *browsing*, not *explicit selection*. Fetch everything
        # once; `select_course` applies scope itself and widens it if a name
        # finds nothing there. Both /courses tables come off one page load, so
        # widening costs no extra navigation.
        courses = fetch_courses(page, config)
        course, source, notes = select_course(
            courses,
            course=args.course,
            course_file=Path(args.course_file) if args.course_file else None,
            scope=scope_from_args(args),
            allow_prompt=not args.no_prompt,
        )
        # Report an expansion *before* acting on the result -- the user asked
        # to know they are getting a course from outside what they asked for.
        for note in notes:
            print(f"  Note: {note}", file=sys.stderr)
        print(f"  Course: {course.id}  {course.name}  (from {source})", file=sys.stderr)

        rows = pull_course(page, config, course.id, limit=args.limit)

    out_path = Path(args.out or f"dates-{course.id}.csv")
    write_sheet(rows, out_path, course_id=course.id)

    dated = sum(1 for r in rows if r.due_at)
    assignments = len({r.assignment_id for r in rows})
    section_rows = sum(1 for r in rows if not r.is_base_row)

    print(
        f"\nWrote {len(rows)} row(s) to {out_path}"
        f"\n  {assignments} assignment(s), {section_rows} per-section row(s), "
        f"{dated} with a due date"
    )
    if len(rows) != dated:
        print(f"  {len(rows) - dated} row(s) have no due date set.")
    return 0


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    """Three independent scope axes, shared by `courses` and `pull`.

    Only the favorite axis defaults to a winnowed field; that is deliberate --
    it is the axis the user curates in Canvas itself.
    """
    scope = parser.add_argument_group(
        "scope",
        "Three independent axes. Enrollment defaults to both active and "
        "archived; publish state defaults to any; favorites are the one "
        "winnowed default -- use --all to include non-favorites.",
    )
    enrollment = scope.add_mutually_exclusive_group()
    enrollment.add_argument(
        "--active", action="store_true", help="only current enrollments"
    )
    enrollment.add_argument(
        "--archived", action="store_true", help="only past enrollments"
    )

    state = scope.add_mutually_exclusive_group()
    state.add_argument("--published", action="store_true", help="only published courses")
    state.add_argument(
        "--unpublished", action="store_true", help="only unpublished courses"
    )

    scope.add_argument(
        "--all",
        dest="include_non_favorites",
        action="store_true",
        help="include courses that are not starred in Canvas",
    )


def scope_from_args(args: argparse.Namespace) -> Scope:
    return Scope(
        active=args.active,
        archived=args.archived,
        published=args.published,
        unpublished=args.unpublished,
        include_non_favorites=args.include_non_favorites,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvasser",
        description=__doc__,
        # Without this, argparse reflows the usage examples into one paragraph.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--factor",
        choices=sorted(APPROVERS),
        default="push",
        help="Duo second factor to use when a login is needed (default: push)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser (requires a display; unavailable in this box)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Report where credentials came from"
    )

    creds = parser.add_argument_group(
        "credentials",
        f"Resolved highest-precedence-first: --secrets-file, ${USERNAME_VAR}/"
        f"${PASSWORD_VAR}, {ENV_FILE}, then a prompt. There is deliberately no "
        f"password flag (ssh's rule: argv is not private). For scripted use, "
        f"sshpass answers the prompt.",
    )
    creds.add_argument("--username", help="GatorLink username")
    creds.add_argument(
        "--secrets-file",
        metavar="PATH",
        help=f"File of KEY=VALUE lines ({USERNAME_VAR}, {PASSWORD_VAR})",
    )
    creds.add_argument(
        "--no-prompt",
        action="store_true",
        help="Never prompt; fail instead. Use for unattended runs.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="check whether the stored session is still valid")
    sub.add_parser("login", help="authenticate, prompting for Duo if needed")

    courses = sub.add_parser("courses", help="list courses (with ids) from the Courses page")
    courses.add_argument("--filter", help="substring match against course name or term")
    courses.add_argument(
        "--teaching", action="store_true", help="only courses where the role is Teacher"
    )
    add_scope_args(courses)

    pull = sub.add_parser(
        "pull",
        help="pull assignment due dates for a course into CSV",
        description="Course may be an id or a name fragment. If omitted, resolves "
        f"from --course-file, then ${COURSE_VAR}, then an interactive picker.",
    )
    pull.add_argument(
        "course", nargs="?", help="course id or name fragment (see: canvasser courses)"
    )
    pull.add_argument(
        "--course-file", metavar="PATH", help="read the course id/name from a file"
    )
    add_scope_args(pull)
    pull.add_argument("--out", help="output CSV path (default: dates-<course>.csv)")
    pull.add_argument(
        "--limit", type=int, help="only the first N assignments (for a quick check)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "status": cmd_status,
        "login": cmd_login,
        "courses": cmd_courses,
        "pull": cmd_pull,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, CourseSelectionError, LoginError, ApprovalError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
