# canvasser

Automation for UF Canvas (`ufl.instructure.com`) that **navigates the site as a person
would**, rather than through the REST API.

That choice is deliberate. UF [discontinued API token
support](https://elearning.ufl.edu/instructor-help/api-tokens/) after Instructure began
capping non-admin tokens at 30 days, and advises retiring token-based scripts. Browser
automation is the better-supported path for this institution.

> **Status: the round trip works, live.** Dates pulled to CSV, edited in a spreadsheet, and
> written back to Canvas — verified against a real course, for both plain assignments and
> classic quizzes. Writing is opt-in (`--commit`) and every write is read back and checked.

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
```

`push` previews by default. Running it repeatedly while editing a sheet cannot touch the
course; only `--commit` writes.

## Requirements

- Python 3.13
- A UF GatorLink account with Duo MFA
- Linux; no display required (runs headless)

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install playwright
./.venv/bin/python -m playwright install --with-deps chromium
```

## Credentials

Resolved per field, first source that has them wins:

1. `--secrets-file PATH` (`--username` supplies the name)
2. `$GATORLINK_USERNAME` / `$GATORLINK_PASSWORD`
3. `~/vault/rw/secrets/canvas.env` (mode `600`)
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
- **Times are minute-only.** Canvas's time box has no seconds field, so seconds cannot be
  written; `11:59 PM` is what a person types and Canvas applies its own `:59`. Seconds you
  type are accepted and dropped.
- **Dates and times are read forgivingly**, because spreadsheets reformat them:
  `2026-02-01`, `2/1/2026`, `1 Feb 2026`, `2026年2月1日`; `23:59`, `11:59 PM`, `2359`.
  A numeric date that could be US or European is resolved US-first **and reported**; one that
  is real in only one order is read that way silently; one that is real in neither is refused.

### What `push` refuses to do

Each of these is refused because it is untested, not because it is hard:

- **assignments with per-section or per-student overrides** — saving Canvas's edit form
  submits *every* date card, so a wrong move there would silently delete a student's
  accommodation date;
- **clearing a date**;
- **a sheet whose timezone no longer matches the course's** — it tells you to re-pull;
- **a time that does not exist**, in the hour skipped by a daylight-saving change.

After writing, `push` re-reads the assignment's own page state and compares date *and* time,
so a save that silently did not take is reported rather than assumed.

## Data handling

This repository is public.

- **Credentials never live in the repo.** They belong in `~/vault/rw/secrets/`.
- `storage_state.json` (the saved session) is **credential-equivalent** — it grants Canvas
  access with no password.
- The browser profile holds live session cookies and is **as sensitive as the password**.
- Debug snapshots render real Canvas pages, which can include student data. They are written
  outside the repository on purpose.
- Pulled CSVs are gitignored: they will contain per-student rows once individual overrides
  are in scope.
