# canvasser

Automation for UF Canvas (`ufl.instructure.com`) that **navigates the site as a person
would**, rather than through the REST API.

That choice is deliberate. UF [discontinued API token
support](https://elearning.ufl.edu/instructor-help/api-tokens/) after Instructure began
capping non-admin tokens at 30 days, and advises retiring token-based scripts. Browser
automation is the better-supported path for this institution.

> **Status: in use.** A full semester of dates for a real course — 37 assignments, set and
> cleared — pulled to CSV, edited in a spreadsheet, and written back, ending in
> `No changes. 37 sheet row(s) match Canvas exactly.` Plain assignments and classic quizzes
> both. Writing is opt-in (`--commit`), and every field is checked three times: the typed
> values are read back out of the form before saving, Canvas's own error messages are read
> after, and the stored value is re-read from the page's state afterwards.
>
> **Not supported:** assignments with per-student or per-section overrides. Saving Canvas's
> edit form submits *every* date card, so getting that wrong deletes an accommodation date.
> It is refused rather than attempted.

## What it does

A round trip: **pull dates to a CSV, edit them in a spreadsheet, push them back.**

```bash
canvasser status                      # is the stored session still authenticated?
canvasser login                       # authenticate (GatorLink + Duo)
canvasser courses                     # list courses, with ids
canvasser settings 580777             # course details, sections, navigation
canvasser pull 580777                 # assignment dates -> CSV
canvasser push dates-580777.csv       # show what would change; writes nothing
canvasser push dates-580777.csv --commit   # actually write it
canvasser install-browser             # fetch Chromium up front (usually automatic)
```

`push` previews by default. Running it repeatedly while editing a sheet cannot touch the
course; only `--commit` writes.

## Requirements

- Python 3.10+ (developed and tested on 3.13)
- A UF GatorLink account with Duo MFA
- No display required — runs headless

## Install

```bash
pip install canvasser
```

That is the whole install. The first command that needs a browser will notice one is missing
and offer to fetch it:

```
Chromium is not installed; canvasser cannot drive Canvas without it.
Download it now (~150 MB, one time)? [y/N]
```

Answer `y` and it continues into the command you asked for. To do it ahead of time, or in a
script, run `canvasser install-browser` (add `--with-deps` on Linux to pull the system
libraries Chromium needs; that part needs root).

**Why there is a download at all.** The `playwright` package on PyPI ships the automation
library and its driver, not the browser — browser builds are large platform-specific native
binaries, not Python, so no PyPI package carries them. Playwright pins an exact build per
library version, which is the whole reason page behaviour is reproducible.

The browsers land in a **shared per-user cache** (`~/.cache/ms-playwright` and equivalents),
so it is once per machine rather than once per virtualenv — verified under plain venvs,
`pipx`, `uv tool install`, and throwaway `uvx` environments. Nothing is downloaded without
asking: with no terminal attached (cron, CI, a pipe) canvasser declines and prints the
command instead.

### From a checkout instead

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/python -m canvasser install-browser --with-deps
```

### Where it keeps things

The saved session, browser profile, and optional secrets file share one per-user directory —
never the working directory, so nothing credential-bearing can be committed by accident:

| | |
|---|---|
| `%LOCALAPPDATA%\canvasser` | Windows |
| `~/Library/Application Support/canvasser` | macOS |
| `$XDG_STATE_HOME/canvasser` | otherwise — usually `~/.local/state/canvasser` |

Set **`$CANVASSER_HOME`** to put it somewhere else; that is the only thing that overrides the
platform default. Use it to keep state on a durable or encrypted volume, or to run two
identities side by side.

## Credentials

Resolved per field, first source that has them wins:

1. `--secrets-file PATH` (`--username` supplies the name)
2. `$GATORLINK_USERNAME` / `$GATORLINK_PASSWORD`
3. `canvas.env` in the state directory above (mode `600`)
4. an interactive prompt

**There is deliberately no `--password` flag.** OpenSSH is the model: argv is not private —
it lands in shell history and `/proc/<pid>/cmdline` is world-readable. For scripted runs,
`sshpass` answers the prompt, exactly as it does for `ssh`:

```bash
sshpass -f ~/.canvas-pw canvasser --username jjb pull 580777
```

Prompts read `/dev/tty`, not stdin, so this works even when stdin is a pipe. `--no-prompt`
disables prompting entirely for unattended use; `-v` reports which source each value came
from, never the value itself.

### Duo

`login` sends a **Duo push to your phone** — have it in hand. Duo's Verified Push shows a
number that must be tapped; canvasser prints it and re-reads it every 5 seconds in case the
push is re-sent. `--factor passcode` reads a 6-digit code instead.

> If you receive a Duo push you were not expecting, **deny it.**

Your MFA stays intact: the stored password is *something you know*, the phone is *something
you have*. No TOTP seed is stored anywhere. Once Duo remembers the device (~10 hours), runs
need no interaction at all.

## Selecting a course

`courses` and `pull` share three independent scope axes:

| Axis | Default | Flags |
|------|---------|-------|
| enrollment | both active and archived | `--active` / `--archived` |
| publish state | any | `--published` / `--unpublished` |
| favorite | **favorites only** | `--favorite` / `--unmarked`; `--all` opens every axis |

**Naming both sides of an axis unions them** — `--active --archived` is every enrollment,
which is what the words say. Favorites is the one deliberately narrowed default — it is the list you curate in Canvas
itself, via the star on the Courses page. Canvas's own "Current Enrollments" is not a useful
definition of current: on a long-lived account it is mostly sandboxes and dev shells.

`pull` takes a course id **or a name fragment**, and an ambiguous fragment is an error rather
than a guess. Omit it entirely for an interactive picker:

```bash
canvasser pull 580777
canvasser pull "Comp Engr Design"
canvasser pull --course-file ~/current-class.txt
CANVASSER_COURSE=580777 canvasser pull
canvasser pull                          # numbered picker
```

## The datesheet CSV

```
# canvasser datesheet v3.1,,course=580777,timezone=Eastern Time (US & Canada),,,,iana=America/New_York,,
Assignment Details,,,,unlock_at,,due_at,,lock_at,
assignment_id,override_id,title,assign_to,open_date,open_time,due_date,due_time,close_date,close_time
7289050,,01 - Equipment Demonstration,Everyone,2026-08-21,00:00,2026-08-28,23:59,2026-09-02,23:59
```

Three date fields, each split into a **date column and a time column** so a spreadsheet can
bulk-shift them: `open_*` is Canvas's `unlock_at` ("Available from"), `due_*` is `due_at`,
`close_*` is `lock_at` ("Until").

- **Only `assignment_id` is required.** Delete any other column, any row, and both preamble
  rows — the file still reads. Columns are matched by name, so order does not matter either.
- **An absent column means "leave this alone"; an empty cell means "clear it".** Deleting the
  `close_*` columns will not wipe your lock dates.
- Rows are matched on `(assignment_id, override_id)`, never on title — renaming an assignment
  in the spreadsheet cannot retarget a write.
- **The timezone is declared once, in row 1, twice over**: `timezone=` is Canvas's familiar
  name for people, `iana=` is the identifier `push` resolves wall clocks through. Values
  themselves carry no offset.
- **The declared zone says what the sheet's own times are written in** — nothing more. If it
  is not the course's zone, `push` converts every value into course time before comparing or
  writing, preserving the *instant*, and prints what it did. A sheet edited in Tokyo saying
  `2026-08-29 12:59` and a New York course holding `2026-08-28 23:59` are the same deadline.
- **`iana=` wins, but `timezone=` is a real fallback.** Canvas is Rails, so
  `Eastern Time (US & Canada)` is an `ActiveSupport::TimeZone` name and maps to an IANA zone;
  if you delete `iana=` the friendly label still resolves. All 153 of Canvas's labels are
  recognised, including the ones IANA has since renamed (Kyiv, Greenland, Rangoon), and a
  label with Canvas's offset pair still attached is accepted. A label that cannot be read is
  **reported**, not silently ignored. Note `EST` and `-05:00` deliberately do *not* resolve:
  one names half a year, the other says nothing about DST.
- With no zone declared at all, the times are taken to be course-local already.
- **Times are minute-only.** Canvas's time box has no seconds field, so seconds cannot be
  written; `11:59 PM` is what a person types and Canvas applies its own `:59`. Seconds you
  type are accepted and dropped.
- **Dates and times are read forgivingly**, because spreadsheets reformat them:
  `2026-02-01`, `2/1/2026`, `1 Feb 2026`, `2026年2月1日`; `23:59`, `11:59 PM`, `2359`.
  A numeric date that could be US or European is resolved US-first **and reported**; one that
  is real in only one order is read that way silently; one that is real in neither is refused.

### What `push` will not do

- **assignments with per-section or per-student overrides** — saving Canvas's edit form
  submits *every* date card, so a wrong move there would silently delete a student's
  accommodation date. Untested, so refused outright;
- **a time that does not exist**, in the hour skipped by a daylight-saving change;
- **a date with no time, when the sheet's zone differs from the course's** — converting
  would have to assume a time, and a different assumed time lands on a different *date*;
- **anything Canvas itself will reject.** Its rules are enforced server-side and reported
  only on the page, so `push` both checks what it can up front and reads Canvas's answer
  back afterwards:

```
4 row(s) have dates Canvas will not accept:
    7289053: lock_at 2026-10-04 23:59 is before due_at 2026-10-09 23:59 -- Canvas
             refuses this ('Until date cannot be before due date')
    Fix these cells in the sheet; they will be skipped.
```

  Ordering is caught before any page is loaded. Other rules — dates before the term start,
  for instance — surface as Canvas's own wording after the save attempt. Either way the row
  is skipped and the rest of the push proceeds.

**Clearing a date works**: leave the cells empty in a column the sheet carries, and `push`
uses the form's own Clear control.

### When something fails, it says why

Every failure prints its reason against the line it happened on, rather than a bare count.
A post-write mismatch distinguishes *Canvas still holds its previous value* (the save was
rejected — its message is on the edit form) from *Canvas holds a third value* (it accepted
the write and then altered it).

After writing, `push` re-reads the assignment's own page state and compares date *and* time,
so a save that silently did not take is reported rather than assumed.

## Data handling

This repository is public.

- **Credentials never live in the repo.** They belong in the state directory described under
  "Where it keeps things", which is deliberately outside any working tree.
- `storage_state.json` (the saved session) is **credential-equivalent** — it grants Canvas
  access with no password.
- The browser profile holds live session cookies and is **as sensitive as the password**.
- Debug snapshots render real Canvas pages, which can include student data. They are written
  outside the repository on purpose.
- Pulled CSVs are gitignored: they will contain per-student rows once individual overrides
  are in scope.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

This program is free software: you may redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. It comes with **absolutely no
warranty**.

## Scope: this is a UF tool

`CANVAS_BASE_URL` is overridable, but the login path is not: the SSO deep link
(`/login/saml/355`) and the identity provider (`login.ufl.edu`) are UF's, and the second
factor assumes Duo. Another institution's Canvas will read fine only if you can already
authenticate to it some other way — sign-in is not portable as written.
