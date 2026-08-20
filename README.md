# canvasser

Automation for UF Canvas (`ufl.instructure.com`) that **navigates the site as a person
would**, rather than through the REST API.

That choice is deliberate. UF [discontinued API token
support](https://elearning.ufl.edu/instructor-help/api-tokens/) after Instructure began
capping non-admin tokens at 30 days, and advises retiring token-based scripts. Browser
automation is the better-supported path for this institution.

> **Status: early.** Authentication scaffolding is built and partially verified. The Duo
> second-factor flow has not yet been exercised against a live login. No task automation
> exists yet.

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

Credentials are read from `~/vault/rw/secrets/canvas.env` (mode `600`), or from real
environment variables, which take precedence:

```ini
GATORLINK_USERNAME=
GATORLINK_PASSWORD=
CANVAS_BASE_URL=https://ufl.instructure.com
```

This file lives outside the repository on purpose and must never be committed.

## Usage

```bash
./.venv/bin/python -m canvasser status     # is the stored session still valid?
./.venv/bin/python -m canvasser login      # authenticate (sends a Duo push)
./.venv/bin/python -m canvasser courses    # list courses
```

`login` sends a **Duo push to your phone** — have it in hand. If push is inconvenient, use
`--factor passcode` to type a 6-digit code from Duo Mobile or a hardware token instead.

> If you receive a Duo push you were not expecting, **deny it.**

## How authentication works

A persistent browser profile keeps Canvas's session cookie and Duo's remembered-device
cookie between runs, so most invocations need no interaction at all. When the session has
lapsed, `canvasser` fills the GatorLink SSO form and hands off to Duo for approval.

Your MFA stays intact: the stored password is *something you know*, the phone is *something
you have*. **No TOTP seed is stored**, so possession of this repo plus the credentials file
is still not enough to log in as you.

Duo's remembered-device window is roughly 10 hours, which bounds how long runs can proceed
untouched.

## Security notes

- The browser profile holds live session cookies and is **as sensitive as the password**. It
  is stored in the vault alongside credentials, not in this repo.
- Debug snapshots capture rendered Canvas pages, which contain **student names, grades, and
  submissions — FERPA-protected data**. They are written outside the repository by design.
  Do not relocate them into it, and do not `git add -f` them.
- This tool is single-user. Using credentials belonging to anyone else violates both Canvas
  API policy and UF's Authentication Management Policy, and UF holds the account holder
  responsible for all activity under the account.

## Layout

| Path | Purpose |
|------|---------|
| `canvasser/config.py` | Credentials and paths |
| `canvasser/browser.py` | Persistent Playwright context, debug snapshots |
| `canvasser/duo.py` | Second-factor handling behind an `Approver` interface |
| `canvasser/auth.py` | SSO login and session liveness |
| `canvasser/cli.py` | Command-line entry point |
| `scripts/canvas_check.py` | REST API smoke test — development convenience, not the product |
