"""Durable single-worker synchronization; never retries bank authentication automatically."""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from open_fin.config import Settings
from open_fin.connectors.demo import DemoConnector
from open_fin.models import ConnectorError, Credentials, Snapshot
from open_fin.processes import stop_group
from open_fin.storage import Store

ERROR_MESSAGES = {
    "connector_failed": "The bank connection failed. Check the connection and try again later.",
    "authentication_failed": "The bank did not accept the login.",
    "authentication_required": "The bank requires interactive approval or authentication.",
    "timeout": "The bank connection exceeded its time limit.",
    "invalid_snapshot": "The bank returned inconsistent data. The previous snapshot was preserved.",
    "extraction_failed": "The bank portal could not be read. The previous snapshot was preserved.",
    "ambiguous_product": "The bank product could not be identified safely.",
    "demo_credentials_required": "Demo mode requires the documented synthetic credentials.",
    "mode_mismatch": (
        "This connection belongs to a different deployment mode. Create a new connection."
    ),
    "process_control_unavailable": "This host cannot safely supervise bank browser processes.",
}
for _code in {
    "ambiguous_currency_control",
    "ambiguous_movement_amount",
    "ambiguous_movement_table",
    "ambiguous_product_currency",
    "ambiguous_product_identity",
    "bank_data_request_failed",
    "inconsistent_card_products",
    "invalid_amount",
    "invalid_date",
    "invalid_movement_description",
    "missing_account_products",
    "missing_card_product",
    "missing_card_products",
    "missing_currency_controls",
    "missing_login_frame",
    "missing_login_fields",
    "missing_movement_column",
    "missing_movement_rows",
    "missing_movement_table",
    "untrusted_bank_origin",
}:
    ERROR_MESSAGES[_code] = ERROR_MESSAGES["extraction_failed"]
for _code in {"bank_data_response_timeout", "bank_page_timeout", "connector_timeout"}:
    ERROR_MESSAGES[_code] = ERROR_MESSAGES["timeout"]
ERROR_MESSAGES["authentication_or_challenge_failed"] = ERROR_MESSAGES["authentication_required"]


def fetch_live(
    settings: Settings, credentials: Credentials, lock_descriptor: int | None = None
) -> Snapshot:
    payload = {
        "credentials": {
            "rut": credentials.rut.get_secret_value(),
            "password": credentials.password.get_secret_value(),
        },
        "headless": settings.browser_headless,
        "timeout_seconds": settings.job_timeout_seconds,
    }
    # The browser process has no reason to inherit API tokens or the vault encryption key.
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "DISPLAY",
            "PLAYWRIGHT_BROWSERS_PATH",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "TMPDIR",
        }
    }
    with subprocess.Popen(
        [sys.executable, "-m", "open_fin.connector_process"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        env=allowed_environment,
        pass_fds=() if lock_descriptor is None else (lock_descriptor,),
    ) as process:
        try:
            stdout, _ = process.communicate(
                json.dumps(payload), timeout=settings.job_timeout_seconds
            )
        except subprocess.TimeoutExpired as error:
            _kill_group(process.pid)
            process.communicate()
            raise ConnectorError("timeout") from error
        except BaseException:
            _kill_group(process.pid)
            process.communicate()
            raise
        if process.returncode:
            try:
                code = json.loads(stdout)["error_code"]
            except (ValueError, KeyError, TypeError):
                code = "connector_failed"
            raise ConnectorError(code if code in ERROR_MESSAGES else "connector_failed")
        try:
            return Snapshot.model_validate_json(stdout)
        except ValueError as error:
            raise ConnectorError("invalid_snapshot") from error


def _kill_group(pid: int) -> None:
    stop_group(pid)


def run_once(settings: Settings, connector=None) -> bool:
    db = Store(settings.database_path, settings.encryption_key)
    lock_path = Path(settings.database_path).with_suffix(".worker.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        db.recover_interrupted()
        job = db.claim()
        if job is None:
            return False
        try:
            if db.connection(job["connection_id"])["mode"] != settings.connector_mode:
                raise ConnectorError("mode_mismatch")
            credentials = db.credentials(job["connection_id"])
            if connector is not None:
                snapshot = connector.fetch(credentials)
            elif settings.connector_mode == "demo":
                snapshot = DemoConnector().fetch(credentials)
            else:
                snapshot = fetch_live(settings, credentials, lock_descriptor=lock.fileno())
            db.ingest(job["id"], Snapshot.model_validate(snapshot))
        except ConnectorError as error:
            code = error.code if error.code in ERROR_MESSAGES else "connector_failed"
            db.fail(job["id"], code, ERROR_MESSAGES[code])
        except Exception:
            db.fail(job["id"], "connector_failed", ERROR_MESSAGES["connector_failed"])
        return True


def main() -> None:
    os.umask(0o077)
    settings = Settings.from_env()
    while True:
        if not run_once(settings):
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
