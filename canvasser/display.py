"""Terminal rendering for course information.

Layout and colour are the user's spec; see `workbook/designs/display-spec.md`,
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
NORMAL_WHITE = "\033[37m"
BRIGHT_BOLD_GREEN = "\033[1;92m"
BRIGHT_BOLD_RED = "\033[1;91m"


def colors_enabled() -> bool:
    """Colour only a real terminal, and honour NO_COLOR."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if colors_enabled() else text


# --- generic text helpers ---------------------------------------------------


def fit(text: str, width: int, centre: bool = False) -> str:
    """Pad to width, truncating with a single-character ellipsis."""
    text = text or ""
    if len(text) > width:
        # rstrip first, or a cut landing on a space leaves "Development …".
        text = text[: width - 1].rstrip() + "…"
    return text.center(width) if centre else text.ljust(width)


def clip(text: str, width: int) -> str:
    """Pad to width, truncating with a three-dot marker.

    Distinct from `fit` deliberately: the user's spec for these tables shows
    `Ally Course Access...`, not the `…` the course table uses.
    """
    text = text or ""
    if len(text) > width:
        text = text[: width - 3].rstrip() + "..."
    return text.ljust(width)


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
