"""Reading assignment due dates out of Canvas.

Four facts from live recon shape everything here:

1. **There is no bulk "Edit Assignment Dates" grid on this instance.** It was
   checked for on a real Assignments page and is absent, so dates are read (and
   later written) one assignment at a time.

2. **The index renders dates without a year** -- "Due Dec 1 at 1:59pm" -- and the
   course-time tooltip has no year either. Neither can be round-tripped safely.

3. **The page's own `ENV` object carries the exact instant**, ISO8601 with an
   offset, plus both timezone names. We read that instead of scraping rendered
   text. This is still the page a person loaded -- not a REST API call -- and it
   removes every inference that could shift a deadline by a day.

4. **An assignment has no timezone of its own.** `due_at` is one absolute
   instant; the user and the course each render it differently. Observed live:
   `2026-12-01T13:59:59+09:00` (user, Asia/Tokyo) is the same moment as
   `2026-11-30 23:59:59 -0500` (course, America/New_York). The CSV uses course
   time with an explicit offset, per the user's choice, because "11:59pm" is
   what a deadline means to whoever set it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import Page

from .auth import quiesce
from .browser import save_debug_snapshot
from .config import Config
from .datesheet import AssignmentRow

ASSIGNMENT_HREF = re.compile(r"/courses/\d+/(?:assignments|quizzes)/(\d+)")

#: Pulled straight from the page's JS state. Quizzes expose ENV.QUIZ, plain
#: assignments ENV.ASSIGNMENT; both carry `due_at`. Overrides live in
#: ENV.ASSIGNMENT_OVERRIDES, which is a far better count of extra date rows than
#: counting table rows and hoping the markup did not change.
ENV_PROBE = """() => {
    if (typeof ENV === 'undefined') return null;
    const subject = ENV.ASSIGNMENT || ENV.QUIZ || {};
    return {
        assignment_id: ENV.ASSIGNMENT_ID != null ? String(ENV.ASSIGNMENT_ID) : null,
        due_at: subject.due_at ?? null,
        user_tz: ENV.TIMEZONE ?? null,
        course_tz: ENV.CONTEXT_TIMEZONE ?? null,
        overrides: (Array.isArray(ENV.ASSIGNMENT_OVERRIDES)
            ? ENV.ASSIGNMENT_OVERRIDES : []).map(o => ({
                id: o && o.id != null ? String(o.id) : '',
                title: (o && o.title) || '',
                due_at: (o && o.due_at) || null,
                // Canvas can carry "unassigned" targets alongside real ones;
                // they are not dates and must not become editable rows.
                unassign: !!(o && o.unassign_item),
            })),
    };
}"""


@dataclass(frozen=True)
class Assignment:
    id: str
    title: str
    url: str


def _wait_for_list_to_settle(
    page: Page, quiet_polls: int = 3, interval_ms: int = 500, timeout_ms: int = 20_000
) -> int:
    """Wait until the assignment list stops growing.

    The index renders rows asynchronously, so reading it too early silently
    returns a *subset*. Two consecutive pulls of the same course produced 17 and
    then 19 assignments -- and a pull that quietly drops assignments is worse
    than one that fails, because the CSV still looks complete.

    `quiesce` (network idle) is not sufficient on its own here; we watch the
    thing we actually care about -- the link count -- and require it to hold
    steady across several polls before trusting it.
    """
    selector = "a[href*='/assignments/'], a[href*='/quizzes/']"
    waited = 0
    last = -1
    stable = 0

    while waited < timeout_ms:
        count = page.locator(selector).count()
        if count == last and count > 0:
            stable += 1
            if stable >= quiet_polls:
                return count
        else:
            stable = 0
        last = count
        page.wait_for_timeout(interval_ms)
        waited += interval_ms

    return last


def list_assignments(page: Page, config: Config, course_id: str) -> list[Assignment]:
    """Enumerate assignments from the course's Assignments index.

    Only ids and titles are taken from this page -- its dates lack years. Order
    is preserved as Canvas presents it, which is the order the instructor sees.
    """
    page.goto(
        f"{config.base_url}/courses/{course_id}/assignments",
        wait_until="domcontentloaded",
    )
    quiesce(page)
    _wait_for_list_to_settle(page)

    links = page.locator("a[href*='/assignments/'], a[href*='/quizzes/']")
    found: dict[str, Assignment] = {}

    for i in range(links.count()):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        match = ASSIGNMENT_HREF.search(href)
        if not match:
            continue
        title = " ".join((link.inner_text() or "").split())
        if not title or match.group(1) in found:
            continue
        found[match.group(1)] = Assignment(
            id=match.group(1),
            title=title,
            url=href if href.startswith("http") else f"{config.base_url}{href}",
        )

    if not found:
        snapshot = save_debug_snapshot(page, f"assignments-empty-{course_id}")
        raise RuntimeError(
            f"No assignments found for course {course_id}. Snapshot: {snapshot}"
        )

    return list(found.values())


def format_in_course_time(iso_due: str, course_tz: str | None) -> str:
    """Render an ISO instant in the course's timezone with an explicit offset.

    Seconds are emitted only when non-zero. Canvas commonly stores end-of-day
    deadlines as :59 seconds (11:59:59pm), and silently truncating those would
    make a pull->push round trip with no edits look like a real change -- the
    exact thing the no-op round-trip test exists to catch.
    """
    if not iso_due:
        return ""

    moment = datetime.fromisoformat(iso_due)
    if course_tz:
        try:
            moment = moment.astimezone(ZoneInfo(course_tz))
        except (ZoneInfoNotFoundError, ValueError):
            pass  # Keep the original offset rather than inventing one.

    pattern = "%Y-%m-%d %H:%M:%S %z" if moment.second else "%Y-%m-%d %H:%M %z"
    return moment.strftime(pattern)


def read_assignment_dates(page: Page, assignment: Assignment) -> dict:
    """Load one assignment page and read its dates out of the page's JS state."""
    page.goto(assignment.url, wait_until="domcontentloaded")
    quiesce(page)

    env = page.evaluate(ENV_PROBE)
    if env is None:
        snapshot = save_debug_snapshot(page, f"assignment-no-env-{assignment.id}")
        raise RuntimeError(
            f"No ENV state on {page.url} -- Canvas may have changed how the page "
            f"is rendered. Snapshot: {snapshot}"
        )
    return env


