"""Course-level settings: the Details, Sections, and Navigation tabs.

All three tabs render from a single load of `/courses/<id>/settings`, so this
module visits the page once and reads everything off it.

That page offers four routes to its data, and this module uses each one only
where it is the best available. In descending order of reliability:

1. **`window.ENV`** -- navigation tabs, participation dates, identity,
   timezones. No selectors at all; always prefer this.
2. **`course[...]`-named form controls** -- the editable settings. Semantic,
   stable names. Rails renders each checkbox as a hidden `0` *followed by* the
   real checkbox, so reading `.value` by name yields `0` for every box; read
   `.checked`, and let the real control overwrite the hidden companion.
3. **Section links** -- `a[href*="/sections/"]`, whose href carries the only
   stable section id the page exposes.
4. **Rendered label/value text** -- `Name`, `Course Code`, `Blueprint Course`.
   The weakest route, and unavoidable: on an SIS-fed course these are not
   inputs at all, they are static text, and `ENV` does not carry the course
   code. It is English- and layout-dependent; see `_READ_ONLY_LABELS`.

**Never select on the React-generated names** (`TextInput___4`, `Select___1`,
`Checkbox___0`). They are render-order counters: the same control was observed
as `TextInput___4`, then `___2`, then `___3` across three loads of this page
minutes apart. Every value they hold is in `ENV` under a stable name.

The timezone split is the one that made `pull` read ENV rather than rendered
dates: the course runs in `America/New_York` while the user's Canvas profile
is `Asia/Tokyo`, so rendered timestamps are in the wrong zone for a syllabus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Page

from .assignments import format_in_course_time
from .auth import quiesce
from .browser import save_debug_snapshot
from .config import Config

#: Tabs with this visibility are shown to teachers and admins but not to
#: students. `hidden` alone is therefore NOT "students can see it" -- on the
#: course this was built against, 22 tabs were un-hidden but 3 of those were
#: admins-only, so the student-visible count was 19.
ADMIN_ONLY = "admins"

#: Read-only fields scraped from rendered text, because they are not inputs on
#: an SIS-fed course and ENV does not carry them. Route 4: the fragile one.
#: If Canvas restyles this block or the user's locale changes, these go blank
#: rather than wrong -- `pull` should surface a blank as a visible gap.
_READ_ONLY_LABELS = ("Name", "Course Code", "Blueprint Course")

#: The `(-05:00/-04:00)` pair Canvas appends to its timezone labels. It carries
#: both the standard and daylight offsets and picks neither, so it is noise in a
#: human label -- and stripping it is what leaves the generic "Eastern Time".
_OFFSETS = re.compile(r"\s*\([-+]\d{2}:\d{2}(?:/[-+]\d{2}:\d{2})?\)\s*$")

_USERS = re.compile(r"(\d+)\s+Users?")
#: Section rows include an unrendered Handlebars template whose href is
#: literally `/sections/{{ id }}`. Matching digits only skips it.
_SECTION_HREF = re.compile(r"/sections/(\d+)$")


@dataclass(frozen=True)
class NavTab:
    id: str
    label: str
    css_class: str
    hidden: bool
    visibility: str | None
    immovable: bool
    external: bool

    @property
    def student_visible(self) -> bool:
        return not self.hidden and self.visibility != ADMIN_ONLY


@dataclass(frozen=True)
class Section:
    id: str
    name: str
    users: int | None = None


@dataclass(frozen=True)
class DateRange:
    start: str = ""
    end: str = ""

    def __bool__(self) -> bool:
        return bool(self.start or self.end)

    def describe(self) -> str:
        if not self:
            return "not set"
        return f"{self.start or '(none)'}  ->  {self.end or '(none)'}"


@dataclass(frozen=True)
class CourseSettings:
    """Everything the Settings page knows about a course."""

    course_id: str
    name: str
    course_code: str
    blueprint: str
    published: bool
    course_timezone: str
    user_timezone: str
    account_id: str
    course_dates: DateRange
    term_dates: DateRange
    student_dates: DateRange
    #: `course[...]` settings, keyed without the wrapper: `hide_final_grades`.
    #: Values are display-ready: "yes"/"no" for checkboxes, the selected
    #: option's *text* for dropdowns.
    options: dict[str, str] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    nav_tabs: list[NavTab] = field(default_factory=list)

    @property
    def student_visible_tabs(self) -> list[NavTab]:
        return [t for t in self.nav_tabs if t.student_visible]

    @property
    def staff_only_tabs(self) -> list[NavTab]:
        """Enabled, but students never see them."""
        return [t for t in self.nav_tabs if not t.hidden and not t.student_visible]

    @property
    def hidden_tabs(self) -> list[NavTab]:
        return [t for t in self.nav_tabs if t.hidden]

    @property
    def total_users(self) -> int:
        return sum(s.users or 0 for s in self.sections)

    @property
    def timezone_label(self) -> str:
        """Canvas's human name for the course zone, offsets stripped.

        `Eastern Time (US & Canada) (-05:00/-04:00)` -> `Eastern Time (US &
        Canada)`.

        **Deliberately the generic form, never EST or EDT.** Eastern Time is
        the year-round name; EST is standard, EDT is daylight. A semester sheet
        spans both -- a spring course is EST in January and EDT in April -- so
        stamping either variant on the file would be wrong for half its rows.
        Canvas's own label is already generic; only the trailing offset pair
        needs removing, and that pair is exactly what makes it look specific.
        """
        return _OFFSETS.sub("", self.options.get("time_zone", "") or "").strip()


_READ_STATE = """() => {
    const env = window.ENV || {};

    // Route 2: course[...] controls. The hidden `0` companion Rails emits for
    // each checkbox comes *first*, so letting real controls overwrite it is
    // enough -- but guard so a later hidden cannot clobber a real value.
    const options = {};
    for (const e of document.querySelectorAll(
            '#course_form input, #course_form select, #course_form textarea')) {
        const n = e.name || '';
        if (!n.startsWith('course[') || !n.endsWith(']')) continue;
        const key = n.slice(7, -1);
        if (e.type === 'hidden') {
            if (!(key in options)) options[key] = e.value;
        } else if (e.type === 'checkbox') {
            options[key] = e.checked ? 'yes' : 'no';
        } else if (e.tagName === 'SELECT') {
            options[key] = (e.selectedOptions[0]?.text || e.value || '').trim();
        } else {
            options[key] = (e.value || '').trim();
        }
    }

    // Route 4: label/value pairs out of the rendered details block.
    const form = document.querySelector('#course_form');
    const lines = (form ? form.innerText : '')
        .split('\\n').map(s => s.trim()).filter(Boolean);
    const readOnly = {};
    for (let i = 0; i < lines.length - 1; i++) {
        const m = lines[i].match(/^(.+?):$/);
        if (m && !/:$/.test(lines[i + 1])) readOnly[m[1]] = lines[i + 1];
    }

    // Route 3: sections, keyed by the id in the href.
    const sections = [];
    const seen = new Set();
    for (const a of document.querySelectorAll('a[href*="/sections/"]')) {
        const m = (a.getAttribute('href') || '').match(/\\/sections\\/(\\d+)$/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        const row = a.closest('li') || a.parentElement;
        sections.push({id: m[1], name: (a.textContent || '').trim(),
                       rowText: (row?.innerText || '').trim()});
    }

    return {
        course_id: env.COURSE_ID, published: env.COURSE_PUBLISHED,
        context: env.current_context, account_id: env.ACCOUNT_ID,
        course_tz: env.CONTEXT_TIMEZONE, user_tz: env.TIMEZONE,
        course_dates: env.COURSE_DATES, term_dates: env.DEFAULT_TERM_DATES,
        student_dates: env.STUDENTS_ENROLLMENT_DATES,
        tabs: env.COURSE_SETTINGS_NAVIGATION_TABS,
        options, readOnly, sections,
    };
}"""


def _range(raw: dict | None, course_tz: str) -> DateRange:
    raw = raw or {}
    return DateRange(
        start=format_in_course_time(raw.get("start_at") or "", course_tz),
        end=format_in_course_time(raw.get("end_at") or "", course_tz),
    )


def fetch_settings(page: Page, config: Config, course_id: str) -> CourseSettings:
    """Load a course's Settings page and read all three tabs off it."""
    page.goto(
        f"{config.base_url}/courses/{course_id}/settings",
        wait_until="domcontentloaded",
    )
    quiesce(page)

    state = page.evaluate(_READ_STATE)

    # An empty ENV means we did not land where we think we did -- New Quizzes
    # taught this once already, where a canonical URL redirected to an app
    # shell with no state. Fail loudly with a picture rather than hand back a
    # settings object full of blanks.
    if not state.get("course_id") or state.get("tabs") is None:
        snapshot = save_debug_snapshot(page, f"settings-env-{course_id}")
        raise RuntimeError(
            f"No course settings state on {page.url} -- ENV.COURSE_ID or "
            f"ENV.COURSE_SETTINGS_NAVIGATION_TABS was missing. Snapshot: {snapshot}"
        )

    course_tz = state.get("course_tz") or ""
    read_only = state.get("readOnly") or {}
    context = state.get("context") or {}

    sections = []
    for raw in state.get("sections") or []:
        found = _USERS.search(raw.get("rowText") or "")
        sections.append(
            Section(
                id=raw["id"],
                name=raw.get("name") or "",
                users=int(found.group(1)) if found else None,
            )
        )

    return CourseSettings(
        course_id=str(state["course_id"]),
        # ENV is authoritative for the name; the scraped label is the fallback.
        name=context.get("name") or read_only.get("Name", ""),
        course_code=read_only.get("Course Code", ""),
        blueprint=read_only.get("Blueprint Course", ""),
        published=bool(state.get("published")),
        course_timezone=course_tz,
        user_timezone=state.get("user_tz") or "",
        account_id=str(state.get("account_id") or ""),
        course_dates=_range(state.get("course_dates"), course_tz),
        term_dates=_range(state.get("term_dates"), course_tz),
        student_dates=_range(state.get("student_dates"), course_tz),
        options=state.get("options") or {},
        sections=sections,
        nav_tabs=[
            NavTab(
                id=str(t.get("id", "")),
                label=t.get("label") or "",
                css_class=t.get("css_class") or "",
                hidden=bool(t.get("hidden")),
                visibility=t.get("visibility"),
                immovable=bool(t.get("immovable")),
                external=bool(t.get("external")),
            )
            for t in state["tabs"]
        ],
    )
