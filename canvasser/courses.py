"""Reading the course list.

The account here carries ~200 courses spanning a decade, so listing them is not
a formality: without an id and a term, picking the right one is guesswork. We
therefore read the *link* in each row rather than the row's visible text -- the
href carries the course id, which is the only stable handle Canvas gives us.

(The first version of this scraped cell text and produced rows like "Click to
add ... to the courses menu" -- the favorite-star's accessible label -- with no
id anywhere. Text is what a page shows; hrefs are what it means.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from playwright.sync_api import Page

from .auth import quiesce
from .browser import save_debug_snapshot
from .config import Config

COURSE_HREF = re.compile(r"/courses/(\d+)(?:$|[/?#])")

#: /courses is titled "All Courses" and renders two tables: current enrollments
#: and past ones. Scraping every `table tbody tr` merged them into one 194-row
#: list, burying 48 live courses under a decade of archived sections. The table
#: ids are the separator Canvas gives us, so scope to them explicitly.
CURRENT_TABLE = "#my_courses_table"
PAST_TABLE = "#past_enrollments_table"

#: What "current" means here, learned the hard way. The /courses page files 46
#: courses under "Current Enrollments": 19 Development Term shells, 16 Ongoing
#: Term spaces, stale enrollments, and only 8 actually-running courses. The
#: global-nav Courses flyout shows a far better list -- and that list turned out
#: to be exactly the *starred* courses, which is why this module derives scope
#: from the star class instead of clicking the tray open.
#:
#: Past enrollments carry the favorite class but NOT "course-list-favoritable":
#: a concluded course keeps its star and can no longer be toggled. Archived
#: courses can therefore genuinely be favorites.
#: Starred courses carry this class on their star span, which also holds the
#: course id. Read favorites as a *set of ids* gathered in one pass rather than
#: per row: two earlier per-row attempts miscounted (7/97 and then 9/68 against
#: a truth of 9/68), and the id is unambiguous where row scoping was not.
#:
#: Note past enrollments have the favorite class but NOT "course-list-favoritable"
#: -- a concluded course keeps its star but can no longer be toggled. So favorite
#: status is real for archived courses too; 68 of them are starred here.
FAVORITE_STAR = "span.course-list-favorite-course"

#: Each tray entry carries "SIS ID: ... | Term: X", so the tray gives us term
#: information directly rather than by cross-referencing.
TERM_IN_TRAY = re.compile(r"Term:\s*(.+?)\s*$")

@dataclass(frozen=True)
class Course:
    id: str
    name: str
    term: str
    role: str
    published: bool
    archived: bool = False
    #: Starred in Canvas. Read from the favorite-star's accessible label, which
    #: says "Click to *remove* ... from the courses menu" for a starred course
    #: and "Click to *add* ..." for an unstarred one.
    favorite: bool = False

    def matches(self, needle: str) -> bool:
        needle = needle.lower()
        return needle in self.name.lower() or needle in self.term.lower()


@dataclass(frozen=True)
class Scope:
    """The three independent scope axes, as the user set them.

    Each axis is a **pair of opposed flags, combined as a union**: naming both
    sides asks for both, it is not a contradiction. `--active --archived` means
    every enrollment, exactly as omitting both does. An earlier version made
    each pair argparse-mutually-exclusive so the combination errored out; the
    user's call (2026-08-21) is that a union is the obvious reading and
    refusing it is unhelpful.

    So per axis: neither flag = that axis's default, one flag = narrow to it,
    both flags = open it up.

    The favourite axis is the one whose *default* is winnowed -- it is the axis
    the user curates in Canvas itself -- which is why `--unmarked` (only
    unstarred) and `--favorite` (only starred) both exist even though
    `--favorite` matches the default. `--all` is shorthand for naming both.

    Kept as a value object rather than loose kwargs because the name search
    *relaxes* scope one axis at a time and has to report which axis it widened.
    """

    active: bool = False
    archived: bool = False
    published: bool = False
    unpublished: bool = False
    favorite: bool = False
    unmarked: bool = False

    # Each axis reports whether it is already as wide as it goes, so the search
    # can skip a relaxation that would re-search an identical set.
    #
    # For the two symmetric axes, "both set" and "neither set" are the same
    # request -- everything -- so equality is the test, not falsity.
    @property
    def publish_is_widest(self) -> bool:
        return self.published == self.unpublished

    @property
    def enrollment_is_widest(self) -> bool:
        return self.active == self.archived

    @property
    def favorites_is_widest(self) -> bool:
        # Not symmetric: neither flag means favourites only, so only naming
        # both sides genuinely opens this axis.
        return self.favorite and self.unmarked

    @property
    def is_widest(self) -> bool:
        return (
            self.publish_is_widest
            and self.enrollment_is_widest
            and self.favorites_is_widest
        )

    def relax_publish(self) -> Scope:
        return replace(self, published=False, unpublished=False)

    def relax_enrollment(self) -> Scope:
        return replace(self, active=False, archived=False)

    def relax_favorites(self) -> Scope:
        return replace(self, favorite=True, unmarked=True)

    def describe(self) -> str:
        """Human-readable scope, for the expansion notice."""
        parts = []
        if self.enrollment_is_widest:
            parts.append("all enrollments")
        else:
            parts.append("current enrollments" if self.active else "past enrollments")

        if not self.publish_is_widest:
            parts.append("published only" if self.published else "unpublished only")

        if self.favorites_is_widest:
            parts.append("starred and unstarred")
        elif self.unmarked:
            parts.append("unstarred only")
        else:
            parts.append("favorites only")
        return ", ".join(parts)


def apply_scope(courses: list[Course], scope: Scope) -> list[Course]:
    """Filter a fetched course list down to a scope. Pure -- no page access.

    Each axis has its own default, and only one of them is winnowed:

        enrollment   default BOTH; --active / --archived narrows, both = union
        published    default ANY;  --published / --unpublished ditto
        favorite     default FAVORITES ONLY; --unmarked flips it,
                     --favorite --unmarked (or --all) opens it

    Favourite is the deliberate exception -- it is the axis that reflects what
    the user actually cares about right now, and it is a knob they already
    control from Canvas's own UI.

    Separated from fetching so the name search can widen scope repeatedly
    without re-visiting /courses; both tables come off a single page load.
    """
    # Exactly one side of a pair means "narrow to that side". Naming both (or
    # neither) is a union, so there is nothing to filter.
    if not scope.enrollment_is_widest:
        courses = [c for c in courses if c.archived == scope.archived]
    if not scope.publish_is_widest:
        courses = [c for c in courses if c.published == scope.published]
    if not scope.favorites_is_widest:
        # Default (neither flag) and an explicit --favorite are the same ask.
        courses = [c for c in courses if c.favorite != scope.unmarked]
    return courses


def fetch_courses(page: Page, config: Config) -> list[Course]:
    """Every course on /courses, both tables, unfiltered.

    One page load. Callers filter with `apply_scope`.
    """
    page.goto(f"{config.base_url}/courses", wait_until="domcontentloaded")
    quiesce(page)

    starred = _starred_ids(page)
    courses = _scrape_table(page, CURRENT_TABLE, archived=False, starred=starred)
    courses += _scrape_table(page, PAST_TABLE, archived=True, starred=starred)

    if not courses:
        snapshot = save_debug_snapshot(page, "courses-empty")
        raise RuntimeError(f"No course links found on /courses. Snapshot: {snapshot}")
    return courses


def _cell_texts(row) -> list[str]:
    cells = row.locator("td")
    return [(cells.nth(i).inner_text() or "").strip() for i in range(cells.count())]


def _starred_ids(page: Page) -> set[str]:
    """Course ids of every starred course on the page, both tables."""
    return set(
        page.evaluate(
            f"""() => [...document.querySelectorAll('{FAVORITE_STAR}')]
                    .map(s => s.getAttribute('data-course-id'))
                    .filter(Boolean)"""
        )
    )


def _scrape_table(
    page: Page, table_selector: str, archived: bool, starred: set[str]
) -> list[Course]:
    rows = page.locator(f"{table_selector} tbody tr")
    courses: list[Course] = []

    for i in range(rows.count()):
        row = rows.nth(i)
        link = row.locator("a[href*='/courses/']").first
        if link.count() == 0:
            continue
        href = link.get_attribute("href") or ""
        match = COURSE_HREF.search(href)
        if not match:
            continue

        texts = _cell_texts(row)
        # Column order (name, nickname, term, role, published) is Canvas's, but
        # padding defensively beats an IndexError on a course that renders oddly.
        texts += [""] * (5 - len(texts))
        name = (link.inner_text() or "").strip() or texts[0]

        courses.append(
            Course(
                id=match.group(1),
                name=" ".join(name.split()),
                term=texts[2],
                role=texts[3],
                published=texts[4].strip().lower().startswith("yes"),
                archived=archived,
                favorite=match.group(1) in starred,
            )
        )

    return courses


def fetch_one(page: Page, config: Config, course_id: str) -> Course | None:
    """Load a single course directly by id, without listing anything.

    When the caller already has the number there is nothing to search for, and
    scraping ~200 rows off /courses to find a row we could have navigated to is
    a page load and a lot of parsing spent on a question already answered.

    Returns None if the id does not resolve, so the caller can fall back to the
    full list rather than treating a typo as a hard failure. Term and role are
    not on this page; they are left blank rather than guessed, because nothing
    downstream of an explicit id needs them.
    """
    page.goto(
        f"{config.base_url}/courses/{course_id}", wait_until="domcontentloaded"
    )
    found = page.evaluate(
        """() => {
            const c = window.ENV?.current_context;
            if (!c || c.type !== 'Course' || !c.id) return null;
            return {id: String(c.id), name: c.name || '',
                    published: window.ENV?.COURSE_PUBLISHED !== false};
        }"""
    )
    if not found or found["id"] != str(course_id):
        return None
    return Course(
        id=found["id"], name=found["name"], term="", role="",
        published=bool(found["published"]),
    )
