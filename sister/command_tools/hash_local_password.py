#!/usr/bin/env python3
"""Print a hashed ``local_users.txt`` entry.

Usage: uv run sister app hash-password <username>   (prompts for the password)
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sister.passwords import hash_password  # noqa: E402


def run(username: str, password: str, repeated_password: str) -> tuple[int, str | None]:
    """Return the rendered local user entry, or a non-zero code for a mismatch."""
    if password != repeated_password:
        return 1, None
    return 0, f"{username}    {hash_password(password)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("username", help="Username to include in the local user entry")
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    status, result = run(args.username, password, getpass.getpass("Repeat: "))
    if status:
        print("Passwords differ", file=sys.stderr)
        return status
    print(result)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
