#!/usr/bin/env python3
"""Create deployment secrets locally; never print them or replace an existing file."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    token = secrets.token_urlsafe(48)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    content = (
        "# Generated locally. Keep private and back up the key with the database.\n"
        f"OPEN_FIN_API_TOKEN={token}\n"
        f"OPEN_FIN_ENCRYPTION_KEY={key}\n"
        "OPEN_FIN_DATABASE_PATH=/data/open-fin.db\n"
        "OPEN_FIN_MODE=demo\n"
        "OPEN_FIN_COOLDOWN_SECONDS=3600\n"
        "OPEN_FIN_JOB_TIMEOUT_SECONDS=240\n"
        "OPEN_FIN_HEADLESS=true\n"
        "OPEN_FIN_PORT=8088\n"
        "OPEN_FIN_SOURCE_URL=https://github.com/robertomontegu/open-fin\n"
    )
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        print("Refusing to overwrite an existing environment file.", file=sys.stderr)
        return 1
    except OSError:
        print(
            "Could not create environment file; check its directory and permissions.",
            file=sys.stderr,
        )
        return 1
    print("Created private environment file. Secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
