"""Terminal rendering for course information.

Layout and colour are the user's spec; see `designs/display-spec.md` in the
project's own notes (not shipped with the package),
which is authoritative. Widths here are load-bearing -- every table is sized to
fit an 80-column terminal without wrapping.

Two rules run through all of it:

* **Show the student's permission, not Canvas's restriction.** Canvas stores
  these inverted (`hide_final_grades`, `restrict_student_future_view`), and a
  screen that reports the raw field under a "Student Permissions" heading says
  the opposite of what it means. Every such field is flipped exactly once, in
  `_permits`.
* **Values are clipped only where a column must hold its shape.** Label/value
  lists never truncate; grid columns do, with a three-dot marker.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from unicodedata import east_asian_width
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- colour -----------------------------------------------------------------

RESET = "\033[0m"
BOLD_WHITE = "\033[1;97m"
BOLD_WHITE_U = "\033[1;4;97m"
GREEN_ITALIC_U = "\033[3;4;92m"
BOLD_GREEN = "\033[1;92m"
BOLD_YELLOW = "\033[1;93m"
BOLD_BLUE = "\033[1;94m"
GREY = "\033[0;37m"
#: One colour, one spelling. These were two constants for the same 37, which is
#: also the progress bar's colour -- a third name would have made it three.
NORMAL_WHITE = GREY
BRIGHT_BOLD_GREEN = "\033[1;92m"
BRIGHT_BOLD_RED = "\033[1;91m"


def colors_enabled() -> bool:
    """Colour only a real terminal, and honour NO_COLOR."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if colors_enabled() else text


# --- generic text helpers ---------------------------------------------------


def display_width(text: str) -> int:
    """Columns a string occupies, not characters it contains.

    Every layout here is built to an exact column budget, and `len` is not that
    measurement: one CJK character or emoji occupies two cells. `✨` is the case
    that proved it -- a Duo box row measuring 32 by `len` draws 34 columns wide.
    No Canvas title has yet contained such a character, which is precisely why
    the mistake would have shipped silently.
    """
    return sum(2 if east_asian_width(char) in "WF" else 1 for char in text or "")


def _truncate(text: str, width: int) -> str:
    """Cut to a COLUMN budget, never splitting a wide character in half."""
    out, used = [], 0
    for char in text:
        size = 2 if east_asian_width(char) in "WF" else 1
        if used + size > width:
            break
        out.append(char)
        used += size
    return "".join(out)


