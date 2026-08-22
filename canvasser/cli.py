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
from .assignments import format_in_course_time, pull_course, read_specific
from .courses import Scope, apply_scope, fetch_courses, fetch_one
from .datesheet import read_sheet, write_sheet
from .display import (
    print_course_table,
    render_diff,
    render_features,
    render_general,
    render_nav,
    render_sections,
)
from .push import (
    FIELD_PAIRS,
    check_course,
    check_timezone,
    compare,
    to_minute,
    describe_scope,
)
from .settings import fetch_settings
from .writer import (
    WriteFailed,
    WriteRefused,
    apply_changes,
    verify,
)
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



def _resolve_course(page, config, args):
    """Shared course resolution: fetch once, scope, search, report widening.

    Scope governs *browsing*, not *explicit selection* -- `select_course`
    applies the scope itself and widens it when a name finds nothing there.
    """
    # An exact numeric id needs no search: go straight to the course. Listing
    # every course to find one whose number we were handed is the same mistake
    # as reading every assignment to compare one row.
    if args.course and str(args.course).strip().isdigit():
        direct = fetch_one(page, config, str(args.course).strip())
        if direct is not None:
            print(f"  Course: {direct.id}  {direct.name}  (from course id)",
                  file=sys.stderr)
            return direct
        print(f"  Note: course id {args.course} did not resolve directly; "
              f"falling back to the full list.", file=sys.stderr)

    courses = fetch_courses(page, config)
    course, source, notes = select_course(
        courses,
        course=args.course,
        course_file=Path(args.course_file) if args.course_file else None,
        scope=scope_from_args(args),
        allow_prompt=not args.no_prompt,
    )
    # Report an expansion *before* acting on the result -- the user asked to
    # know when they are getting a course from outside what they asked for.
    for note in notes:
        print(f"  Note: {note}", file=sys.stderr)
    print(f"  Course: {course.id}  {course.name}  (from {source})", file=sys.stderr)
    return course


#: Display selectors, in the order they render when several are asked for.
#: `--general` is the default when none is named.
DISPLAYS = (
    ("general", render_general),
    ("features", render_features),
    ("sections", render_sections),
    ("nav_active", lambda s: render_nav(s, enabled=True)),
    ("nav_disabled", lambda s: render_nav(s, enabled=False)),
)


