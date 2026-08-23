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

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .auth import quiesce
from .browser import save_debug_snapshot
from .config import Config
from .datesheet import AssignmentRow
from .progress import Progress

ASSIGNMENT_HREF = re.compile(r"/courses/\d+/(?:assignments|quizzes)/(\d+)")

#: Pulled straight from the page's JS state. Quizzes expose ENV.QUIZ, plain
#: assignments ENV.ASSIGNMENT; both carry `due_at`. Overrides live in
#: ENV.ASSIGNMENT_OVERRIDES, which is a far better count of extra date rows than
#: counting table rows and hoping the markup did not change.
ENV_PROBE = """() => {
    if (typeof ENV === 'undefined') return null;
    const subject = ENV.ASSIGNMENT || ENV.QUIZ || {};
    return {
        // Whether a date-bearing subject was on the page at all. A page with
        // neither is not "an assignment with no due date" -- it is the wrong
        // page, and reporting it as an empty date loses real data silently.
        has_subject: !!(ENV.ASSIGNMENT || ENV.QUIZ),
        assignment_id: ENV.ASSIGNMENT_ID != null ? String(ENV.ASSIGNMENT_ID) : null,
        // Canvas's three date boxes. The UI labels unlock_at "Available from"
        // and lock_at "Until"; the API names are what the sheet records.
        title: subject.name || subject.title || null,
        due_at: subject.due_at ?? null,
        unlock_at: subject.unlock_at ?? null,
        lock_at: subject.lock_at ?? null,
        user_tz: ENV.TIMEZONE ?? null,
        course_tz: ENV.CONTEXT_TIMEZONE ?? null,
        overrides: (Array.isArray(ENV.ASSIGNMENT_OVERRIDES)
            ? ENV.ASSIGNMENT_OVERRIDES : []).map(o => ({
                id: o && o.id != null ? String(o.id) : '',
                title: (o && o.title) || '',
                due_at: (o && o.due_at) || null,
                unlock_at: (o && o.unlock_at) || null,
                lock_at: (o && o.lock_at) || null,
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


def split_course_time(iso: str, course_tz: str | None) -> tuple[str, str]:
    """Render an ISO instant as (date, time) in the course's timezone.

    The offset is deliberately dropped: schema v3 declares the zone once in the
    sheet's header instead of repeating it on every value, so the columns stay
    editable in a spreadsheet.

    **Seconds are dropped too, always.** Canvas's edit form has a minute-only
    time box, so seconds cannot be written back -- proved live, where a request
    for `22:59:00` against a stored `22:59:59` left the `:59` in place. A sheet
    that displayed seconds would be inviting an edit it cannot deliver. Canvas
    keeps its own `:59` end-of-day convention regardless; that is its business,
    not the sheet's.
    """
    if not iso:
        return "", ""

    moment = datetime.fromisoformat(iso)
    if course_tz:
        try:
            moment = moment.astimezone(ZoneInfo(course_tz))
        except (ZoneInfoNotFoundError, ValueError):
            pass  # Keep the original offset rather than inventing one.

    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


def read_assignment_dates(page: Page, assignment: Assignment) -> dict:
    """Load one assignment page and read its dates out of the page's JS state.

    Which page actually carries the dates depends on the assignment's type, and
    getting this wrong is *silent*, so the rule is: follow the index href, and
    if it lands somewhere with no date-bearing state, fall back to the edit
    page.

        classic quiz        index href -> /quizzes/<qid>          ENV.QUIZ ✓
        New Quizzes         index href -> /edit?quiz_lti=true     ENV.QUIZ ✓
        classic assignment  index href -> /assignments/<id>       NEITHER ✗
                            fall back  -> /assignments/<id>/edit  ENV.ASSIGNMENT ✓

    The show page for a classic assignment renders its dates as markup and
    carries no ENV state at all, so a probe there returns "no due date" for an
    assignment that has one. That produced a CSV where 34 of 37 rows claimed to
    be undated; at least two of them were not.

    So we ask for the **edit view first**, which is the one page every type
    exposes state on -- a classic quiz's `/assignments/<id>/edit` redirects to
    `/quizzes/<qid>/edit`, still carrying `ENV.QUIZ`. That keeps this to one
    page load per assignment; probing the href and then falling back doubled
    every course's traffic, and not hammering the LMS is a standing rule.

    An href that already carries a query is used verbatim: New Quizzes hands
    out `/edit?quiz_lti=true`, and rebuilding that URL lands on a `/build/` app
    with an empty ENV. If the edit view somehow yields nothing, the original
    href is tried before giving up.
    """
    primary, fallback = _date_urls(assignment.url)
    env = _probe_at(page, primary, assignment.id)
    if not env.get("has_subject") and fallback:
        env = _probe_at(page, fallback, assignment.id)

    if not env.get("has_subject"):
        snapshot = save_debug_snapshot(page, f"assignment-no-subject-{assignment.id}")
        raise RuntimeError(
            f"Neither ENV.ASSIGNMENT nor ENV.QUIZ on {page.url} for assignment "
            f"{assignment.id} ({assignment.title!r}). Refusing to record it as "
            f"undated -- an absent subject is not an absent due date. "
            f"Snapshot: {snapshot}"
        )
    return env


def _date_urls(href: str) -> tuple[str, str | None]:
    """(page to try first, page to fall back to) for reading dates.

    A query string means Canvas handed us a purpose-built URL (New Quizzes);
    leave it alone. An href already pointing at /edit needs no help either.
    """
    path = href.split("?")[0].rstrip("/")
    if "?" in href or path.endswith("/edit"):
        return href, None
    return f"{path}/edit", href


#: How long to wait for ENV to appear. Short on purpose -- it is inline script
#: in the document, so it is there almost immediately or not at all.
ENV_TIMEOUT_MS = 8_000

#: True once the page's JS state carries something with dates on it.
_ENV_READY = "() => !!(window.ENV && (window.ENV.ASSIGNMENT || window.ENV.QUIZ))"


def _probe_at(page: Page, url: str, assignment_id: str) -> dict:
    """Load a page and read ENV off it as soon as ENV exists.

    **Deliberately not `quiesce`.** Waiting for `networkidle` here cost ~14
    seconds per assignment: a Canvas edit page never goes idle -- LTI iframes,
    the rich content editor and its polling keep a connection open -- so every
    single load burned the full 15s timeout before doing anything. `ENV` is
    inline `<script>` in the document, so it is available at DOMContentLoaded;
    waiting for network silence was waiting for an event that never comes, to
    read data that had already arrived.

    A timeout is tolerated rather than raised: the show page genuinely has no
    subject, and the caller's fallback to the edit view is how that is handled.
    """
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_function(_ENV_READY, timeout=ENV_TIMEOUT_MS)
    except PlaywrightTimeout:
        pass
    env = page.evaluate(ENV_PROBE)
    if env is None:
        snapshot = save_debug_snapshot(page, f"assignment-no-env-{assignment_id}")
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
) -> tuple[list[AssignmentRow], str]:
    """Walk a course's assignments and produce CSV rows.

    Returns the rows *and* the course timezone, because schema v3 records the
    zone once in the sheet header rather than on every value -- so the caller
    that writes the file needs it.
    """
    assignments = list_assignments(page, config, course_id)
    if limit:
        assignments = assignments[:limit]

    rows: list[AssignmentRow] = []
    course_tz = ""
    # Advance BEFORE the fetch: each page costs ~2s, and a bar that only moved
    # afterwards would sit frozen for exactly the interval it exists to cover.
    bar = Progress(len(assignments))
    for index, assignment in enumerate(assignments, start=1):
        bar.advance(index, assignment.title)
        env = read_assignment_dates(page, assignment)
        course_tz = env.get("course_tz") or course_tz
        overrides = [o for o in (env.get("overrides") or []) if not o.get("unassign")]

        # Trust ENV.ASSIGNMENT_ID over the id parsed from the link. A quiz-backed
        # assignment is linked as /quizzes/<quiz_id>, and that number is NOT the
        # assignment id -- observed live: /assignments/7261693 redirects to
        # /quizzes/1640715. Keying the sheet on whichever number happened to be in
        # the href would mean push later resolving a row to the wrong object.
        assignment_id = env.get("assignment_id") or assignment.id

        def dates(source: dict) -> dict:
            """The three date pairs, in the course's timezone."""
            due = split_course_time(source.get("due_at") or "", course_tz)
            open_ = split_course_time(source.get("unlock_at") or "", course_tz)
            close = split_course_time(source.get("lock_at") or "", course_tz)
            return {
                "due_date": due[0], "due_time": due[1],
                "open_date": open_[0], "open_time": open_[1],
                "close_date": close[0], "close_time": close[1],
            }

        for override in overrides:
            rows.append(
                AssignmentRow(
                    assignment_id=assignment_id,
                    override_id=override["id"],
                    title=assignment.title,
                    assign_to=override["title"] or f"override {override['id']}",
                    **dates(override),
                )
            )

        # With overrides present Canvas relabels the base row "Everyone else",
        # because it no longer applies to everyone. Mirroring that wording keeps
        # the sheet honest about what the row covers.
        base = AssignmentRow(
            assignment_id=assignment_id,
            override_id="",
            title=assignment.title,
            assign_to="Everyone else" if overrides else "Everyone",
            **dates(env),
        )
        rows.append(base)

    bar.finish()
    return rows, course_tz


