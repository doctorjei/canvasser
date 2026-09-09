"""Command-line entry point.

    canvasser status               # is the stored session still authenticated?
    canvasser login                # authenticate (prompts Duo if needed)
    canvasser courses --teaching   # list courses with their ids
    canvasser settings <course>    # details, sections, navigation
    canvasser pull <course>        # dates AND settings -> two CSVs
    canvasser pull <course> --info # just the settings sheet (--dates for the other)
    canvasser push <sheet.csv>     # what would change; writes nothing
    canvasser push <sheet.csv> --commit    # write it, verifying each field
    canvasser install-browser      # fetch Chromium (otherwise offered on first use)

Credentials come from the first source that has them: --secrets-file, the
environment, the default secrets file, then a prompt. There is no password flag
-- argv is not private -- so use a file, an env var, or sshpass for scripted
runs. Run with -v to see which source was used (never the value itself).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .auth import LoginError, ensure_logged_in, is_logged_in
from .browser import (
    DOWNLOAD_SIZE,
    BrowserUnavailable,
    install_chromium,
    open_page,
)
from .assignments import (
    format_in_course_time,
    pull_course,
    read_specific,
    read_specific_info,
)
from .courses import Scope, apply_scope, fetch_courses, fetch_one
from .dateparse import DateFormatError
from .datesheet import SheetError, read_sheet, write_sheet
from .infosheet import (
    DATES as SHEET_DATES,
    read_sheet as read_info_sheet,
    INFO as SHEET_INFO,
    identify as identify_sheet,
    write_sheet as write_info_sheet,
)
from .progress import course_heading, glyphs_for, session_banner, stat_box
from .display import (
    print_course_table,
    render_diff,
    render_info_diff,
    render_features,
    render_general,
    render_nav,
    render_sections,
)
from .push import (
    FIELD_PAIRS,
    WRITABLE_INFO_FIELDS,
    align_timezone,
    check_course,
    check_info_course,
    check_order,
    compare,
    compare_info,
    same_value,
    to_minute,
    describe_scope,
)
from .settings import fetch_settings
from .writer import (
    WriteFailed,
    WriteRefused,
    apply_changes,
    apply_settings,
    verify,
    verify_settings,
)
from .selection import COURSE_VAR, CourseSelectionError, select_course
from .config import (
    DEFAULT_INSTITUTION_SETTING,
    ENV_FILE,
    INSTITUTION_VAR,
    Config,
    ConfigError,
    load_config,
)
from .credentials import (
    PASSWORD_VAR,
    USERNAME_VAR,
    _read_from_tty,
    tty_available,
)
from .duo import APPROVERS, ApprovalError, was_announced


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
        institution=getattr(args, "institution", None),
    )
    if args.verbose:
        print(f"  Credentials: {config.credential_sources}", file=sys.stderr)
        print(f"  Canvas: {config.base_url} (institution {config.institution})",
              file=sys.stderr)
        print(f"  State: {config.profile_dir.parent}", file=sys.stderr)
    return config


def browser_args(config: "Config", args: argparse.Namespace) -> dict:
    """Everything `open_page` needs, decided in ONE place.

    **The paths come from the config, not from module constants**, because they
    are per-institution: two Canvases sharing one `storage_state.json` means
    logging in to the second silently destroys the first's session. Gathered
    here rather than spelled out at each of the nine call sites, so a tenth
    cannot be written that quietly uses the defaults -- the arity trap that
    shipped 0.1.8.
    """
    return {
        "headless": not args.headed,
        "on_missing_browser": browser_installer(args),
        "profile_dir": config.profile_dir,
        "session_file": config.session_state_file,
    }


def browser_installer(args: argparse.Namespace):
    """A callback that offers to download Chromium, or None if it must not ask.

    Passed to `open_context`, which calls it only when a launch failed because
    the browser was never downloaded. Returning True means "installed, retry".

    Prompting is the point: this is a ~150 MB download onto someone's machine,
    and a tool that starts one unannounced is a tool people stop trusting. With
    no terminal -- cron, CI, a pipe -- it declines and the normal error explains
    the command, rather than silently pulling 150 MB in a context where nobody
    would see it happen.
    """
    if args.no_prompt or not tty_available():
        return None

    def ask() -> bool:
        answer = _read_from_tty(
            f"\n  Chromium is not installed; canvasser cannot drive Canvas "
            f"without it.\n  Download it now ({DOWNLOAD_SIZE}, one time)? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("  Declined. Run `canvasser install-browser` when ready.",
                  file=sys.stderr)
            return False
        install_chromium()
        return True

    return ask


def cmd_install_browser(args: argparse.Namespace) -> int:
    """Download the browser Playwright drives. The one non-Canvas command."""
    install_chromium(with_deps=args.with_deps)
    print("Chromium installed.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    with open_page(**browser_args(config, args)) as page:
        alive = is_logged_in(page, config)
    print("Session: ALIVE (authenticated)" if alive else "Session: DEAD (login required)")
    return 0 if alive else 1


def cmd_login(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(**browser_args(config, args)) as page:
        performed = ensure_logged_in(page, config, approver)
    print("Logged in." if performed else "Already logged in; nothing to do.")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    """Read the course list off the Courses page, as a person would see it."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(**browser_args(config, args)) as page:
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


