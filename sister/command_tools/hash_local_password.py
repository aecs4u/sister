#!/usr/bin/env python3
"""Print a hashed ``local_users.txt`` entry.

Usage: uv run sister app hash-password <username>   (prompts for the password)
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sister.passwords import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords differ", file=sys.stderr)
        return 1
    print(f"{sys.argv[1]}    {hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