def read_specific(
    page: Page, config: Config, course_id: str, assignment_ids: list[str]
) -> tuple[list[AssignmentRow], str]:
    """Read dates for named assignments only, going straight to each one.

    **No index load, no walk of the course.** When the caller already has ids --
    which is exactly the case for `push`, whose sheet names them -- enumerating
    all 37 assignments to answer a question about one of them is ~90 seconds
    spent to learn nothing. One id is one page load.

    The trade-off, stated so it is not a surprise: without the index there is no
    list of assignments the sheet *omits*, so a push cannot report "left alone".
    That listing was reassurance, not information the write needs.

    Titles come from the page's own `ENV` rather than the index, which is also
    more accurate -- it is the live name, not one cached in a link.
    """
    rows: list[AssignmentRow] = []
    course_tz = ""

    bar = Progress(len(assignment_ids))
    for index, assignment_id in enumerate(assignment_ids, start=1):
        bar.advance(index, f"#{assignment_id}")
        target = Assignment(
            id=assignment_id,
            title=f"assignment {assignment_id}",
            url=f"{config.base_url}/courses/{course_id}/assignments/{assignment_id}",
        )
        env = read_assignment_dates(page, target)
        course_tz = env.get("course_tz") or course_tz
        title = env.get("title") or target.title
        real_id = env.get("assignment_id") or assignment_id

        overrides = [o for o in (env.get("overrides") or []) if not o.get("unassign")]
        for override in overrides:
            rows.append(
                AssignmentRow(
                    assignment_id=real_id,
                    override_id=override["id"],
                    title=title,
                    assign_to=override["title"] or f"override {override['id']}",
                    **_date_fields(override, course_tz),
                )
            )
        base = AssignmentRow(
            assignment_id=real_id,
            override_id="",
            title=title,
            assign_to="Everyone else" if overrides else "Everyone",
            **_date_fields(env, course_tz),
        )
        rows.append(base)
        # Relabel now the title is known. `push` is handed ids, not titles, so
        # the row opens as `#7261693` and settles into its real name.
        bar.advance(index, title)

    bar.finish()
    return rows, course_tz


def _date_fields(source: dict, course_tz: str | None) -> dict:
    """The three date pairs from an ENV probe, in the course's timezone."""
    due = split_course_time(source.get("due_at") or "", course_tz)
    open_ = split_course_time(source.get("unlock_at") or "", course_tz)
    close = split_course_time(source.get("lock_at") or "", course_tz)
    return {
        "due_date": due[0], "due_time": due[1],
        "open_date": open_[0], "open_time": open_[1],
        "close_date": close[0], "close_time": close[1],
    }