def pull_course(
    page: Page,
    config: Config,
    course_id: str,
    limit: int | None = None,
) -> list[AssignmentRow]:
    """Walk a course's assignments and produce one CSV row per assignment."""
    assignments = list_assignments(page, config, course_id)
    if limit:
        assignments = assignments[:limit]

    rows: list[AssignmentRow] = []
    for index, assignment in enumerate(assignments, start=1):
        env = read_assignment_dates(page, assignment)
        course_tz = env.get("course_tz")
        overrides = [o for o in (env.get("overrides") or []) if not o.get("unassign")]

        # Trust ENV.ASSIGNMENT_ID over the id parsed from the link. A quiz-backed
        # assignment is linked as /quizzes/<quiz_id>, and that number is NOT the
        # assignment id -- observed live: /assignments/7261693 redirects to
        # /quizzes/1640715. Keying the sheet on whichever number happened to be in
        # the href would mean push later resolving a row to the wrong object.
        assignment_id = env.get("assignment_id") or assignment.id

        for override in overrides:
            rows.append(
                AssignmentRow(
                    assignment_id=assignment_id,
                    override_id=override["id"],
                    title=assignment.title,
                    assign_to=override["title"] or f"override {override['id']}",
                    due_at=format_in_course_time(override.get("due_at") or "", course_tz),
                )
            )

        # With overrides present Canvas relabels the base row "Everyone else",
        # because it no longer applies to everyone. Mirroring that wording keeps
        # the sheet honest about what the row covers.
        base_due = format_in_course_time(env.get("due_at") or "", course_tz)
        rows.append(
            AssignmentRow(
                assignment_id=assignment_id,
                override_id="",
                title=assignment.title,
                assign_to="Everyone else" if overrides else "Everyone",
                due_at=base_due,
            )
        )

        summary = base_due or "(no base due date)"
        if overrides:
            summary += f"  +{len(overrides)} section row(s)"
        print(
            f"  [{index}/{len(assignments)}] {assignment.title[:48]:<48} {summary}",
            flush=True,
        )

    return rows