def chosen_displays(args: argparse.Namespace) -> list[str]:
    """Which settings displays to print. **Naming none means all of them.**

    The same rule `pull` follows -- naming neither `--dates` nor `--info`
    writes both -- and the documentation has cited *this* command as that
    rule's precedent since before `pull` had selectors. It was untrue: the
    default here was `general` alone from the day it was written, so a design
    decision elsewhere rested on behaviour that never existed. Corrected at the
    user's direction 2026-09-09, in favour of what the documentation said.

    Split out of `cmd_settings` so it can be checked without a browser. The
    whole command cannot run offline, which is why nothing caught this.
    """
    chosen = [name for name, _ in DISPLAYS if getattr(args, name, False)]
    return chosen or [name for name, _ in DISPLAYS]


def cmd_settings(args: argparse.Namespace) -> int:
    """Show a course's Details, Sections and Navigation on screen."""
    config = config_from_args(args)
    approver = APPROVERS[args.factor]()

    chosen = chosen_displays(args)

    with open_page(**browser_args(config, args)) as page:
        ensure_logged_in(page, config, approver)
        course = _resolve_course(page, config, args)
        settings = fetch_settings(page, config, course.id)

    for name, render in DISPLAYS:
        if name in chosen:
            print()
            print("\n".join(render(settings)))
    return 0


#: Appended before the extension, so a spreadsheet still opens the file by type.
PARTIAL_SUFFIX = "-partial"

PARTIAL_PROMPT = ("  [S]uffix new, [o]verwrite existing, [r]ename existing, "
                  "or [a]bort [S/o/r/a]? ")


def partial_path(path: Path) -> Path:
    """`dates-580777.csv` -> `dates-580777-partial.csv`."""
    return path.with_name(f"{path.stem}{PARTIAL_SUFFIX}{path.suffix}")


def rename_target(path: Path, stamp: str, exists=Path.exists) -> Path:
    """A free name to move an existing sheet aside to.

    Dated rather than `.bak`, because the useful question later is *which pull
    is this*, and a `.bak` answers it only until the second one. `exists` is a
    parameter so the collision walk can be checked without touching a disk.
    """
    stem = f"{path.stem}-{stamp}"
    candidate = path.with_name(f"{stem}{path.suffix}")
    n = 2
    while exists(candidate):
        candidate = path.with_name(f"{stem}-{n}{path.suffix}")
        n += 1
    return candidate


