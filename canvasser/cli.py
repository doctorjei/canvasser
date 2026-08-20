"""Command-line entry point.

Usage:
    python -m canvasser status     # is the stored session still authenticated?
    python -m canvasser login      # authenticate (prompts Duo if needed)
    python -m canvasser courses    # list courses, by navigating as a person would
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .auth import LoginError, ensure_logged_in, is_logged_in, quiesce
from .browser import open_page
from .assignments import pull_course
from .courses import list_courses
from .datesheet import write_sheet
from .config import ConfigError, load_config
from .duo import APPROVERS, ApprovalError


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    with open_page(headless=not args.headed) as page:
        alive = is_logged_in(page, config)
    print("Session: ALIVE (authenticated)" if alive else "Session: DEAD (login required)")
    return 0 if alive else 1


def cmd_login(args: argparse.Namespace) -> int:
    config = load_config()
    approver = APPROVERS[args.factor]()
    with open_page(headless=not args.headed) as page:
        performed = ensure_logged_in(page, config, approver)
    print("Logged in." if performed else "Already logged in; nothing to do.")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    """Read the course list off the Courses page, as a person would see it."""
    config = load_config()
    approver = APPROVERS[args.factor]()
    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        courses = list_courses(page, config, include_archived=args.archived)

    if args.filter:
        courses = [c for c in courses if c.matches(args.filter)]
    if args.teaching:
        courses = [c for c in courses if c.role.lower() == "teacher"]

    if not courses:
        print("No courses matched.")
        return 1

    print(f"{len(courses)} course(s):\n")
    for course in courses:
        flag = " " if course.published else "*"
        print(f"  {course.id:>8}{flag} {course.term:<16} {course.role:<12} {course.name}")
    print("\n  (* = unpublished)")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull assignment due dates for one course into a CSV."""
    config = load_config()
    approver = APPROVERS[args.factor]()
    out_path = Path(args.out or f"dates-{args.course}.csv")

    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        rows = pull_course(page, config, args.course, limit=args.limit)

    write_sheet(rows, out_path)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="canvasser", description=__doc__)
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

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="check whether the stored session is still valid")
    sub.add_parser("login", help="authenticate, prompting for Duo if needed")

    courses = sub.add_parser("courses", help="list courses (with ids) from the Courses page")
    courses.add_argument("--filter", help="substring match against course name or term")
    courses.add_argument(
        "--teaching", action="store_true", help="only courses where the role is Teacher"
    )
    courses.add_argument(
        "--archived",
        action="store_true",
        help="also include past enrollments (default: current courses only)",
    )

    pull = sub.add_parser("pull", help="pull assignment due dates for a course into CSV")
    pull.add_argument("course", help="course id (see: canvasser courses)")
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
    except (ConfigError, LoginError, ApprovalError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
