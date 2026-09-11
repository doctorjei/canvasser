"""Live progress and session chrome for the commands that walk a course.

Layout is the user's spec; `designs/progress-display-spec.md` in the project's
own notes is authoritative, and the mock it was derived from is transcribed
there. Every width in this module is load-bearing.

**Three tiers, on two independent axes.** Charset and capability are separate
questions and must not be collapsed into one flag:

===========  =========  ==========  =====================================
tier         charset    repaint     chosen when
===========  =========  ==========  =====================================
1            Unicode    yes         UTF-8 stream on a real terminal
2            ASCII      yes         non-UTF-8 stream on a real terminal
3            ASCII      no          a pipe, a log, or TERM=dumb
===========  =========  ==========  =====================================

Tier 2 keeps colour -- it is a *charset* fallback, not a plain-text one. Tier 3
is the plain-text one: it never emits a control sequence and never rewrites a
line, so it survives being piped into a file. It draws a numbered two-column
list instead of a bar, because a bar that cannot repaint is just noise.

The tier is decided once, in `Progress.__init__`, and everything below reads
it off the object. Nothing here inspects the environment a second time.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

from .display import (BOLD_WHITE_U, GREY, RESET, colors_enabled,
                      display_width, fit, shorten)

# --- geometry ---------------------------------------------------------------

#: Cells in the bar itself, between its two edge characters. Fixed: the user's
#: mock is 50 wide on every row, and the title's truncation is measured against
#: it, so this is not a number that may drift with the terminal.
BAR_W = 50

#: Past this the title stops floating right of the fill and anchors left, so
#: the bar's leading edge stays visible. Exactly half; the user's edge cases
#: pin it -- a 25-cell fill still floats, 26 anchors.
ANCHOR_AT = BAR_W // 2

#: The bar's own colour (user, 2026-08-23, "for now"). `GREY` is colour 37.
BAR_COLOUR = GREY

#: Overall drawn width. Like droste's DESIGN_W this clamps down on a narrow
#: terminal but never stretches on a wide one: these blocks are read as a fixed
#: shape, not as flowed text.
DESIGN_W = 69

#: Inner width of the Duo frame. 34 columns drawn, including both edges.
DUO_W = 32


@dataclass(frozen=True)
class Glyphs:
    """One charset's drawing characters.

    The ASCII set is not a degradation of the Unicode one, it is a twin: the
    sparkle replacements are two columns each exactly like `✨`, so a box keeps
    its geometry across the two and only its glyphs change.
    """

    full: str
    dark: str
    medium: str
    light: str
    bar_l: str
    bar_r: str
    star_l: str
    star_r: str
    rule: str
    #: top-left, top-right, bottom-left, bottom-right, horizontal, vertical
    heavy: tuple[str, str, str, str, str, str]
    light_box: tuple[str, str, str, str, str, str]


UNICODE = Glyphs(
    full="█", dark="▓", medium="▒", light="░",
    bar_l="│", bar_r="│", star_l="✨", star_r="✨", rule="─",
    heavy=("┏", "┓", "┗", "┛", "━", "┃"),
    light_box=("┌", "┐", "└", "┘", "─", "│"),
)

#: Every shade collapses to `#`. The gradient is invisible here, which is the
#: user's own spec -- an ASCII bar reads as one solid run and that is fine.
ASCII = Glyphs(
    full="#", dark="#", medium="#", light="#",
    bar_l="[", bar_r="]", star_l=".*", star_r="*.", rule="-",
    heavy=(".", ".", "`", "'", "-", "|"),
    light_box=(".", ".", "`", "'", "-", "|"),
)


# --- the bar ----------------------------------------------------------------


def gradient(filled: int) -> tuple[int, int, int]:
    """How many dark / medium / light cells the tail carries.

    Not one length that grows -- three shades that grow independently. Each
    starts at one and gains one every 8 cells of bar, cycling medium, dark,
    light (the user's schedule: 8, 16, 24, 32, 40, 48). An earlier reading of
    `3 + filled // 8` matches the short bars and diverges past 32.
    """
    steps = filled // 8
    return (1 + (steps + 1) // 3,     # dark
            1 + (steps + 2) // 3,     # medium
            1 + steps // 3)           # light


def bar_cells(filled: int, glyphs: Glyphs) -> str:
    """`filled` cells: a solid run, then the gradient tail."""
    dark, medium, light = gradient(filled)
    tail = glyphs.dark * dark + glyphs.medium * medium + glyphs.light * light
    if filled <= len(tail):
        # A bar shorter than its own tail shows the tail's END, so the first
        # cell to appear is the lightest rather than the darkest.
        return tail[len(tail) - filled:]
    return glyphs.full * (filled - len(tail)) + tail


def right_padding(filled: int) -> int:
    """MINIMUM bar cells kept to the right of an anchored title.

    A minimum, not a width: when the title is shorter than the room available
    the surplus stays bar rather than becoming margin. Reproduces the user's
    schedule -- 26 to 1, 27/28 to 2, 29/30 to 3, 31 and up to 4.
    """
    return min(4, (filled - 23) // 2)


def render_bar(title: str, done: int, total: int, glyphs: Glyphs = UNICODE) -> str:
    """One bar row, exactly `BAR_W` columns.

    The bar is filled, then the title is PAINTED OVER it -- which is the whole
    model. What look like two layouts are one: past halfway the title simply
    covers the middle of a longer bar, leaving cells showing on both sides.
    """
    filled = round(done / total * BAR_W) if total else 0
    cells = bar_cells(filled, glyphs)

    if filled <= ANCHOR_AT:
        # Floating: the whole fill sits left of the title.
        body = cells + " " + shorten(title, BAR_W - filled - 2)
        return fit(body, BAR_W)

    shown = shorten(title, filled - 3 - right_padding(filled))
    # Whatever the title did not use stays bar, so a short title shows more
    # tail than the minimum demands.
    trailing = filled - 3 - display_width(shown)
    return fit(cells[0] + " " + shown + " " + cells[filled - trailing:], BAR_W)


def render_finished(glyphs: Glyphs = UNICODE) -> str:
    """The closing row: a full bar with `[DONE]` centred in it."""
    label = " [DONE] "
    cells = bar_cells(BAR_W, glyphs)
    room = BAR_W - len(label)
    left = room // 2
    return cells[:left] + label + cells[BAR_W - (room - left):]


def _tail_field(counter: int, done: int, total: int) -> str:
    """`N/37,  24.3%` -- padded so the bar's right edge cannot move.

    **The two halves count different things**, and collapsing them into one
    number is the mistake this signature exists to prevent. `counter` names the
    item being FETCHED; `done` names how many are FINISHED. So the first row is
    `1/37, 0.0%` and the last is `37/37, 97.3%` -- one item in hand, none of it
    finished, then the last one in hand with 36 behind it.
    """
    width = len(str(total))
    pct = (done / total * 100) if total else 0.0
    return f"{counter:>{width}}/{total}, {pct:>5.1f}%"


# --- boxes ------------------------------------------------------------------


def _centre(text: str, width: int) -> str:
    room = width - display_width(text)
    return " " * (room // 2) + text + " " * (room - room // 2)


def _box(lines: list[str], width: int, corners) -> list[str]:
    top_l, top_r, bot_l, bot_r, horiz, vert = corners
    out = [top_l + horiz * width + top_r]
    out += [vert + _centre(line, width) + vert for line in lines]
    out.append(bot_l + horiz * width + bot_r)
    return out


#: How far a box is indented when printed. Duo's, adopted as the shared value
#: so a second provider's prompt lines up with the first rather than merely
#: looking similar.
BOX_INDENT = " " * 13


def print_box(lines: list[str], stream=None) -> None:
    """Put a box on the screen, blank line above and below.

    **To stderr by default, as Duo's always has been** -- this is a prompt to a
    human, not part of any output being captured. One implementation so two
    providers cannot drift apart on the indent.
    """
    stream = stream if stream is not None else sys.stderr
    print("\n" + "\n".join(BOX_INDENT + line for line in lines) + "\n",
          file=stream, flush=True)


def code_box(code: str | None, *, heading: str, instruction: str,
             glyphs: Glyphs = UNICODE) -> list[str]:
    """A number a human must match, framed so it cannot be missed.

    **Generalised from `duo_box` because a second provider needed the same
    thing** (2026-09-11). Duo and Microsoft Entra ID ask the identical question
    -- here is a number, tap it on your phone -- and the number exists only on a
    page in a headless browser nobody can see. Entra's arrived as one
    unremarkable line among nine, at exactly the moment it matters: a ~60s
    window, on someone else's account.

    **Absence is normal, not an error, at both providers**: Duo returns no
    number when the tenant has Verified Push switched off, and at Entra the
    number may simply not have been found. The frame is drawn either way so the
    screen keeps its shape, and so that the *prompt* is unmissable even when the
    number is not in hand -- which is the half that does not depend on getting
    any selector right.

    `heading` and `instruction` are the caller's, because the words name the app
    the human has to open and there is no generic phrasing for that. Keep both
    inside `DUO_W`; nothing here wraps.
    """
    # Centred over the frame, but NOT padded out to it: this line has no
    # right-hand edge to meet, and a trailing space is just invisible litter.
    head = _centre(heading, DUO_W + 2).rstrip()
    if code is None:
        body = [f"{glyphs.star_l} {instruction} {glyphs.star_r}"]
        return [head] + _box(body, DUO_W, glyphs.heavy)

    spaced = " ".join(code)
    cell = f" {glyphs.star_l} {spaced} {glyphs.star_r} "
    inner = _box([], display_width(cell), glyphs.light_box)
    vert = glyphs.light_box[5]
    body = ["CODE:", inner[0], vert + cell + vert, inner[-1]]
    return [head] + _box(body, DUO_W, glyphs.heavy)


def duo_box(code: str | None, glyphs: Glyphs = UNICODE) -> list[str]:
    """Duo's Verified Push number, or the instruction when there is none.

    **Kept as its own name, and its output must not move.** The user's original
    mock is the fixture `progress_check.py` asserts against, so this is a
    wrapper over `code_box` rather than a rewrite: the words are Duo's, the
    frame is shared.
    """
    return code_box(code, heading="Duo App: Please Authenticate Now",
                    instruction="Open Duo and Approve", glyphs=glyphs)


def stat_box(rows: list[tuple[str, int]], glyphs: Glyphs = UNICODE) -> list[str]:
    """The pull summary. Labels left, counts right-aligned in four columns."""
    top_l, top_r, bot_l, bot_r, horiz, vert = glyphs.light_box
    out = [top_l + horiz * 29 + top_r]
    out += [f"{vert}  {label:<21}{count:>4}  {vert}" for label, count in rows]
    out.append(bot_l + horiz * 29 + bot_r)
    return out


def banner(text: str = "Canvasser", width: int = DESIGN_W,
           glyphs: Glyphs = UNICODE) -> str:
    label = f"  {text}  "
    room = width - display_width(label)
    return glyphs.rule * (room // 2) + label + glyphs.rule * (room - room // 2)


# --- tier selection ---------------------------------------------------------


def terminal_width(stream=None) -> int:
    """Drawn width: clamps down on a narrow terminal, never stretches.

    `os.get_terminal_size` consults the stream and then COLUMNS, which is the
    right order -- bash only maintains COLUMNS in an interactive shell, so a
    piped run that trusted it would pin itself to the fallback forever.
    """
    try:
        columns = os.get_terminal_size(
            (stream or sys.stdout).fileno()).columns
    except (OSError, ValueError, AttributeError):
        columns = int(os.environ.get("COLUMNS") or 80)
    return max(20, min(DESIGN_W, columns - 1))


def _wants_ascii(stream) -> bool:
    if os.environ.get("CANVASSER_ASCII"):
        return True
    encoding = (getattr(stream, "encoding", "") or "").lower()
    return "utf" not in encoding


def _can_repaint(stream) -> bool:
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return os.environ.get("TERM", "dumb") not in ("dumb", "unknown")


# --- the driver -------------------------------------------------------------

#: Tier 3's grid: a 3-column index, two spaces, a 31-column title, twice over
#: with a five-space gutter -- 79 columns, the same budget as the course table.
LIST_INDEX, LIST_TITLE, LIST_GAP = 3, 31, 5


class Progress:
    """A progress display over a known number of items.

    `advance` is called BEFORE each item is fetched, which is what makes the
    percentage mean "finished so far" rather than "finished including this".
    """

    #: Repaints are cheap but not free, and a fast loop should not flood the
    #: terminal. Each item here costs ~2s, so this only ever guards a cache hit.
    THROTTLE_S = 0.1

    def __init__(self, total: int, stream=None, label: str = "assignments"):
        self.stream = stream or sys.stdout
        self.total = total
        self.label = label
        self.ascii = _wants_ascii(self.stream)
        self.repaint = _can_repaint(self.stream)
        self.glyphs = ASCII if self.ascii else UNICODE
        self._last_paint = 0.0
        self._pending: list[tuple[int, str]] = []
        self._opened = False
        self._closed = False

    # -- tier 3 ------------------------------------------------------------

    def _append_only(self, index: int, title: str) -> None:
        cell = f"{index:<{LIST_INDEX}}  {fit(title, LIST_TITLE)}"
        # A repeated index is a RELABEL, not a second item. `push` is handed ids
        # rather than titles, so it opens a row as `#7289047` and calls again
        # with the real name once the page gives it up. Appending both made a
        # pair mean "one item twice" -- the index repeated on every line and a
        # 19-line log ran to 37.
        if self._pending and self._pending[-1][0] == index:
            self._pending[-1] = (index, cell)
            return
        # Flush on the arrival of a NEW item, not on reaching two. A cell is
        # only final once the walk has moved past its index; flushing at two
        # would print the second half of the pair while it was still `#<id>`,
        # and a relabel arriving after the line is out has nowhere to land.
        if len(self._pending) == 2:
            self._flush_pair()
        self._pending.append((index, cell))

    def _flush_pair(self) -> None:
        if not self._pending:
            return
        line = (" " * LIST_GAP).join(cell for _, cell in self._pending)
        self.stream.write("  " + line.rstrip() + "\n")
        self.stream.flush()
        self._pending = []

    # -- public ------------------------------------------------------------

    def start(self) -> None:
        if self._opened:
            return
        self._opened = True
        if not self.repaint:
            self.stream.write(
                f"  Downloading details for {self.total} {self.label}...\n")
            self.stream.flush()

    def advance(self, index: int, title: str) -> None:
        """Report that item `index` (1-based) is about to be fetched."""
        self.start()
        if not self.repaint:
            self._append_only(index, title)
            return
        now = time.monotonic()
        if index > 1 and now - self._last_paint < self.THROTTLE_S:
            return
        self._last_paint = now
        self._paint(render_bar(title, index - 1, self.total, self.glyphs),
                    _tail_field(index, index - 1, self.total))

    def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.repaint:
            self._flush_pair()
            self.stream.write("  ### Done! ###\n")
            self.stream.flush()
            return
        self._paint(render_finished(self.glyphs),
                    _tail_field(self.total, self.total, self.total))
        self.stream.write("\n")
        self.stream.flush()

    def _paint(self, bar: str, tail: str) -> None:
        painted = f"{BAR_COLOUR}{bar}{RESET}" if self._colour() else bar
        self.stream.write(
            f"\r  {self.glyphs.bar_l}{painted}{self.glyphs.bar_r} {tail}\033[K")
        self.stream.flush()

    def _colour(self) -> bool:
        return self.repaint and not os.environ.get("NO_COLOR")

    def __enter__(self) -> "Progress":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        # Close the line even when the walk raised: a traceback printed onto a
        # half-drawn bar is unreadable, and the bar is the less important of
        # the two.
        self.finish()


# --- session chrome ---------------------------------------------------------

#: The timezone line under the course title. Italic, plain grey -- it is
#: reference material, not something to act on.
ITALIC_GREY = "\033[3;37m"


def glyphs_for(stream=None) -> Glyphs:
    """The charset this stream can actually spell."""
    return ASCII if _wants_ascii(stream or sys.stdout) else UNICODE


def session_banner(text: str = "Canvasser", stream=None) -> str:
    return banner(text, terminal_width(stream), glyphs_for(stream))


def course_heading(settings) -> str:
    """`Long Name: CODE (#id)` over the course's timezone, both spellings.

    The zone is shown twice on purpose, exactly as the sheet records it: the
    familiar name is what a person reads, the IANA id is what `push` resolves
    wall-clock times through. Seeing both here means a surprising conversion
    later can be traced back to what the course actually said.
    """
    title = f"{settings.name}: {settings.course_code} (#{settings.course_id})"
    zone = " / ".join(part for part in (settings.timezone_label,
                                        settings.course_timezone) if part)
    if colors_enabled():
        title = f"{BOLD_WHITE_U}{title}{RESET}"
        zone = f"{ITALIC_GREY}{zone}{RESET}"
    return f"  {title}\n  {zone}"