def resolve_partial_paths(
    paths: list[Path],
    *,
    allow_prompt: bool,
    stamp: str,
    ask=None,
    exists=Path.exists,
    rename=Path.rename,
    out=print,
) -> list[Path]:
    """Decide where a `--limit` pull may write, asking before it destroys data.

    A partial pull aimed at the DEFAULT filename replaces a complete sheet, and
    CSVs are gitignored -- so there is no recovery path at all. Near-miss on
    2026-08-25: a 37-row reference sheet survived only because it happened to
    have been copied seconds earlier.

    Asked BEFORE the walk rather than at write time, where the ~80s of page
    loads have already been spent and the answer arrives too late to matter.

    **Suffixing applies to every path, not only the ones that exist.** The two
    sheets come from one walk and are read as a pair; splitting them across
    `dates-...-partial.csv` and `info-...csv` would make the pair's halves
    disagree about which pull they came from.

    The collaborators are injected so this is checkable offline -- it decides
    what happens to the user's files, which is not a thing to leave to a live
    run to discover.
    """
    at_risk = [p for p in paths if exists(p)]
    if not at_risk:
        # Nothing to lose, so nothing to ask. A prompt that fires when there is
        # no risk is training to answer without reading it.
        return paths

    names = ", ".join(str(p) for p in at_risk)
    out(f"\n  {names} already exists.")
    out("  A partial pull would replace it, and CSVs are not in git.")

    if not allow_prompt:
        # S is the SAFE default, so taking it silently is right where a prompt
        # cannot be answered. This deliberately differs from the credentials
        # rule, which refuses -- a password has no safe default and this does.
        suffixed = [partial_path(p) for p in paths]
        out(f"  Not a terminal, so writing {', '.join(str(p) for p in suffixed)}"
            " instead.")
        out("  (--out names the file explicitly.)\n")
        return suffixed

    answer = (ask or _read_from_tty)(PARTIAL_PROMPT).strip().lower()
    # Empty (a bare Enter, or EOF) takes the default rather than re-asking:
    # the default is the non-destructive one, so falling into it is safe.
    choice = answer[:1] or "s"
    while choice not in ("s", "o", "r", "a"):
        out(f"  '{answer}' is not one of S, o, r, a.")
        answer = (ask or _read_from_tty)(PARTIAL_PROMPT).strip().lower()
        choice = answer[:1] or "s"

    if choice == "a":
        raise ConfigError("Aborted; nothing was read and nothing was written.")
    if choice == "o":
        out("")
        return paths
    if choice == "r":
        for path in at_risk:
            moved = rename_target(path, stamp, exists=exists)
            rename(path, moved)
            out(f"  Renamed {path} -> {moved}")
        out("")
        return paths
    suffixed = [partial_path(p) for p in paths]
    out(f"  Writing {', '.join(str(p) for p in suffixed)}.\n")
    return suffixed


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull one course's assignment dates and settings into CSVs."""
    # Naming neither selector means both, matching `settings`, where no display
    # flag shows all five.
    want_dates = args.dates or not args.info
    want_info = args.info or not args.dates
    # `--out` names ONE file; two artifacts cannot share it. Refused rather
    # than resolved, because both ways of resolving it are worse than an error:
    # inventing a second name puts data somewhere the user did not ask for, and
    # dropping an artifact silently loses a pull they paid ~80s of page loads
    # for.
    if args.out and want_dates and want_info:
        raise ConfigError(
            "--out names a single file, but this pull writes two sheets. "
            "Add --dates or --info to choose one, or drop --out and take the "
            "default names (dates-<course>.csv and info-<course>.csv)."
        )

    config = config_from_args(args)
    approver = APPROVERS[args.factor]()

    print()
    print(session_banner())
    print()
    print(f"  Fetching assignment details from "
          f"{args.course or 'the selected course'}...")

    with open_page(**browser_args(config, args)) as page:
        print("  Connecting...", end="", flush=True)
        ensure_logged_in(page, config, approver)
        # Duo's box, if it appeared, was written to stderr straight through the
        # middle of the line above. Continuing that line would append to
        # whatever the box left on screen, so start a fresh one instead.
        if was_announced():
            print("  Connected & authenticated.")
        else:
            print(" connected & authenticated.")

        course = _resolve_course(page, config, args)
        # One extra page load, for the timezone's human name. Canvas hands out
        # the IANA id on every assignment page but its friendly label only on
        # the settings page, and the sheet carries both -- the label for the
        # person editing it, the id for push to resolve times through.
        course_settings = fetch_settings(page, config, course.id)

        print()
        print(course_heading(course_settings))

        # Decided BEFORE the walk. `--out` is exempt: that path was typed
        # deliberately, and an explicit instruction wins here for the same
        # reason an exact course id beats a name that merely contains it.
        dates_path = Path(args.out or f"dates-{course.id}.csv")
        info_path = Path(args.out or f"info-{course.id}.csv")
        if args.limit and not args.out:
            wanted = ([dates_path] if want_dates else []) + \
                     ([info_path] if want_info else [])
            resolved = resolve_partial_paths(
                wanted,
                allow_prompt=not args.no_prompt and tty_available(),
                stamp=date.today().isoformat(),
            )
            if want_dates:
                dates_path = resolved[0]
            if want_info:
                info_path = resolved[-1]

        print()
        pulled = pull_course(page, config, course.id, limit=args.limit)
    rows, course_tz = pulled.rows, pulled.course_tz

    written: list[Path] = []
    if want_dates:
        out_path = dates_path
        write_sheet(
            rows,
            out_path,
            course_id=course.id,
            timezone=course_settings.timezone_label,
            iana=course_tz or course_settings.course_timezone,
        )
        written.append(out_path)
    if want_info:
        # `--out` is guarded above to a single selector, so if it is set here
        # the infosheet is the only artifact and it owns the name.
        write_info_sheet(pulled.info_rows, info_path, course_id=course.id)
        written.append(info_path)

    # Every tally feeds the BOX. The sentence says where the data went, the box
    # says how much -- so neither repeats the other (user, 2026-08-23).
    print(f"\n  Pulled and stored in {', '.join(str(p) for p in written)}.")
    for line in stat_box([
        ("Assignments", len({r.assignment_id for r in rows})),
        ("Assigned by Section", sum(1 for r in rows if not r.is_base_row)),
        ("Open Dates", sum(1 for r in rows if r.open_date)),
        ("Due Dates", sum(1 for r in rows if r.due_date)),
        ("Close Dates", sum(1 for r in rows if r.close_date)),
        ("No Dates", sum(1 for r in rows if not r.has_any_date)),
    ], glyphs_for()):
        print(f"  {line}")
    return 0


