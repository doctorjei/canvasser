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
import sys
from dataclasses import dataclass

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


def _cell_texts(row) -> list[str]:
    cells = row.locator("td")
    return [(cells.nth(i).inner_text() or "").strip() for i in range(cells.count())]


def list_courses(
    page: Page,
    config: Config,
    active: bool = False,
    archived: bool = False,
    include_non_favorites: bool = False,
    published: bool = False,
    unpublished: bool = False,
) -> list[Course]:
    """Return courses, filtered on three independent axes.

    Each axis has its own default, and only one of them is winnowed:

        enrollment   default BOTH; --active or --archived narrows it
        published    default ANY;  --published or --unpublished narrows it
        favorite     default FAVORITES ONLY; --all opens it up

    Favourite is the deliberate exception -- it is the axis that reflects what
    the user actually cares about right now, and it is a knob they already
    control from Canvas's own UI.

    Everything is derived from the two /courses tables plus the star class. An
    earlier version clicked the global-nav Courses flyout to get the "current"
    set; that turned out to be exactly the set of starred courses, so the click
    (and its async rendering) is no longer needed.
    """
    page.goto(f"{config.base_url}/courses", wait_until="domcontentloaded")
    quiesce(page)

    starred = _starred_ids(page)
    courses: list[Course] = []
    if not archived:
        courses += _scrape_table(page, CURRENT_TABLE, archived=False, starred=starred)
    if not active:
        courses += _scrape_table(page, PAST_TABLE, archived=True, starred=starred)

    if not courses and not archived:
        snapshot = save_debug_snapshot(page, "courses-empty")
        raise RuntimeError(f"No course links found on /courses. Snapshot: {snapshot}")

    if not include_non_favorites:
        courses = [c for c in courses if c.favorite]
    if published:
        courses = [c for c in courses if c.published]
    if unpublished:
        courses = [c for c in courses if not c.published]
    return courses


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
