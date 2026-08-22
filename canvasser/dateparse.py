"""Forgiving date and time parsing for hand-edited sheets.

A spreadsheet will reformat a date column the moment it decides the column is
dates: `2026-02-01` comes back as `2/1/2026`, and `23:59` as `11:59:00 PM`. The
sheet is meant to be edited by a person in exactly that spreadsheet, so the
reader accepts the common renderings and normalises them back to the canonical
form (`YYYY-MM-DD` and `HH:MM[:SS]`) before anything compares or writes them.

Normalising on *read* is what makes the no-op round trip survive a spreadsheet
open-and-save: the diff compares canonical strings, so a purely cosmetic
reformat produces no changes.

## Day/month order

A numeric date like `3/4/2026` is 4 March in the US and 3 April in most of
Europe -- a three-week difference in a deadline, decided by nothing visible in
the cell. This resolves it in the only honest order:

1. **If only one reading is a real date, use it.** `13/4/2026` cannot be a US
   date (there is no month 13), so it is 13 April. No warning: nothing was
   guessed.
2. **If both readings are real, warn and take the US one.** `3/4/2026` becomes
   4 March, and the user is told which cells were assumed so they can check the
   three that mattered rather than re-reading the sheet.
3. **If the two readings agree, say nothing.** `5/5/2026` needs no warning.

`YYYY-MM-DD` and the named-month forms (`1 Feb 2026`) are never ambiguous and
never warn, which is the argument for preferring them in a sheet.

**Anything unrecognised raises.** Guessing at a deadline is worse than
stopping: the failure mode of a wrong guess is a wrong due date in a live
course, and the failure mode of an error is a message telling the user which
cell to fix.
"""

from __future__ import annotations

import re
from datetime import datetime

#: Unambiguous forms, tried before any day/month reasoning. The canonical form
#: is first so the common case is one try.
DATE_FORMATS = (
    "%Y-%m-%d",      # 2026-02-01   (canonical)
    "%Y/%m/%d",      # 2026/02/01
    "%Y.%m.%d",      # 2026.02.01
    "%d %b %Y",      # 1 Feb 2026
    "%d %B %Y",      # 1 February 2026
    "%b %d %Y",      # Feb 1 2026
    "%B %d %Y",      # February 1 2026
    "%d-%b-%Y",      # 1-Feb-2026   (a common spreadsheet rendering)
    "%Y%m%d",        # 20260201
)

#: `2026年8月2日`, with or without the trailing 日 and with any spacing. Year
#: first, so there is nothing to disambiguate.
JAPANESE_DATE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$")

#: Three numeric parts. Only reached when the year is NOT first, which is
#: exactly the case where US and European order disagree.
NUMERIC_DATE = re.compile(r"^(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})$")

TIME_FORMATS = (
    "%H:%M",         # 23:59        (canonical, no seconds)
    "%H:%M:%S",      # 23:59:59     (canonical, with seconds)
    "%I:%M %p",      # 11:59 PM
    "%I:%M:%S %p",   # 11:59:00 PM
    "%I %p",         # 11 PM
    "%H%M",          # 2359
)

#: `a.m.` / `A.M.` / `pm` all become the `AM`/`PM` strptime wants, and the
#: separator is normalised to a single space.
_MERIDIEM = re.compile(r"\s*([ap])\.?\s*m\.?\s*$", re.I)
_SPACES = re.compile(r"\s+")


class DateFormatError(ValueError):
    """A cell could not be read as a date or a time."""


def _clean(value: str) -> str:
    return _SPACES.sub(" ", (value or "").strip())


def normalize_date(
    value: str, *, where: str = "", warnings: list[str] | None = None
) -> str:
    """Return `YYYY-MM-DD`, or "" for an empty cell.

    `warnings` collects a note for every cell where US and European readings
    were both valid and the US one was assumed. Passing None discards them --
    callers that show the user their sheet should pass a list.
    """
    text = _clean(value)
    if not text:
        return ""
    # A spreadsheet that decided the cell is a datetime writes both parts into
    # it. Keep the date half rather than refusing the whole row.
    if _looks_like_datetime(text):
        text = text.split(" ")[0]

    japanese = JAPANESE_DATE.match(text)
    if japanese:
        year, month, day = (int(p) for p in japanese.groups())
        return _build(year, month, day, value, where)

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    numeric = NUMERIC_DATE.match(text)
    if numeric:
        return _resolve_numeric(numeric, value, where, warnings)

    raise DateFormatError(
        f"cannot read {value!r} as a date{_at(where)}. Accepted forms include "
        f"2026-02-01, 2/1/2026, 1 Feb 2026, and 2026年2月1日."
    )


def _resolve_numeric(
    match: re.Match, original: str, where: str, warnings: list[str] | None
) -> str:
    """Decide day/month order for a bare numeric date.

    Both readings are tested against the calendar. Only when *both* are real
    dates is anything assumed, and that assumption is always reported.
    """
    first, second, year = (int(p) for p in match.groups())
    if year < 100:  # 2-digit year, same window strptime's %y uses
        year += 2000 if year < 69 else 1900

    us = _valid(year, first, second)      # month/day -- US
    european = _valid(year, second, first)  # day/month -- most of Europe

    if us and european:
        if first != second and warnings is not None:
            warnings.append(
                f"{original!r}{_at(where)} is ambiguous: read as "
                f"{us} (US month/day), not {european} (day/month)."
            )
        return us
    if us:
        return us
    if european:
        return european

    raise DateFormatError(
        f"{original!r}{_at(where)} is not a real date in either month/day or "
        f"day/month order."
    )


def _valid(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _build(year: int, month: int, day: int, original: str, where: str) -> str:
    built = _valid(year, month, day)
    if not built:
        raise DateFormatError(f"{original!r}{_at(where)} is not a real date.")
    return built


def normalize_time(value: str, *, where: str = "") -> str:
    """Return `HH:MM`, or "" for an empty cell.

    **Seconds are accepted and discarded.** They are a perfectly reasonable
    thing for someone to type, and several of the formats below carry them, so
    refusing would be hostile -- but Canvas's edit form has a minute-only time
    box and cannot write them. Keeping a second the tool cannot deliver would
    promise something false; dropping it quietly is the graceful degradation.
    """
    text = _clean(value)
    if not text:
        return ""
    text = _MERIDIEM.sub(lambda m: f" {m.group(1).upper()}M", text)

    for fmt in TIME_FORMATS:
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.strftime("%H:%M")
    raise DateFormatError(
        f"cannot read {value!r} as a time{_at(where)}. Accepted forms include "
        f"23:59, 23:59:59, and 11:59 PM."
    )


def _looks_like_datetime(text: str) -> bool:
    return " " in text and ":" in text.split(" ", 1)[1]


def _at(where: str) -> str:
    return f" ({where})" if where else ""