def _why_mismatch(previous, got: str, date_column: str, time_column: str,
                  course_tz: str) -> str:
    """Say *why* a post-write check failed, not merely that it did.

    "MISMATCH" alone sends the user to Canvas to work out what happened. The
    two outcomes mean very different things and are worth distinguishing:

    * Canvas still holds exactly what it held before -- the save was rejected
      or never applied. Something on the page said why; if it was a field
      message `apply_changes` has already raised with Canvas's own wording, so
      reaching here means the rejection was silent.
    * Canvas holds a third value -- it accepted the write and then altered it.
      Its `:59` end-of-day seconds are the known example, already compared away
      at minute precision, so anything surviving to here is new behaviour.
    """
    if previous is None:
        return ("no before-value was recorded, so it cannot be said whether "
                "the save was rejected or the value was altered")

    was = to_minute(
        " ".join(p for p in (getattr(previous, date_column),
                             getattr(previous, time_column)) if p)
    )
    now = to_minute(got.rsplit(" ", 1)[0] if got else "")
    if was == now:
        return (f"Canvas still holds its previous value ({was or '(empty)'}), "
                f"so the save did not take. Canvas usually explains this on the "
                f"page -- check the assignment's edit form for a message under "
                f"the date, most often a date-ordering or term-bounds rule")
    return (f"Canvas accepted a write but stored something else again "
            f"(was {was or '(empty)'}, now {now or '(empty)'}). That is not a "
            f"rule this build knows about; capture the edit page before "
            f"re-running")


