"""Short-lived live adapter process: secrets enter only over stdin."""

import json
import os
import signal
import sys
import threading

from open_fin.connectors.santander import SantanderConnector
from open_fin.models import ConnectorError, Credentials
from open_fin.processes import ensure_process_control, stop_descendants


def terminate_group() -> None:
    """An orphaned adapter must release its inherited worker lock after a finite time."""
    try:
        stop_descendants(os.getpid())
    finally:
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
        else:
            os.kill(os.getpid(), signal.SIGKILL)


def main() -> None:
    watchdog = None
    try:
        payload = json.loads(sys.stdin.read())
        ensure_process_control()
        timeout = max(1, min(900, int(payload["timeout_seconds"])))
        watchdog = threading.Timer(timeout + 5, terminate_group)
        watchdog.daemon = True
        watchdog.start()
        connector = SantanderConnector(
            headless=payload["headless"], timeout_seconds=payload["timeout_seconds"]
        )
        snapshot = connector.fetch(Credentials.model_validate(payload["credentials"]))
        print(snapshot.model_dump_json())
    except ConnectorError as error:
        failure = {"error_code": error.code}
        if error.stage is not None:
            failure["stage"] = error.stage
        print(json.dumps(failure))
        sys.exit(1)
    except Exception:
        # Raw exceptions can contain RUTs, bank HTML, paths or Playwright call arguments.
        print(json.dumps({"error_code": "connector_failed"}))
        sys.exit(1)
    finally:
        if watchdog is not None:
            watchdog.cancel()


if __name__ == "__main__":
    main()