def cmd_settings(args: argparse.Namespace) -> int:
    """Show a course's Details, Sections and Navigation on screen."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()

    chosen = [name for name, _ in DISPLAYS if getattr(args, name)]
    if not chosen:
        chosen = ["general"]

    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        course = _resolve_course(page, config, args)
        settings = fetch_settings(page, config, course.id)

    for name, render in DISPLAYS:
        if name in chosen:
            print()
            print("\n".join(render(settings)))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull assignment due dates for one course into a CSV."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()

    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)

        course = _resolve_course(page, config, args)
        # One extra page load, for the timezone's human name. Canvas hands out
        # the IANA id on every assignment page but its friendly label only on
        # the settings page, and the sheet carries both -- the label for the
        # person editing it, the id for push to resolve times through.
        course_settings = fetch_settings(page, config, course.id)
        rows, course_tz = pull_course(page, config, course.id, limit=args.limit)

    out_path = Path(args.out or f"dates-{course.id}.csv")
    write_sheet(
        rows,
        out_path,
        course_id=course.id,
        timezone=course_settings.timezone_label,
        iana=course_tz or course_settings.course_timezone,
    )

    dated = sum(1 for r in rows if r.due_date)
    opens = sum(1 for r in rows if r.open_date)
    closes = sum(1 for r in rows if r.close_date)
    assignments = len({r.assignment_id for r in rows})
    section_rows = sum(1 for r in rows if not r.is_base_row)

    print(
        f"\nWrote {len(rows)} row(s) to {out_path}"
        f"\n  timezone: {course_settings.timezone_label or '(unknown)'} "
        f"({course_tz or '?'}) -- all times are course-local, no per-value offset"
        f"\n  {assignments} assignment(s), {section_rows} per-section row(s)"
        f"\n  dates set: {dated} due, {opens} open, {closes} close"
    )
    undated = sum(1 for r in rows if not r.has_any_date)
    if undated:
        print(f"  {undated} row(s) have no dates at all.")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Compare an edited datesheet against the live course.

    Preview by default; `--commit` writes. Everything up to the commit block
    is read-only, so the common case -- running this repeatedly while editing a
    sheet -- cannot touch the course.
    """
    sheet_path = Path(args.sheet)
    sheet = read_sheet(sheet_path)
    print(
        f"  Sheet: {sheet_path}  (v{sheet.version or '?'}, "
        f"course={sheet.course_id or '?'}, {len(sheet.rows)} row(s))\n"
        f"  Times read as: {sheet.timezone or sheet.iana or 'the course zone'}\n"
        f"  Sheet can change: {describe_scope(sheet)}",
        file=sys.stderr,
    )
    # Ambiguous day/month cells are reported before anything else happens: the
    # user can then check the two or three that mattered instead of re-reading
    # the whole sheet, and a wrong assumption is caught before a write exists.
    for warning in sheet.warnings:
        print(f"  AMBIGUOUS DATE: {warning}", file=sys.stderr)

    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        # The sheet names its own course, so there is nothing to resolve --
        # and resolving one from flags would invite pushing into the wrong
        # class. An explicit course argument is only honoured as a cross-check.
        course_id = args.course or sheet.course_id
        if not course_id:
            raise CourseSelectionError(
                f"{sheet_path} does not record a course and none was given. "
                f"Re-run `canvasser pull` to regenerate it with a header."
            )
        check_course(sheet, course_id)

        # Straight to the assignments the sheet names. Reading the whole
        # course to compare a handful of rows cost ~90s of page loads to learn
        # nothing about the other 30-odd.
        targets = sorted({row.assignment_id for row in sheet.rows})
        print(
            f"\n  Reading {len(targets)} assignment(s) named by the sheet. "
            f"This is the read pass -- nothing is being changed.\n",
            file=sys.stderr,
        )
        current, course_tz = read_specific(page, config, course_id, targets)

    # The sheet's times are bare wall clocks; its `iana=` header is the only
    # thing that says what they mean. Checked before the diff, because a zone
    # change makes every comparison in it meaningless.
    check_timezone(sheet, course_tz)

    diff = compare(sheet, current)
    print()
    print("\n".join(render_diff(diff, sheet)))

    if not args.commit:
        return 0 if diff.is_empty else 1
    if diff.is_empty:
        print("\nNothing to commit.")
        return 0

    # Everything above this line is read-only. Everything below writes.
    source_tz = sheet.iana or course_tz
    if not source_tz:
        raise ConfigError(
            "No timezone for the sheet's times: row 1 has no `iana=` and the "
            "course did not report one. Re-run `canvasser pull` to regenerate "
            "the sheet with a header."
        )

    by_key = sheet.by_key()
    print(f"\nCommitting to course {course_id}. Times read as {source_tz}.")
    failures = 0
    with open_page(headless=not args.headed) as page:
        ensure_logged_in(page, config, approver)
        for row in diff.changed:
            wanted = by_key[row.key]
            if not wanted.is_base_row:
                print(f"  SKIP {row.title}: override rows are not written yet.",
                      file=sys.stderr)
                failures += 1
                continue
            changes = {
                change.field: (
                    getattr(wanted, date_column), getattr(wanted, time_column)
                )
                for change in row.changes
                for field, date_column, time_column in FIELD_PAIRS
                if field == change.field
            }
            try:
                apply_changes(page, config, course_id, row.key[0], changes, source_tz)
                after = verify(page, config, course_id, row.key[0])
            except (WriteRefused, WriteFailed) as exc:
                print(f"  REFUSED {row.title}: {exc}", file=sys.stderr)
                failures += 1
                continue
            print(f"  {row.title}  #{row.key[0]}")
            for field, _, _ in FIELD_PAIRS:
                if field in changes:
                    got = format_in_course_time(after.get(field) or "", course_tz)
                    asked = " ".join(p for p in changes[field] if p)
                    # Compare date AND time. An earlier version tested only
                    # `startswith(date)`, which would have reported OK for a
                    # value written an hour -- or fourteen hours -- off, which
                    # is the exact failure the timezone handling exists to
                    # prevent. The offset suffix is dropped before comparing;
                    # the sheet does not carry one.
                    # Minute precision: Canvas keeps its own seconds and the
                    # form cannot express them. See push.to_minute.
                    ok = to_minute(got.rsplit(" ", 1)[0]) == to_minute(asked)
                    print(f"      {field:<11}{asked:<22} -> Canvas now: {got}"
                          f"   {'OK' if ok else 'MISMATCH'}")
                    failures += 0 if ok else 1

    if failures:
        print(f"\n{failures} field(s)/row(s) did not land. Nothing was retried.",
              file=sys.stderr)
        return 2
    print("\nCommitted.")
    return 0


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    """Three independent scope axes, shared by `courses`, `pull`, `settings`.

    **Opposed flags union rather than conflict.** `--active --archived` asks
    for both, which is what the words say; an earlier version made each pair
    mutually exclusive and errored instead. Only the favorite axis defaults to
    a winnowed field -- deliberately, it is the axis the user curates in
    Canvas itself.
    """
    scope = parser.add_argument_group(
        "scope",
        "Three independent axes. Naming both sides of an axis unions them "
        "(--active --archived is every enrollment). Enrollment and publish "
        "state default to everything; favorites are the one winnowed default.",
    )
    scope.add_argument("--active", action="store_true", help="current enrollments")
    scope.add_argument("--archived", action="store_true", help="past enrollments")
    scope.add_argument("--published", action="store_true", help="published courses")
    scope.add_argument("--unpublished", action="store_true", help="unpublished courses")
    scope.add_argument(
        "--favorite", action="store_true", help="starred courses (the default)"
    )
    scope.add_argument(
        "--unmarked", action="store_true", help="only courses NOT starred in Canvas"
    )
    scope.add_argument(
        "--all",
        dest="all_courses",
        action="store_true",
        help="everything: both sides of all three axes",
    )