def cmd_push_info(args: argparse.Namespace, sheet_path: Path) -> int:
    """Compare an edited infosheet against the live course, and optionally write.

    Preview by default, exactly like the datesheet path: everything above the
    commit block is read-only.

    **`push.WRITABLE_INFO_FIELDS` says what can be written**; it is deliberately
    a short list, grown one widget at a time (user, 2026-08-30), because each
    control is a different shape and must be reconnoitred before it is coded.
    Edits to the remaining editable columns are **reported per row**, never
    silently skipped -- `pull` populates those columns, so someone will edit one,
    and a no-op that looks like a success is the failure this project keeps
    meeting.
    """
    sheet = read_info_sheet(sheet_path)
    print(
        f"  Sheet: {sheet_path}  (infosheet v{sheet.version or '?'}, "
        f"course={sheet.course_id or '?'}, {len(sheet.rows)} row(s))\n"
        f"  Sheet can change: "
        f"{', '.join(sheet.editable_present) or 'nothing -- no editable column'}\n"
        f"  This build can write: {', '.join(WRITABLE_INFO_FIELDS)}",
        file=sys.stderr,
    )

    config = config_from_args(args)
    approver = APPROVERS[args.factor]()
    with open_page(**browser_args(config, args)) as page:
        ensure_logged_in(page, config, approver)
        course_id = args.course or sheet.course_id
        if not course_id:
            raise CourseSelectionError(
                f"{sheet_path} does not record a course and none was given. "
                f"Re-run `canvasser pull` to regenerate it with a header."
            )
        check_info_course(sheet, course_id)

        targets = sorted({row.assignment_id for row in sheet.rows})
        print(
            f"\n  Reading {len(targets)} assignment(s) named by the sheet. "
            f"This is the read pass -- nothing is being changed.\n",
            file=sys.stderr,
        )
        live = read_specific_info(page, config, course_id, targets)
        diff = compare_info(sheet, live.rows, live.graded)

        print()
        print("\n".join(render_info_diff(diff)))

        if not args.commit:
            return 1 if not diff.is_empty else 0
        if diff.is_empty:
            print("\nNothing to commit.")
            return 0

        # Everything above this line is read-only. Everything below writes.
        #
        # The graded warning is restated HERE, not only beside its row. The
        # user chose warn-and-write over refusing, and the objection to that
        # choice was that a warning in a long preview is easy to scroll past --
        # so it is repeated at the moment it stops being hypothetical.
        bad = [(r, msg) for r in diff.changed for msg in r.invalid]
        if bad:
            print(f"\n  {len(bad)} cell(s) hold a value Canvas does not accept "
                  f"and are SKIPPED:", file=sys.stderr)
            for row, msg in bad:
                print(f"      {row.title} #{row.assignment_id}: {msg}",
                      file=sys.stderr)

        grading_rows = [r for r in diff.changed if r.graded and r.changes]
        if grading_rows:
            print(
                f"\n  !! {len(grading_rows)} assignment(s) below already have "
                f"graded submissions. Changing points re-scales every "
                f"student's percentage on them:",
                file=sys.stderr,
            )
            for row in grading_rows:
                print(f"       {row.title}  #{row.assignment_id}", file=sys.stderr)

        print()
        problems: list[str] = []
        for row in diff.changed:
            if not row.changes:
                continue
            # Every field for one assignment goes in ONE form load and one
            # save. Saving per field would mean two page loads and a window
            # where the assignment holds half the edit.
            wanted = {c.field: c.after for c in row.changes}
            try:
                apply_settings(page, config, course_id, row.assignment_id, wanted)
                got = verify_settings(page, config, course_id, row.assignment_id,
                                      tuple(wanted))
            except (WriteRefused, WriteFailed) as exc:
                print(f"  REFUSED {row.title}: {exc}", file=sys.stderr)
                problems.append(f"{row.title} #{row.assignment_id}")
                continue
            print(f"  {row.title}  #{row.assignment_id}")
            for change in row.changes:
                landed = got.get(change.field, "")
                # **The post-write check must compare the same way the diff
                # does**, so it calls the same function: each of these is a
                # field whose text can differ while its meaning does not --
                # `8.340` for `8.34`, a reordered submission-type list, `TRUE`
                # for `true` -- and a check that compares text calls a correct
                # write a failure. `submission_types` did exactly that live on
                # 2026-09-09, because this was a separate chain of `if`s that a
                # new field had to be added to twice.
                ok = same_value(change.field, landed, change.after)
                print(f"      {change.field:<18}{change.before} -> {change.after}"
                      f"   Canvas now: {landed}   {'OK' if ok else 'MISMATCH'}")
                if not ok:
                    # Distinguish "Canvas kept its old value" (the save was
                    # rejected -- go read the form) from "Canvas holds a third
                    # value" (it accepted then altered), as `_why_mismatch`
                    # does for dates. A bare MISMATCH tells the reader nothing
                    # they can act on.
                    # Compared the same way as above, for the same reason: this
                    # decides WHICH failure the user is told about, and text
                    # equality here reported "neither the old nor the new
                    # value" about a value that was exactly the new one.
                    kept = same_value(change.field, landed, change.before)
                    why = ("Canvas still holds its previous value, so the save "
                           "was rejected -- open the form and read its message"
                           if kept else
                           f"Canvas accepted the save but holds {landed!r}, "
                           f"which is neither the old nor the new value")
                    print(f"      {'':<18}why: {why}", file=sys.stderr)
                    problems.append(
                        f"{row.title} #{row.assignment_id} ({change.field})")

    if problems:
        print(f"\n{len(problems)} item(s) did not land, each with a reason above:",
              file=sys.stderr)
        for where in problems:
            print(f"      {where}", file=sys.stderr)
        return 2
    print("\nCommitted.")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Compare an edited datesheet against the live course.

    Preview by default; `--commit` writes. Everything up to the commit block
    is read-only, so the common case -- running this repeatedly while editing a
    sheet -- cannot touch the course.
    """
    sheet_path = Path(args.sheet)
    # Two CSVs with near-identical names now sit side by side, so handing this
    # the wrong one is a matter of time. **An infosheet would otherwise parse
    # as a datesheet with every date column absent** -- which means "leave every
    # date alone", so it would report no changes and exit 0, looking exactly
    # like a clean run against a course that was in sync.
    kind = identify_sheet(sheet_path)
    if args.expect and kind and kind != args.expect:
        # Named, not built by concatenation: `f"{kind}sheet"` against
        # kind="dates" reads "datessheet".
        names = {SHEET_DATES: "datesheet", SHEET_INFO: "infosheet"}
        raise SheetError(
            f"{sheet_path} says it is a {names[kind]} in its first row, but "
            f"--{args.expect} was given. The file's own header is trusted over "
            f"the flag; check which file you meant rather than renaming it."
        )
    # Dispatch on what the FILE says it is, never on the flag. The flag only
    # asserts; the header decides.
    if kind == SHEET_INFO:
        return cmd_push_info(args, sheet_path)
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
    with open_page(**browser_args(config, args)) as page:
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
    # thing that says what they mean. If that is not the course's zone, convert
    # now -- before the diff, so both sides of every comparison, the values
    # typed into the form, and the post-write check all speak course time.
    sheet, realigned, zone_warnings = align_timezone(sheet, course_tz)
    for warning in zone_warnings:
        print(f"\n  UNREADABLE TIMEZONE: {warning}", file=sys.stderr)
    if realigned:
        print(f"\n  {realigned.describe()}", file=sys.stderr)

    diff = compare(sheet, current)
    print()
    print("\n".join(render_diff(diff, sheet)))

    # Rows Canvas will reject on sight. Reported with the diff so they are
    # visible in a dry run, and skipped at commit rather than costing an edit
    # page load each to attempt a save that cannot succeed.
    out_of_order = check_order(sheet, current)
    if out_of_order:
        print(f"\n  {len(out_of_order)} row(s) have dates Canvas will not accept:",
              file=sys.stderr)
        for problem in out_of_order:
            print(f"      {problem}", file=sys.stderr)
        print("      Fix these cells in the sheet; they will be skipped.",
              file=sys.stderr)
    blocked = {problem.split(":")[0] for problem in out_of_order}

    if not args.commit:
        return 1 if (out_of_order or not diff.is_empty) else 0
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
    before = {row.key: row for row in current}
    print(f"\nCommitting to course {course_id}. Times read as {source_tz}.")

    #: Every failure, with the reason it failed, collected rather than counted.
    #: A trailing "3 field(s) did not land" says nothing a person can act on --
    #: the user asked for this directly: *"It would be good to get an explicit
    #: note as to why when things fail."*
    problems: list[tuple[str, str]] = []

    def failed(row_title: str, key: str, why: str) -> None:
        problems.append((f"{row_title}  #{key}", why))

    with open_page(**browser_args(config, args)) as page:
        ensure_logged_in(page, config, approver)
        for row in diff.changed:
            wanted = by_key[row.key]
            if not wanted.is_base_row:
                print(f"  SKIP {row.title}: override rows are not written yet.",
                      file=sys.stderr)
                failed(row.title, row.key[0],
                       "it is an override row, and writing overrides is not "
                       "implemented -- saving the form submits every date card, "
                       "so a wrong move deletes a student's accommodation date")
                continue
            if row.key[0] in blocked:
                print(f"  SKIP {row.title}: its dates are out of order and "
                      f"Canvas would refuse the save.", file=sys.stderr)
                failed(row.title, row.key[0], next(
                    (p.split(": ", 1)[1] for p in out_of_order
                     if p.startswith(row.key[0] + ":")),
                    "its dates are out of order"))
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
                failed(row.title, row.key[0], str(exc))
                continue
            print(f"  {row.title}  #{row.key[0]}")
            for field, date_column, time_column in FIELD_PAIRS:
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
                    if not ok:
                        why = _why_mismatch(before.get(row.key), got,
                                            date_column, time_column, course_tz)
                        # Said here, against the line it explains, rather than
                        # gathered into a footer -- "MISMATCH" on its own sends
                        # the reader to Canvas to work out what happened.
                        print(f"      {'':<11}why: {why}", file=sys.stderr)
                        failed(row.title, row.key[0], f"{field}: {why}")

    if problems:
        print(f"\n{len(problems)} item(s) did not land, each with a reason "
              f"above. Nothing was retried:", file=sys.stderr)
        for where, _ in problems:
            print(f"      {where}", file=sys.stderr)
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
    # Worth having on an installed copy: "which build is this?" is the first
    # question when a user reports behaviour from a machine we cannot see. The
    # licence line is the short notice the GPL asks an interactive program to
    # show; `--version` is where a command-line tool conventionally puts it.
    parser.add_argument(
        "--version",
        action="version",
        version=(
            f"canvasser {__version__}\n"
            "GPL-3.0-or-later. This program comes with ABSOLUTELY NO WARRANTY.\n"
            "Free software: you are welcome to redistribute it under the terms\n"
            "of the GNU General Public License <https://gnu.org/licenses/gpl>."
        ),
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
    creds.add_argument("--username", help="Canvas/SSO username")
    creds.add_argument(
        "--secrets-file",
        metavar="PATH",
        help=f"File of KEY=VALUE lines ({USERNAME_VAR}, {PASSWORD_VAR})",
    )
    creds.add_argument(
        "--institution",
        metavar="SUBDOMAIN",
        help=(
            "Which Canvas to use, by its instructure subdomain (e.g. templeu). "
            f"Its own credentials, session and browser profile live in a "
            f"subdirectory of the state directory. Defaults to "
            f"${INSTITUTION_VAR}, then {DEFAULT_INSTITUTION_SETTING} in the "
            f"secrets file, then the single account already set up."
        ),
    )
    creds.add_argument(
        "--no-prompt",
        action="store_true",
        help="Never prompt; fail instead. Use for unattended runs.",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="check whether the stored session is still valid")
    sub.add_parser("login", help="authenticate, prompting for Duo if needed")

    install = sub.add_parser(
        "install-browser",
        help=f"download the Chromium build canvasser drives ({DOWNLOAD_SIZE})",
        description="Runs `playwright install chromium` in this interpreter's "
        "own environment. `pip install canvasser` brings in the Playwright "
        "library from PyPI, but the browser binaries are a separate download "
        "-- they are platform-specific native builds, not Python. They land in "
        "a shared per-user cache, so this is once per machine, not once per "
        "virtualenv. Every other command offers to run this for you the first "
        "time it needs a browser.",
    )
    install.add_argument(
        "--with-deps",
        action="store_true",
        help="also install the system libraries Chromium needs (Linux; needs root)",
    )

    courses = sub.add_parser("courses", help="list courses (with ids) from the Courses page")
    courses.add_argument("--filter", help="substring match against course name or term")
    courses.add_argument(
        "--teaching", action="store_true", help="only courses where the role is Teacher"
    )
    add_scope_args(courses)

    pull = sub.add_parser(
        "pull",
        help="pull a course's assignment dates and settings into CSV",
        description="Course may be an id or a name fragment. If omitted, resolves "
        f"from --course-file, then ${COURSE_VAR}, then an interactive picker. "
        "Writes dates-<course>.csv and info-<course>.csv; --dates or --info "
        "narrows that to one. Both come from the same page loads, so asking "
        "for one is no faster than asking for both.",
    )
    pull.add_argument(
        "course", nargs="?", help="course id or name fragment (see: canvasser courses)"
    )
    pull.add_argument(
        "--course-file", metavar="PATH", help="read the course id/name from a file"
    )
    add_scope_args(pull)
    # Selectors, following `settings` (--general/--sections/...): naming none
    # means all. Not mutually exclusive -- `--dates --info` is just both, the
    # same reading the scope flags settled on.
    pull.add_argument(
        "--dates", action="store_true",
        help="write only the datesheet (default: both sheets)",
    )
    pull.add_argument(
        "--info", action="store_true",
        help="write only the infosheet: points, grading, submission, publish state",
    )
    pull.add_argument("--out", help="output CSV path; only with a single selector")
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
        help="compare an edited sheet against the live course",
        description="Reads a datesheet or an infosheet, re-reads the course, "
        "and reports what would change. Which kind it is comes from the file's "
        "own first row, not its name. Writes nothing without --commit: this is "
        "the preview half of the round trip, and pull -> push with no edits "
        "must report zero changes.",
    )
    push.add_argument("sheet", help="path to a datesheet or infosheet CSV")
    push.add_argument(
        "--course", help="cross-check: refuse if the sheet names a different course"
    )
    # An ASSERTION, not a router. The sheet declares its own kind in row 1 and
    # that declaration wins; this only says "refuse if it is not what I think".
    # A flag that could override the header would let date rules run against
    # info rows, which is the one outcome worth engineering against here.
    push.add_argument(
        "--dates", dest="expect", action="store_const", const=SHEET_DATES,
        help="cross-check: refuse unless the file says it is a datesheet",
    )
    push.add_argument(
        "--info", dest="expect", action="store_const", const=SHEET_INFO,
        help="cross-check: refuse unless the file says it is an infosheet",
    )
    push.set_defaults(expect=None)
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
        "install-browser": cmd_install_browser,
        "courses": cmd_courses,
        "pull": cmd_pull,
        "settings": cmd_settings,
        "push": cmd_push,
    }
    try:
        return handlers[args.command](args)
    # Every one of these carries a message written for the person running the
    # command -- a sheet to fix, a cell to correct, a login to redo. A
    # traceback would bury it. Anything else still crashes loudly, because an
    # unexpected exception IS a defect and should look like one.
    except (
        BrowserUnavailable,
        ConfigError,
        CourseSelectionError,
        LoginError,
        ApprovalError,
        SheetError,
        DateFormatError,
    ) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
