# canvasser

Automation for Canvas (`*.instructure.com`) that **navigates the site as a person would**,
rather than through the REST API. Built against UF; other institutions need one setting
(see [Scope](#scope-official-instructure-hosts-only)).

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
> **Assignment settings** — points, grading type, submission types, allowed attempts, peer
> review, publish state — are pulled to a second CSV. Writing them is newer than the date
> path: everything but publish state can be written. Anything not yet writable is reported
> rather than silently ignored.
>
> **Not supported:** assignments with per-student or per-section overrides. Saving Canvas's
> edit form submits *every* date card, so getting that wrong deletes an accommodation date.
> It is refused rather than attempted — for settings writes too, since they save the same
> form.

## What it does

A round trip: **pull to CSV, edit in a spreadsheet, push it back.**

```bash
canvasser status                      # is the stored session still authenticated?
canvasser login                       # authenticate (GatorLink + Duo)
canvasser courses                     # list courses, with ids
canvasser settings 580777             # course details, sections, navigation
canvasser pull 580777                 # dates AND settings -> two CSVs
canvasser pull 580777 --dates         # just dates-580777.csv
canvasser pull 580777 --info          # just info-580777.csv
canvasser push dates-580777.csv       # show what would change; writes nothing
canvasser push dates-580777.csv --commit   # actually write it
canvasser install-browser             # fetch Chromium up front (usually automatic)
canvasser --institution templeu status     # a different Canvas, with its own session
```

`push` previews by default. Running it repeatedly while editing a sheet cannot touch the
course; only `--commit` writes.

**`push` works out which kind of sheet it was given by reading the file's first row**, not
its name — so `push info-580777.csv` reaches the settings path even if you rename the file.
`--dates` and `--info` on `push` are assertions rather than switches: they refuse a file
that says it is the other kind.

Both sheets come from the same page loads, so asking for one is no faster than asking for
both.

Reading a course means loading one page per assignment, so `pull` and `push` show a progress
bar while they work. Piped or redirected, they print a plain numbered list instead — no
escape sequences, so a captured log stays readable. Set `CANVASSER_ASCII=1` to force plain
ASCII drawing on a terminal that cannot render box characters.

## Requirements

- Python 3.10+ (developed and tested on 3.13)
- A Canvas account at an `*.instructure.com` institution (developed against
  UF GatorLink with Duo MFA)
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

The saved session, browser profile, optional secrets file, and any debug snapshots share one
per-user directory — never the working directory, so nothing credential-bearing can be
committed by accident. (Snapshots render whole Canvas pages, so they can contain student
data; the directory is created mode 700.)

| | |
|---|---|
| `%LOCALAPPDATA%\canvasser` | Windows |
| `~/Library/Application Support/canvasser` | macOS |
| `$XDG_STATE_HOME/canvasser` | otherwise — usually `~/.local/state/canvasser` |

Set **`$CANVASSER_HOME`** to put it somewhere else; that is the only thing that overrides the
platform default. Use it to keep state on a durable or encrypted volume.

For a second *institution*, use `--institution <subdomain>` rather than a second
`$CANVASSER_HOME`: it keeps that Canvas's credentials, session and browser profile in a
subdirectory of the same state directory, so the two cannot overwrite each other's login.
The account you already have stays exactly where it is.

## Credentials

Resolved per field, first source that has them wins:

1. `--secrets-file PATH` (`--username` supplies the name)
2. `$CANVAS_USERNAME` / `$CANVAS_PASSWORD`
3. `canvas.env` in the state directory above (mode `600`)
4. an interactive prompt

The former `$GATORLINK_USERNAME` / `$GATORLINK_PASSWORD` spellings are still read, so an
existing `canvas.env` keeps working; `-v` reports them as deprecated.

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

## The infosheet CSV

Everything about an assignment that is *not* a date:

```
# canvasser infosheet v1,,course=580777,,,,,,,,
Assignment Details,,,,Grading,,Submission,,Availability,,
assignment_id,title,kind,assignment_group,points_possible,grading_type,submission_types,allowed_attempts,published,peer_reviews,override_count
7289050,01 - Equipment Demonstration,assignment,Intro Assignments,30,points,external_tool,-1,true,false,0
```

**A separate file from the datesheet, and the reason is one rule, not two.** A date is the
one field Canvas lets an assignment simply *not have* — so on the datesheet an empty cell
**clears** the date. Nothing here can be unset: an assignment always has a publish state,
always sits in exactly one group, and an empty points box is coerced to `0`, which would be
a silent grade change rather than an absence. So on the infosheet **an empty cell means
"leave this field alone"**, the same as deleting the column. There is no "clear" marker
because there is nothing for it to mark.

- Rows are matched on `assignment_id` alone. **There is no `override_id`** — these settings
  are per assignment, not per date card.
- **Read-only columns:** `kind`, `assignment_group`, `override_count`. They are written so
  the sheet is legible and ignored on the way back in. (`title` is editable — see below —
  but a rename is reported rather than written, pending a flag that does not exist yet.)
- **`kind` explains the blanks.** A classic quiz's page carries no `grading_type`,
  `submission_types` or `peer_reviews` at all, so those cells are empty on every quiz row —
  that is a fact about quizzes, not a failed read.
- **`override_count` tells you which assignments carry overrides** before you start editing,
  rather than when `push` refuses the row.
- Values are written exactly as Canvas reports them, including `allowed_attempts = -1`
  for unlimited.

### What `push` writes from it

**`points_possible`, `grading_type`, `submission_types`, `allowed_attempts` and
`peer_reviews` today.** `published` is the one remaining column, reported per row as
`NOT WRITABLE YET` and skipped — never silently dropped.

Each is compared by **meaning rather than text**, so a spreadsheet's reformatting is not
mistaken for an edit: points numerically (`8.34` = `8.340`), submission types as a set
(order does not matter), attempts as an integer (`3` = `3.0`), peer review as a boolean
(`TRUE` = `true`, which is what a spreadsheet writes back). Comparing these as strings
would report a change nobody made, write it, and report it again on every push afterwards.

**Renaming is recognised but not yet written.** A changed `title` is reported as needing
`--rename`, a flag that does not exist yet — the gate is built, the write is not. The gate
is deliberate: the title is also the column you read to find your row, so an edit made to
keep the sheet legible should not quietly rename what students see.

**`submission_types`** takes the online sub-types (`online_upload`, `online_text_entry`,
`online_url`, `media_recording`, `student_annotation`) or a whole mode (`none`, `on_paper`).
`external_tool` is refused: it needs a tool URL this sheet has no column for, so writing it
would leave an assignment configured for a tool it does not have.

**`peer_reviews`** takes `true` or `false` — and also `yes`/`no` and `1`/`0`, because a
spreadsheet that recognises `true` as a boolean re-saves it as `TRUE`. Anything else is
refused before a page is loaded rather than guessed at either way.

**`allowed_attempts`** takes a positive count, or `-1` for unlimited — Canvas's own
encoding, which is what the sheet carries. Canvas hides the control unless the assignment
accepts submissions, so limiting attempts on a `none` submission type is refused, naming
`submission_types` as the thing to set first.

`grading_type` takes the option **values**, not the words on the form: `points`, `percent`,
`letter_grade`, `gpa_scale`, `pass_fail`, `not_graded`. Anything else is refused before a
page is loaded, naming what is accepted:

```
Check-In 1   #7289061
    grading_type='Points' is not one of points, percent, letter_grade, gpa_scale,
    pass_fail, not_graded (these are the option values, not the words shown on the form)
```

Changing points on an assignment that **already has graded submissions** re-scales every
student's percentage, so it is called out against the row and again before writing. It is a
warning, not a refusal — the write proceeds.

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

## Scope: official Instructure hosts only

Built against UF and extended to other institutions. Point it elsewhere with
`--institution <subdomain>`, which keeps that Canvas's credentials, saved session and
browser profile in their own directory — so a second account cannot overwrite the first's
session. Selection follows the usual precedence: `--institution`, then
`$CANVAS_INSTITUTION`, then `default_institution` in the secrets file, then the account you
already set up.

Two things another institution must supply, because neither is guessable:

- **`CANVAS_SSO_PATH`** — the Canvas login route. There is deliberately no default:
  `/login/saml/355` is *UF's own* SAML provider id, and using it elsewhere would send your
  credentials to UF's identity provider. Unset, a non-UF institution is refused.
- **A supported second factor.** Duo is what has been implemented and tested. The factor
  sits behind an interface, so another one is an addition rather than a rewrite — but it is
  not written yet.

**Only `*.instructure.com` hosts are supported.** Self-hosted and vanity-domain Canvas are
refused outright: the institution is identified by its subdomain, and a host without one
would make two different schools indistinguishable in the state directory.