def scope_from_args(args: argparse.Namespace) -> Scope:
    """Build a Scope from parsed flags.

    `--all` names **both sides of every axis** (user, 2026-08-21), so it is the
    widest possible scope rather than a favorites-only shorthand.

    Consequence worth knowing: because opposed flags union, pairing `--all`
    with a narrowing flag does not narrow anything -- `--all --published` is
    every course, not every published course, since `--all` has already named
    `--unpublished` too. That falls straight out of the union rule.
    """
    everything = args.all_courses
    return Scope(
        active=args.active or everything,
        archived=args.archived or everything,
        published=args.published or everything,
        unpublished=args.unpublished or everything,
        favorite=args.favorite or everything,
        unmarked=args.unmarked or everything,
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

    settings = sub.add_parser(
        "settings",
        help="show a course's Details, Sections and Navigation",
        description="Reads the Settings page and prints Course Details, "
        "Sections, and which navigation items students can actually see. "
        "Course may be an id or a name fragment, resolved exactly as `pull` "
        "does.",
    )
    settings.add_argument(
        "course", nargs="?", help="course id or name fragment (see: canvasser courses)"
    )
    settings.add_argument(
        "--course-file", metavar="PATH", help="read the course id/name from a file"
    )
    shown = settings.add_argument_group(
        "displays",
        "Choose one or more. With none named, --general is shown.",
    )
    shown.add_argument(
        "--general", action="store_true", help="course information (the default)"
    )
    shown.add_argument(
        "--features", action="store_true", help="features, interface and visibility"
    )
    shown.add_argument("--sections", action="store_true", help="section information")
    shown.add_argument(
        "--nav-active", dest="nav_active", action="store_true",
        help="enabled navigation elements",
    )
    shown.add_argument(
        "--nav-disabled", dest="nav_disabled", action="store_true",
        help="disabled navigation elements",
    )
    add_scope_args(settings)

    push = sub.add_parser(
        "push",
        help="compare an edited datesheet against the live course",
        description="Reads a datesheet, re-reads the course, and reports what "
        "would change. Writes nothing: this is the preview half of the round "
        "trip, and pull -> push with no edits must report zero changes.",
    )
    push.add_argument("sheet", help="path to a datesheet CSV")
    push.add_argument(
        "--course", help="cross-check: refuse if the sheet names a different course"
    )
    push.add_argument(
        "--limit", type=int, help="only the first N assignments (for a quick check)"
    )
    push.add_argument(
        "--commit", action="store_true",
        help="actually write the changes (default is preview only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "status": cmd_status,
        "login": cmd_login,
        "courses": cmd_courses,
        "pull": cmd_pull,
        "settings": cmd_settings,
        "push": cmd_push,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, CourseSelectionError, LoginError, ApprovalError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
