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


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    term: str
    role: str
    published: bool
    archived: bool = False

    def matches(self, needle: str) -> bool:
        needle = needle.lower()
        return needle in self.name.lower() or needle in self.term.lower()


def _cell_texts(row) -> list[str]:
    cells = row.locator("td")
    return [(cells.nth(i).inner_text() or "").strip() for i in range(cells.count())]


def list_courses(page: Page, config: Config, include_archived: bool = False) -> list[Course]:
    """Return courses from /courses, current ones by default."""
    page.goto(f"{config.base_url}/courses", wait_until="domcontentloaded")
    quiesce(page)

    courses = _scrape_table(page, CURRENT_TABLE, archived=False)
    if include_archived:
        courses += _scrape_table(page, PAST_TABLE, archived=True)

    if not courses:
        snapshot = save_debug_snapshot(page, "courses-empty")
        raise RuntimeError(f"No course links found on /courses. Snapshot: {snapshot}")

    return courses


def _scrape_table(page: Page, table_selector: str, archived: bool) -> list[Course]:
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
            )
        )

    return courses
