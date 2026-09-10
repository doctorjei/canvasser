"""Which Canvas pages must never be rendered to disk.

A debug snapshot is a full-page screenshot plus the page's HTML, written so a
headless failure can be diagnosed at all. On a gradebook, a roster or a
submissions page that is **student names, grades and submitted work** -- FERPA
data, captured to explain a bug that had nothing to do with it.

The rule this implements (user, 2026-09-09: *"we should try to keep explicit
grade and student information in a separate area if we are caching it at all"*,
and `CONVENTIONS.md` -> Data Handling) is the stronger reading of that: **prefer
not to capture it.** A page that trips anything here is not written out. The
caller still gets a file -- a short text breadcrumb naming the page and what
tripped -- because "no snapshot" must never read as "no failure".

## Two nets, because a URL does not say who is on the page

**Routes** are the cheap first pass: `/courses/<c>/gradebook` is a gradebook
whatever it renders. But a route list alone has a hole on the page this tool
snapshots *most*. An ordinary `/courses/<c>/assignments/<a>/edit` shows
**student names in its "Assign to" override cards**, and `writer.py` reaches
`save_debug_snapshot` from eleven places on that page. Routing cannot see that.

**Content** is therefore the second net, and it is deliberately weighted toward
`ENV` over markup -- the project's standing preference ("Read `ENV`, not
rendered dates"), and `read_overrides` already reads the very same two lists.
An override targeting a list of students carries their ids in `student_ids`;
a section- or group-targeted card names no individual and is not flagged.

## Fail closed

If the probe cannot run -- a destroyed execution context, a page mid-navigation
-- the page counts as sensitive. An unknown state is not an absent one; that is
this project's own documented trap (`can_unpublish: null`), and here the cost of
guessing wrong is student data on disk.

## What is verified against real Canvas, and the one case that is not

Run against live pages on 2026-09-10, not only fixtures:

    a real gradebook                     -> WITHHELD (route + ENV key + grid)
    an ordinary assignment edit page     -> captured
    an edit page with a SECTION override -> captured   <- the deliberate half
    an edit page with an INDIVIDUAL card -> NOT YET VERIFIED

**The last row is the one this file exists for**, and it is still unproven. An
override was created deliberately to test it, and only a *section*-targeted card
could be made: the unpublished course used for that has **no students enrolled**,
so its assignee picker offers one option. Closing it needs a course with a
student in it.

The `ENV` half is the sounder of the two nets -- it reads the same fields
`read_overrides` has always read -- and the DOM selectors beside it remain
belt-and-braces. If an assignment with an individually-targeted card ever comes
to hand, that is the thing to check first.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Page

#: Canvas routes that exist to show people. Matched against the URL **path**,
#: so a query string cannot smuggle one past and a host cannot fake one.
#:
#: A denylist rather than an allowlist, unlike the liveness check in `auth.py`,
#: and for the opposite reason: there the question is "is this the one host that
#: means success", which has a single right answer, while here the safe set is
#: every ordinary Canvas page and the unsafe set is enumerable. The **content
#: probe below is what covers a route nobody thought of**, which is why this
#: list not being exhaustive is survivable.
SENSITIVE_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/courses/\d+/gradebook"), "the gradebook"),
    (re.compile(r"/speed_grader"), "SpeedGrader"),
    (re.compile(r"/courses/\d+/grades"), "a grades page"),
    (re.compile(r"/courses/\d+/users"), "the course roster"),
    (re.compile(r"/accounts/\d+/users"), "an account roster"),
    (re.compile(r"/users/\d+"), "a user profile"),
    (re.compile(r"/submissions(/|$)"), "a submissions page"),
    (re.compile(r"/quizzes/\d+/(history|statistics|moderate|submissions)"),
     "quiz results"),
    (re.compile(r"/assignments/\d+/peer_reviews"), "a peer-review list"),
    (re.compile(r"/courses/\d+/statistics"), "course statistics"),
    (re.compile(r"/courses/\d+/analytics"), "course analytics"),
    (re.compile(r"/courses/\d+/groups"), "group membership"),
    (re.compile(r"/courses/\d+/discussion_topics/\d+"), "a discussion thread"),
    (re.compile(r"/courses/\d+/modules/progressions"), "module progressions"),
    (re.compile(r"/conversations"), "the Canvas inbox"),
)

#: Top-level `ENV` keys that only appear where Canvas is rendering people.
#:
#: **Deliberately short and exact, not a prefix scan.** A scan for anything
#: containing "STUDENT" or "SUBMISSION" would match `ENV.SPEED_GRADER_ENABLED`
#: and the student-annotation feature flags, both of which sit on an ordinary
#: assignment edit page -- and a false positive here withholds the snapshot that
#: would have explained a real failure. Missing a key costs a route or a DOM
#: marker instead; inventing one costs every diagnostic on the write path.
PEOPLE_ENV_KEYS = (
    "GRADEBOOK_OPTIONS",
    "SUBMISSIONS",
    "RECENT_STUDENTS",
    "students",
    "enrollments",
    "SPEED_GRADER_SUBMISSION_UPDATE_URL",
)

#: Containers that only exist on a page listing people. Second net, for pages
#: whose `ENV` carries no key worth naming.
PEOPLE_CONTAINERS = {
    "#gradebook_grid": "a gradebook grid",
    "#gradebook-grid-wrapper": "a gradebook grid",
    "#speed_grader_container": "the SpeedGrader panel",
    "#students_selectmenu": "a student picker",
    "table.roster": "a roster table",
    "#submission_details": "submission details",
    ".student_context_card": "a student context card",
}

#: Reads the page's own state for evidence that a person is named on it.
#:
#: Returns a list of human-readable reasons, empty when nothing tripped. Written
#: to be total -- it never throws for a missing key -- because a probe that
#: raises on an unexpected page would be indistinguishable from a destroyed
#: context, and those two are handled differently.
_PEOPLE_PROBE = """(config) => {
    const found = [];
    const env = (typeof ENV === 'undefined' || !ENV) ? {} : ENV;

    // The "Assign to" cards, read the way `read_overrides` reads them. This is
    // the marker a route list cannot have: the page is an ordinary assignment
    // edit form, and the student names are inside it.
    const subject = env.ASSIGNMENT || env.QUIZ || {};
    const cards = [].concat(
        Array.isArray(subject.assignment_overrides) ? subject.assignment_overrides : [],
        Array.isArray(env.ASSIGNMENT_OVERRIDES) ? env.ASSIGNMENT_OVERRIDES : []);
    for (const card of cards) {
        if (!card) continue;
        // ADHOC is Canvas's own name for "this card targets a list of
        // students". A CourseSection or Group card names no individual, so it
        // is deliberately NOT flagged -- withholding on every overridden
        // assignment would cost diagnostics for no privacy gain.
        const ids = card.student_ids;
        if ((Array.isArray(ids) && ids.length > 0) || card.set_type === 'ADHOC') {
            found.push('an "Assign to" card naming individual students');
            break;
        }
    }

    for (const key of config.envKeys) {
        // Truthiness is correct here: every one of these is an object, an
        // array or a URL string, so falsy genuinely means absent. Contrast
        // `_cell`, which must test `is None` because 0 and false are real
        // values there.
        if (env[key]) found.push('ENV.' + key);
    }

    for (const selector of Object.keys(config.containers)) {
        if (document.querySelector(selector)) found.push(config.containers[selector]);
    }
    return found;
}"""


def route_reasons(url: str) -> tuple[str, ...]:
    """Why this URL is a page about people, or an empty tuple.

    Matched on the parsed path. A bare substring test over the whole URL would
    let `https://example.test/?next=/courses/1/gradebook` read as a gradebook --
    the same mistake the liveness check made until 2026-09-09, arriving in a
    place where it fails the other way (a false positive, not a false negative).
    """
    try:
        path = urlparse(url or "").path
    except ValueError:
        # An unparseable URL is not a safe URL; see the module's fail-closed
        # note. This is cheap to reach -- `page.url` is "about:blank" before a
        # first navigation, which parses fine and matches nothing.
        return ("its address could not be parsed",)
    return tuple(what for pattern, what in SENSITIVE_ROUTES if pattern.search(path))


def content_reasons(page: Page) -> tuple[str, ...]:
    """Why this page's own content is about people, or an empty tuple.

    **Fails closed.** A probe that cannot run leaves us unable to say the page
    is safe, and "we could not tell" must not resolve to "capture it".
    """
    try:
        found = page.evaluate(
            _PEOPLE_PROBE,
            {"envKeys": list(PEOPLE_ENV_KEYS), "containers": dict(PEOPLE_CONTAINERS)},
        )
    except PlaywrightError as exc:
        return (f"its content could not be checked ({type(exc).__name__})",)
    return tuple(found or ())


def why_withhold(page: Page) -> tuple[str, ...]:
    """Every reason this page must not be written to disk; empty means capture.

    Both nets are run and their reasons concatenated rather than short-circuited
    on the first hit: a withheld capture reports *why*, and "the gradebook, a
    gradebook grid" is a more useful line in a bug report than either alone.
    """
    try:
        url = page.url
    except PlaywrightError:
        return ("its address could not be read",)
    return route_reasons(url) + content_reasons(page)
