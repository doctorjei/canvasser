# canvasser

Automation for UF Canvas (`ufl.instructure.com`) that **navigates the site as a person
would**, rather than through the REST API.

That choice is deliberate. UF [discontinued API token
support](https://elearning.ufl.edu/instructor-help/api-tokens/) after Instructure began
capping non-admin tokens at 30 days, and advises retiring token-based scripts. Browser
automation is the better-supported path for this institution.

> **Status: working, read-only.** Login, session reuse, course listing, and pulling
> assignment due dates to CSV all work against the live site. Nothing writes to Canvas yet —
> `push` (applying an edited CSV back) is the next piece.

## What it does

The goal is a round trip: **pull dates to a CSV, edit them in a spreadsheet, push them
back.** The read half exists.

```bash
canvasser status                      # is the stored session still authenticated?
canvasser login                       # authenticate (GatorLink + Duo)
canvasser courses                     # list courses, with ids
canvasser pull "Wednesday Fall 26"    # assignment due dates -> CSV
```

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
sshpass -f ~/.canvas-pw canvasser --username jjb pull 574855
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
| favorite | **favorites only** | `--all` to include the rest |

Favorites is the one deliberately narrowed default — it is the list you curate in Canvas
itself, via the star on the Courses page. Canvas's own "Current Enrollments" is not a useful
definition of current: on a long-lived account it is mostly sandboxes and dev shells.

`pull` takes a course id **or a name fragment**, and an ambiguous fragment is an error rather
than a guess. Omit it entirely for an interactive picker:

```bash
canvasser pull 574855
canvasser pull "Wednesday Fall 26"
canvasser pull --course-file ~/current-class.txt
CANVASSER_COURSE=574855 canvasser pull
canvasser pull                          # numbered picker
```

## The datesheet CSV

```
# canvasser datesheet v2 course=574855
assignment_id,override_id,title,assign_to,due_at
7256241,818682,Civil Quiz,EGS1006-02F3(11989),2026-09-02 18:00 -0400
7256241,,Civil Quiz,Everyone else,
```

- **Only `due_at` is editable.** The other columns are for orientation and matching.
- One row per **assign-to target**: an assignment with per-section dates gets one row per
  section, plus a base row (empty `override_id`).
- Rows are matched on `(assignment_id, override_id)`, never on title — so renaming an
  assignment in the spreadsheet cannot retarget a write.
- Dates are in **course time with an explicit offset** (`2026-11-30 23:59:59 -0500`). An
  assignment has no timezone of its own; Canvas stores one instant and renders it per viewer,
  so the offset is what makes the value unambiguous across DST.

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
