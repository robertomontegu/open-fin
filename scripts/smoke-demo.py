#!/usr/bin/env python3
"""Exercise the running demo over HTTP without printing deployment secrets."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", help="Check existing data after restarting the services")
    args = parser.parse_args()
    local = {}
    if Path(".env").is_file():
        for line in Path(".env").read_text().splitlines():
            if line.startswith("OPEN_FIN_") and "=" in line:
                key, value = line.split("=", 1)
                local[key] = value
    token = os.getenv("OPEN_FIN_API_TOKEN", local.get("OPEN_FIN_API_TOKEN", ""))
    port = int(os.getenv("OPEN_FIN_PORT", local.get("OPEN_FIN_PORT", "8088")))
    base = f"http://127.0.0.1:{port}"

    def request(path: str, method: str = "GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    if request("/health")["mode"] != "demo":
        print("Refusing demo smoke against a live deployment.", file=sys.stderr)
        return 1
    connection_id = args.connection_id
    if not connection_id:
        connection_id = request(
            "/v1/connections",
            "POST",
            {
                "bank": "santander",
                "label": "Synthetic HTTP smoke",
                "credentials": {"rut": "11111111-1", "password": "demo"},
            },
        )["id"]
        job_id = request(f"/v1/connections/{connection_id}/sync", "POST")["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = request(f"/v1/syncs/{job_id}")
            if job["status"] == "succeeded":
                break
            if job["status"] == "failed":
                raise RuntimeError("Synthetic synchronization failed: " + job["error_code"])
            time.sleep(0.25)
        else:
            raise RuntimeError("Synthetic worker did not finish within 30 seconds")
    accounts = request(f"/v1/accounts?connection_id={connection_id}")
    cards = request(f"/v1/cards?connection_id={connection_id}")
    movements = request(f"/v1/movements?connection_id={connection_id}")
    if accounts["total"] != 2 or cards["total"] != 2 or movements["total"] != 4:
        raise RuntimeError("Unexpected synthetic product or movement counts")
    if not any(
        row["amount"] == "-14.82" and row["currency"] == "USD" for row in movements["items"]
    ):
        raise RuntimeError("Decimal cents were not preserved")
    print(
        json.dumps(
            {
                "result": "passed",
                "connection_id": connection_id,
                "accounts": accounts["total"],
                "cards": cards["total"],
                "movements": movements["total"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        # HTTPError's default repr can include URLs; no request bodies/headers are printed.
        print("Demo smoke failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