def _pad(text: str, width: int, centre: bool) -> str:
    room = max(0, width - display_width(text))
    if not centre:
        return text + " " * room
    return " " * (room // 2) + text + " " * (room - room // 2)


#: The truncation marker, everywhere. Never `…`: it is tofu on some terminals
#: (droste's finding, and the user's stated preference 2026-08-23). The course
#: table used to differ from the rest of the display; it no longer does.
ELLIPSIS = "..."


def shorten(text: str, width: int) -> str:
    """Truncate to a column budget with the three-dot marker. No padding.

    The one place truncation is decided, so the bar, the tables and the boxes
    cannot drift apart on where a cut lands or what marks it.
    """
    text = text or ""
    if display_width(text) <= width:
        return text
    # rstrip first, or a cut landing on a space leaves "Development ...".
    return _truncate(text, width - len(ELLIPSIS)).rstrip() + ELLIPSIS


def fit(text: str, width: int, centre: bool = False) -> str:
    """Pad to a column width, truncating with the three-dot marker."""
    return _pad(shorten(text, width), width, centre)


def clip(text: str, width: int) -> str:
    """Pad to a column width, truncating with the three-dot marker.

    Kept separate from `fit` only because it never centres; the two markers
    agree now that the course table no longer uses a one-character ellipsis.
    """
    return _pad(shorten(text, width), width, False)


TRUTHY = {"yes", "true", "1", "on"}


def _truth(value: object) -> bool:
    """Canvas hands these back as 'yes'/'no', 'true'/'false', 'No', and bools."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUTHY


def _yn(value: object) -> str:
    return "Yes" if _truth(value) else "No"


def _permits(restriction: object) -> str:
    """A student permission read off a Canvas *restriction* field.

    `hide_final_grades = no` means students DO see final grades; likewise
    `restrict_student_future_view = true` means they do NOT get in early. The
    inversion happens here and nowhere else.
    """
    return "No" if _truth(restriction) else "Yes"


# --- value formatting -------------------------------------------------------

_OFFSETS = re.compile(r"\s*\([-+]\d{2}:\d{2}(?:/[-+]\d{2}:\d{2})?\)\s*$")
_REGION = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_CLOCK = re.compile(r"(\d{1,2}:\d{2})\s*([ap])m", re.I)
_LOCALE_DEFAULT = re.compile(r"defaults? to .*?\(([^)]*)\)", re.I)


def format_timezone(canvas_label: str, iana: str) -> str:
    """`Eastern Time (US & Canada) (-05:00/-04:00)` + `America/New_York`
    -> `Eastern Daylight Time, US/Canada (America / New York)`.

    Daylight vs Standard is resolved against *today* in the course's zone,
    which is why the IANA name is needed and the offset pair in Canvas's label
    is not enough -- that label carries both offsets and picks neither.
    """
    label = _OFFSETS.sub("", canvas_label or "").strip()
    region = ""
    matched = _REGION.match(label)
    if matched:
        label, region = matched.group(1).strip(), matched.group(2).strip()
    region = region.replace(" & ", "/")

    if label.endswith(" Time") and iana:
        try:
            now = datetime.now(ZoneInfo(iana))
            daylight = (now.dst() or timedelta()) != timedelta()
            label = label[: -len("Time")] + ("Daylight Time" if daylight else
                                             "Standard Time")
        except (ZoneInfoNotFoundError, ValueError):
            pass  # Keep Canvas's wording rather than invent a season.

    pretty = (iana or "").replace("_", " ").replace("/", " / ")
    head = f"{label}, {region}" if region else label
    if not head:
        return pretty
    return f"{head} ({pretty})" if pretty else head


def format_locale(value: str) -> str:
    """`Not set (user-configurable, defaults to English (United States))`
    -> `Default (United States)`."""
    found = _LOCALE_DEFAULT.search(value or "")
    if found:
        return f"Default ({found.group(1)})"
    return value or ""


def format_due_time(value: str) -> str:
    """`Account default (11:59pm)` -> `11:59 PM`."""
    found = _CLOCK.search(value or "")
    if found:
        return f"{found.group(1)} {found.group(2).upper()}M"
    return value or ""


def format_wiki_roles(value: str) -> str:
    """`Only Teachers` -> `Teachers Only`."""
    value = (value or "").strip()
    if value.lower().startswith("only "):
        return f"{value[5:].strip()} Only"
    return value


def format_range(span) -> str:
    """`start -> end`, `?` for a missing side, `[Unset / Unknown]` for neither."""
    if not span.start and not span.end:
        return "[Unset / Unknown]"
    return f"{span.start or '?'} -> {span.end or '?'}"


def format_nav_id(raw: str) -> str:
    """`context_external_tool_469884` -> `Ext.469884`; numeric ids unchanged."""
    if raw.startswith("context_external_tool_"):
        return f"Ext.{raw[len('context_external_tool_'):]}"
    return raw


def format_visibility(value: str | None) -> str:
    return (value or "None").capitalize()


# --- general ----------------------------------------------------------------


def render_general(s) -> list[str]:
    out = [_paint("General Information", GREEN_ITALIC_U), ""]

    title = (
        _paint(s.name, BOLD_GREEN)
        + " ("
        + _paint(s.course_code, BOLD_YELLOW)
        + f"): #{s.course_id} "
        + _paint(f"[Format: {s.options.get('course_format') or 'Not Set'}]", GREY)
    )
    out += [title, ""]

    for label, value in (
        ("Timezone", format_timezone(s.options.get("time_zone", ""),
                                     s.course_timezone)),
        ("Locale", format_locale(s.options.get("locale", ""))),
        ("Course Dates", format_range(s.course_dates)),
        ("Term Dates", format_range(s.term_dates)),
        ("Student Dates", format_range(s.student_dates)),
        ("Default Due Time", format_due_time(s.options.get("default_due_time", ""))),
    ):
        out.append(_paint(fit(label, 18), BOLD_WHITE) + value)

    # Two rows of three. `*` reads "this mode is on".
    flags = [
        ("Published", s.published),
        ("Restricted Enrollment", s.options.get("restrict_enrollments_to_course_dates")),
        ("Pacing", s.options.get("enable_course_paces")),
        ("Blueprint", s.blueprint),
        ("Template", s.options.get("template")),
        ("Mastery Paths", s.options.get("conditional_release")),
    ]
    out.append("")
    rows = (flags[:3], flags[3:])
    # Each column sizes to its own longest label, so the two rows line up
    # without a uniform width padding "Pacing" out to match "Mastery Paths".
    widths = [max(len(rows[0][i][0]), len(rows[1][i][0])) + 4 for i in range(3)]
    for row in rows:
        line = ""
        for (label, value), width in zip(row, widths):
            on = _truth(value)
            mark = _paint("*" if on else "-", BOLD_GREEN if on else BOLD_BLUE)
            line += f"[{mark}] " + label.ljust(width - 4)
            line += " " * 4
        out.append(line.rstrip())
    return out


# --- features ---------------------------------------------------------------

#: Left labels pad to 25 with a 2-space gap, putting every left value at column
#: 27 and the right block at 34. Right labels size to their own band.
LEFT_LABEL = 25
LEFT_BLOCK = 34


def _two_group_band(left_title, left_rows, right_title, right_rows) -> list[str]:
    """Two labelled groups side by side.

    Each title is underlined individually -- the spec calls for no underlined
    gap between them, so the padding must sit outside the escape codes.
    """
    right_label = max((len(label) for label, _ in right_rows), default=0) + 2
    header = (
        _paint(left_title, BOLD_WHITE_U)
        + " " * (LEFT_BLOCK - len(left_title))
        + _paint(right_title, BOLD_WHITE_U)
    )
    lines = [header]
    for index in range(max(len(left_rows), len(right_rows))):
        line = ""
        if index < len(left_rows):
            label, value = left_rows[index]
            line = label.ljust(LEFT_LABEL) + "  " + value
        line = line.ljust(LEFT_BLOCK)
        if index < len(right_rows):
            label, value = right_rows[index]
            line += label.ljust(right_label) + value
        lines.append(line.rstrip())
    return lines


def render_features(s) -> list[str]:
    opt = s.options.get
    out = [_paint("Features & Interface", BOLD_WHITE_U), ""]

    out += _two_group_band(
        "Announcements",
        [
            ("Announcements Locked", _yn(opt("lock_all_announcements"))),
            ("Homepage Announcements On", _yn(opt("show_announcements_on_home_page"))),
            ("Homepage Announcement Max", opt("home_page_announcement_limit", "")),
        ],
        "Grading",
        [
            ("Course Grading Standard Enabled",
             _yn(opt("course_grading_standard_enabled"))),
            ("Filter Speed Grader by Group",
             _yn(opt("filter_speed_grader_by_student_group"))),
            ("Grading Standard ID", opt("grading_standard_id", "")),
        ],
    )
    out.append("")
    out += _two_group_band(
        "Student Permissions",
        [
            ("Edit Discussions", _yn(opt("allow_student_discussion_editing"))),
            ("Add Discussion Topics", _yn(opt("allow_student_discussion_topics"))),
            ("Attach Files in Forums", _yn(opt("allow_student_forum_attachments"))),
            ("Organize Groups", _yn(opt("allow_student_organized_groups"))),
            # Restrictions, shown as the permission they leave behind.
            ("View Before Course Start", _permits(opt("restrict_student_future_view"))),
            ("View After Course End", _permits(opt("restrict_student_past_view"))),
            ("Custom Course Visibility", _yn(opt("custom_course_visibility"))),
        ],
        "Visible / Visibility Level",
        [
            ("Distribution Graphs", _permits(opt("hide_distribution_graphs"))),
            ("Final Grades", _permits(opt("hide_final_grades"))),
            ("Sections (For Course Users)",
             _permits(opt("hide_sections_on_course_users_page"))),
            ("Files Access (Default)", opt("files_visibility_option", "")),
            ("Content Access (Default)", opt("course_visibility", "")),
            ("Syllabus Access (Default)", opt("syllabus_visibility_option", "")),
            ("Default Wiki Edit Access",
             format_wiki_roles(opt("default_wiki_editing_roles", ""))),
        ],
    )
    return out


# --- sections ---------------------------------------------------------------

#: 7 + 2 + 22 + 2 + 5, twice over with a 3-space gap, is exactly 79 columns --
#: the full 80-column budget. Column starts are 0, 9, 33, 41, 50, 74.
#:
#: The name column is 22 on BOTH sides. The user's ruler labelled the left one
#: "23 characters", but that token is itself 22 wide and every example row puts
#: the right block at 41; the rows are the unambiguous source.
SEC_ID, SEC_NAME, SEC_SIZE, SEC_GAP = 7, 22, 5, 3


def render_sections(s) -> list[str]:
    out = [_paint("Section Information", BOLD_WHITE_U), "",
           _paint("Sections", GREEN_ITALIC_U), ""]

    head = (
        fit("Sec. ID", SEC_ID) + "  " + fit("Section Name", SEC_NAME) + "  "
        + fit("Size", SEC_SIZE)
    )
    # The whole header line is underlined here, unlike the nav tables where the
    # gap between column groups stays plain. The user's spec distinguishes them.
    out.append(_paint(head + " " * SEC_GAP + head, BOLD_WHITE_U))

    rows = s.sections
    half = (len(rows) + 1) // 2  # column-major: first half down the left
    for index in range(half):
        line = _section_cell(rows[index])
        if index + half < len(rows):
            line += " " * SEC_GAP + _section_cell(rows[index + half])
        out.append(line.rstrip())
    return out


def _section_cell(sec) -> str:
    size = "" if sec.users is None else str(sec.users)
    return (
        fit(sec.id, SEC_ID) + "  " + clip(sec.name, SEC_NAME) + "  "
        + fit(size, SEC_SIZE)
    )


# --- navigation -------------------------------------------------------------

NAV_ELEMENT, NAV_ID, NAV_VIS, NAV_EXT, NAV_MOV = 24, 13, 13, 11, 7


def render_nav(s, enabled: bool) -> list[str]:
    """Enabled or disabled navigation elements.

    "Enabled" is everything not hidden -- including the `admins`-only entries,
    which students never see but which are genuinely enabled. Those sort last,
    so the list reads student-visible first.
    """
    if enabled:
        tabs = s.student_visible_tabs + s.staff_only_tabs
        title = "Navigation Settings (Enabled Elements)"
    else:
        tabs = s.hidden_tabs
        title = "Navigation Settings (Disabled Elements)"

    out = [_paint(title, BOLD_WHITE_U), ""]

    columns = [("Element", NAV_ELEMENT), ("ID", NAV_ID),
               ("Visibility", NAV_VIS), ("External", NAV_EXT)]
    if enabled:
        columns.append(("Movable", NAV_MOV))
    # Each header is underlined on its own; the gaps between stay plain.
    out.append("".join(_paint(label, BOLD_WHITE_U) + " " * (width - len(label))
                       for label, width in columns).rstrip())

    for tab in tabs:
        line = (
            clip(tab.label, NAV_ELEMENT - 3) + "   "
            + fit(format_nav_id(tab.id), NAV_ID)
            + fit(format_visibility(tab.visibility), NAV_VIS)
            + fit(_yn(tab.external), NAV_EXT)
        )
        if enabled:
            line += _yn(not tab.immovable)
        out.append(line.rstrip())
    return out


# --- course table (unchanged behaviour, moved here) -------------------------

#: Column widths, to the user's spec. Every gap is two spaces; the total is 79,
#: so the table fits an 80-column terminal without wrapping.
COLUMNS = (("ID Num", 6), ("Fav", 3), ("Pub", 3), ("Course Name", 32),
           ("Term", 13), ("Role(s)", 12))
GAP = "  "
BOLD_UNDERLINE_WHITE = BOLD_WHITE_U


def render_diff(diff, sheet) -> list[str]:
    """What a push would change. Loud about anything destructive.

    Clearing a date gets the red treatment that publish state gets in the
    course table, for the same reason: removing a due date makes an assignment
    open-ended for every student in the course, and it is the edit most likely
    to be an accident (an emptied spreadsheet cell looks like nothing at all).
    """
    out = [_paint(fit("Push preview -- nothing has been written", 79), BOLD_WHITE_U), ""]

    if diff.is_empty:
        out.append(_paint("  No changes. ", BOLD_GREEN)
                   + f"{diff.compared} sheet row(s) match Canvas exactly.")
        return out

    for row in diff.changed:
        # The id is always shown, never only the title: the title is decoration
        # and may be absent or renamed, but the id is what a write would target.
        where = row.assign_to or "Everyone"
        if row.key[1]:
            where += f", override {row.key[1]}"
        out.append(_paint(f"  {row.title}", BOLD_WHITE)
                   + _paint(f"   #{row.key[0]}  [{where}]", GREY))
        for change in row.changes:
            before = change.before or "(unset)"
            after = change.after or "(unset)"
            tint = BRIGHT_BOLD_RED if change.is_clear else BOLD_GREEN
            out.append(
                f"      {fit(change.field, 11)}{_paint(fit(before, 22), GREY)}"
                f" -> {_paint(after, tint)}"
                + (_paint("   CLEARS THIS DATE", BRIGHT_BOLD_RED)
                   if change.is_clear else "")
            )
        out.append("")

    if diff.missing:
        out.append(_paint(f"  {len(diff.missing)} row(s) in the sheet no longer "
                          f"exist on Canvas:", BRIGHT_BOLD_RED))
        for row in diff.missing[:10]:
            out.append(f"      {row.assignment_id:<10} {row.title}")
        out.append("")

    if diff.untouched:
        # Not a warning. Deleting rows is a supported way to narrow a push, so
        # this says what will happen to them, not that something is wrong.
        out.append(_paint(f"  {len(diff.untouched)} assignment(s) not in the "
                          f"sheet -- left alone:", GREY))
        for row in diff.untouched[:8]:
            out.append(_paint(f"      {row.assignment_id:<10} {row.title}", GREY))
        if len(diff.untouched) > 8:
            out.append(_paint(f"      ... and {len(diff.untouched) - 8} more", GREY))
        out.append("")

    clears = sum(1 for r in diff.changed for c in r.changes if c.is_clear)
    out.append(f"  {len(diff.changed)} row(s), {diff.field_count} field(s) would change"
               + (_paint(f"; {clears} would CLEAR a date", BRIGHT_BOLD_RED)
                  if clears else ""))
    return out


def render_creates(plans, refusals) -> list[str]:
    """What an infosheet push would CREATE, and which rows it will not.

    Rendered separately from the diff rather than folded into it, because a
    create is not a change to something: it has no before value, no live row to
    compare against, and its failure mode is the opposite one. A diff that
    reported "(unset) -> Homework 1" would be describing an edit to an
    assignment that does not exist.

    **The loud item here is the count itself.** Creation is the one operation
    this tool cannot undo -- deleting is not built -- so the number of new
    assignments is stated plainly before anything happens.
    """
    if not plans and not refusals:
        return []

    out = [_paint(fit("New assignments -- nothing has been created", 79),
                  BOLD_WHITE_U), ""]

    for plan in plans:
        out.append(_paint(f"  {plan.title}", BOLD_WHITE)
                   + _paint("   (new)", GREY))
        for column in sorted(plan.values):
            if column == "title":
                continue
            out.append(f"      {fit(column, 18)}"
                       f"{_paint(fit('(new)', 14), GREY)}"
                       f" -> {_paint(plan.values[column], BOLD_GREEN)}")
        for note in plan.notes:
            # Not a refusal: a field left to Canvas's own default, said out
            # loud because a default is only harmless when it is expected.
            out.append(_paint(f"      note: {note}", GREY))
        for change in plan.unsupported:
            out.append(
                f"      {fit(change.field, 18)}"
                f"{_paint(fit('(new)', 14), GREY)}"
                f" -> {_paint(change.after, GREY)}"
                + _paint("   NOT WRITABLE YET -- ignored", BRIGHT_BOLD_RED)
            )
        out.append("")

    for refusal in refusals:
        out.append(_paint(f"  {refusal.title}", BOLD_WHITE)
                   + _paint(f"   line {refusal.line}", GREY)
                   + _paint("   WILL NOT BE CREATED", BRIGHT_BOLD_RED))
        for reason in refusal.reasons:
            out.append(_paint(f"      {reason}", BRIGHT_BOLD_RED))
        out.append("")

    if plans:
        out.append(_paint(
            f"  {len(plans)} assignment(s) would be created. ", BOLD_WHITE)
            + _paint("This cannot be undone from here -- ", BRIGHT_BOLD_RED)
            + _paint("deleting is not built.", BRIGHT_BOLD_RED))
        out.append("")
    return out


def render_info_diff(diff) -> list[str]:
    """What an infosheet push would change.

    Two things get loud treatment, for the same reason publish state does in
    the course table -- they are the ones with consequences a re-run cannot
    undo:

    * **a points change on an assignment that already has grades**, which
      re-scales every student's percentage; and
    * **an edit to a column this build cannot write**, which would otherwise be
      a silent no-op the user reads as success.
    """
    out = [_paint(fit("Info push preview -- nothing has been written", 79),
                  BOLD_WHITE_U), ""]

    if (diff.is_empty and not diff.has_unsupported and not diff.has_invalid
            and not diff.has_gated):
        if not diff.compared:
            # A sheet of nothing but NEW rows compares no rows at all, and
            # "0 sheet row(s) match Canvas exactly" reads like a failed match
            # rather than an empty question. The creates are rendered
            # separately, above this.
            out.append(_paint("  No existing rows to compare. ", BOLD_GREEN)
                       + "Every row in this sheet is a new assignment.")
            return out
        out.append(_paint("  No changes. ", BOLD_GREEN)
                   + f"{diff.compared} sheet row(s) match Canvas exactly.")
        return out

    for row in diff.changed:
        out.append(_paint(f"  {row.title}", BOLD_WHITE)
                   + _paint(f"   #{row.assignment_id}", GREY))
        for change in row.changes:
            out.append(
                f"      {fit(change.field, 18)}"
                f"{_paint(fit(change.before or '(unset)', 14), GREY)}"
                f" -> {_paint(change.after, BOLD_GREEN)}"
            )
            if row.graded:
                out.append(_paint(
                    "      ^ this assignment already has graded submissions; "
                    "changing", BRIGHT_BOLD_RED))
                out.append(_paint(
                    "        its points re-scales every student's percentage",
                    BRIGHT_BOLD_RED))
        for message in row.invalid:
            # A value this build understands the column for but cannot use --
            # typically the label a person sees ("Points") where Canvas wants
            # the option value (`points`). Named here so the dry run says which
            # cell to fix, rather than costing a page load each to discover.
            out.append(_paint(f"      {message}", BRIGHT_BOLD_RED))
        for change in row.gated:
            # Writable, but not without being asked. Reported for exactly the
            # same reason as `unsupported`: an edit that vanishes silently
            # reads as "no change", and the reader has no way to tell the
            # difference between "nothing to do" and "declined to do it".
            out.append(
                f"      {fit(change.field, 18)}"
                f"{_paint(fit(change.before or '(unset)', 14), GREY)}"
                f" -> {_paint(change.after, BOLD_GREEN)}"
                + _paint("   needs --rename", BRIGHT_BOLD_RED)
            )
        for change in row.unsupported:
            # Reported, never attempted. A column pull writes but push cannot
            # act on is a trap: the edit looks applied because nothing
            # complained.
            out.append(
                f"      {fit(change.field, 18)}"
                f"{_paint(fit(change.before or '(unset)', 14), GREY)}"
                f" -> {_paint(change.after, GREY)}"
                + _paint("   NOT WRITABLE YET -- ignored", BRIGHT_BOLD_RED)
            )
        out.append("")

    if diff.missing:
        out.append(_paint(f"  {len(diff.missing)} row(s) in the sheet no longer "
                          f"exist on Canvas:", BRIGHT_BOLD_RED))
        for row in diff.missing[:10]:
            out.append(f"      {row.assignment_id:<10} {row.title}")
        out.append("")

    if diff.untouched:
        out.append(_paint(f"  {len(diff.untouched)} assignment(s) not in the "
                          f"sheet -- left alone:", GREY))
        for row in diff.untouched[:8]:
            out.append(_paint(f"      {row.assignment_id:<10} {row.title}", GREY))
        if len(diff.untouched) > 8:
            out.append(_paint(f"      ... and {len(diff.untouched) - 8} more", GREY))
        out.append("")

    unsupported = sum(len(r.unsupported) for r in diff.changed)
    gated = sum(len(r.gated) for r in diff.changed)
    invalid = sum(len(r.invalid) for r in diff.changed)
    graded = sum(1 for r in diff.changed if r.graded and r.changes)
    out.append(
        f"  {len(diff.changed)} row(s), {diff.field_count} field(s) would change"
        + (_paint(f"; {unsupported} edit(s) NOT writable yet", BRIGHT_BOLD_RED)
           if unsupported else "")
        + (_paint(f"; {gated} rename(s) need --rename", BRIGHT_BOLD_RED)
           if gated else "")
        + (_paint(f"; {invalid} cell(s) SKIPPED as invalid", BRIGHT_BOLD_RED)
           if invalid else "")
        + (_paint(f"; {graded} already graded", BRIGHT_BOLD_RED) if graded else "")
    )
    return out


def print_course_table(courses: list) -> None:
    header = GAP.join(fit(label, width) for label, width in COLUMNS)
    colour = colors_enabled()
    print(f"{BOLD_UNDERLINE_WHITE if colour else ''}{header}{RESET if colour else ''}")

    for course in courses:
        mark = "✓" if course.published else "✗"
        state = BRIGHT_BOLD_GREEN if course.published else BRIGHT_BOLD_RED
        pub = fit(mark, 3, centre=True)
        if colour:
            # Return to the row colour after the mark, or the rest of the line
            # would fall back to the terminal default.
            pub = f"{state}{pub}{RESET}{NORMAL_WHITE}"

        row = GAP.join(
            (
                fit(course.id, 6),
                fit("★" if course.favorite else "", 3, centre=True),
                pub,
                fit(course.name, 32),
                fit(course.term, 13),
                fit(course.role, 12),
            )
        )
        print(f"{NORMAL_WHITE if colour else ''}{row}{RESET if colour else ''}")

    plural = "course" if len(courses) == 1 else "courses"
    print(f"\n[{len(courses)} {plural}]")
