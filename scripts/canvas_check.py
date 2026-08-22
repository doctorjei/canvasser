#!/usr/bin/env python3
"""Verify Canvas API credentials and report what the token can see.

SUPERSEDED. The project drives a browser, not the REST API -- UF discontinued
token support and the full round trip works without one. Kept because it is
still the cheapest way to check a token if one ever exists.

Reads CANVAS_BASE_URL / CANVAS_API_TOKEN from the same secrets file canvasser
uses (canvasser.config.state_dir()), overridable with --env-file
(or from the environment, which wins). Prints the authenticated user, the
token's scope/expiry as Canvas reports it, and the active courses it can
reach -- enough to confirm creds work before building anything on top.

Usage:
    python3 scripts/canvas_check.py [--env-file PATH]
"""

import argparse
import os
import sys
from pathlib import Path

import requests

try:                                    # keep this runnable standalone
    from canvasser.config import ENV_FILE as DEFAULT_ENV_FILE
except ImportError:                     # pragma: no cover
    DEFAULT_ENV_FILE = Path.home() / ".local/state/canvasser/canvas.env"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file, ignoring blanks and # comments."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def get_config(env_file: Path) -> tuple[str, str]:
    file_values = load_env_file(env_file)
    base_url = os.environ.get("CANVAS_BASE_URL") or file_values.get("CANVAS_BASE_URL", "")
    token = os.environ.get("CANVAS_API_TOKEN") or file_values.get("CANVAS_API_TOKEN", "")

    if not base_url:
        sys.exit(f"No CANVAS_BASE_URL set (checked environment and {env_file}).")
    if not token:
        sys.exit(
            f"No CANVAS_API_TOKEN set (checked environment and {env_file}).\n"
            f"Generate one at {base_url}/profile/settings -> '+ New Access Token'."
        )
    return base_url.rstrip("/"), token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args()

    base_url, token = get_config(args.env_file)
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    who = session.get(f"{base_url}/api/v1/users/self", timeout=30)
    if who.status_code == 401:
        sys.exit(f"401 from Canvas -- token is invalid, revoked, or expired.\n{who.text}")
    who.raise_for_status()
    user = who.json()
    print(f"Authenticated as {user.get('name')} (id {user.get('id')}, {user.get('login_id')})")

    # Canvas reports the token's own metadata under the "self" alias.
    info = session.get(f"{base_url}/api/v1/users/self/tokens/self", timeout=30)
    if info.ok:
        meta = info.json()
        print(f"Token purpose: {meta.get('purpose')!r}  expires: {meta.get('expires_at')}")
        print(f"Scopes: {meta.get('scopes') or 'unscoped (full user access)'}")
    else:
        print(f"Token metadata unavailable (HTTP {info.status_code}) -- not fatal.")

    courses = session.get(
        f"{base_url}/api/v1/courses",
        params={"enrollment_state": "active", "per_page": 100, "include[]": "term"},
        timeout=30,
    )
    courses.raise_for_status()
    rows = courses.json()
    print(f"\nActive courses visible to this token: {len(rows)}")
    for course in rows:
        term = (course.get("term") or {}).get("name", "?")
        print(f"  {course['id']:>8}  {term:<12}  {course.get('course_code')} -- {course.get('name')}")

    remaining = courses.headers.get("X-Rate-Limit-Remaining")
    if remaining:
        print(f"\nRate-limit bucket remaining: {remaining} (starts at 700, refills ~1/sec)")


if __name__ == "__main__":
    main()
