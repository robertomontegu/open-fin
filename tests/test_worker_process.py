"""Synthetic subprocess boundaries; these tests never create a banking browser."""

import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace

import psutil
import pytest

from open_fin import connector_process, processes, worker
from open_fin.models import ConnectorError, CreateConnection, Credentials, Snapshot
from open_fin.storage import Store


@pytest.fixture
def credentials():
    return Credentials(rut="11111111-1", password="synthetic-worker-password")


@pytest.fixture
def process_stub(monkeypatch):
    captured = {"kills": []}

    class Process:
        pid = 1234567
        returncode = 0

        def __init__(self, command, **options):
            captured["command"] = command
            captured["options"] = options
            self.calls = 0
            self.output = Snapshot(products=[], movements=[]).model_dump_json()
            self.error = None
            captured["process"] = self

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            return False

        def communicate(self, data=None, timeout=None):
            self.calls += 1
            if data is not None:
                captured["input"] = json.loads(data)
                captured["timeout"] = timeout
            if self.calls == 1 and self.error is not None:
                raise self.error
            return self.output, None

    def configure(*, output=None, returncode=0, error=None):
        def factory(command, **options):
            process = Process(command, **options)
            process.returncode = returncode
            process.error = error
            if output is not None:
                process.output = output
            return process

        monkeypatch.setattr(worker.subprocess, "Popen", factory)
        monkeypatch.setattr(
            worker.os, "killpg", lambda pid, action: captured["kills"].append((pid, action))
        )
        return captured

    return configure


@pytest.mark.spec("OF-AC-001")
def test_live_child_receives_bank_credentials_only_over_stdin(
    settings, credentials, process_stub, monkeypatch, tmp_path
):
    monkeypatch.setenv("OPEN_FIN_API_TOKEN", settings.api_token)
    monkeypatch.setenv("OPEN_FIN_ENCRYPTION_KEY", settings.encryption_key)
    monkeypatch.setenv("UNRELATED_PRIVATE_SECRET", "synthetic-unrelated-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-private-proxy.invalid")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/synthetic-browsers")
    captured = process_stub()
    descriptor = os.open(tmp_path / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        snapshot = worker.fetch_live(settings, credentials, lock_descriptor=descriptor)
    finally:
        os.close(descriptor)
    assert snapshot.products == snapshot.movements == []
    assert captured["command"] == [sys.executable, "-m", "open_fin.connector_process"]
    assert captured["options"]["pass_fds"] == (descriptor,)
    assert captured["options"]["start_new_session"] is True
    assert captured["options"]["stderr"] == subprocess.DEVNULL
    environment = captured["options"]["env"]
    assert not {
        "OPEN_FIN_API_TOKEN",
        "OPEN_FIN_ENCRYPTION_KEY",
        "UNRELATED_PRIVATE_SECRET",
        "HTTPS_PROXY",
    }.intersection(environment)
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "/synthetic-browsers"
    assert captured["input"]["credentials"] == {
        "rut": "11111111-1",
        "password": "synthetic-worker-password",
    }
    assert captured["timeout"] == settings.job_timeout_seconds
    arguments_and_environment = json.dumps([captured["command"], environment])
    assert credentials.password.get_secret_value() not in arguments_and_environment
    assert settings.api_token not in arguments_and_environment
    assert settings.encryption_key not in arguments_and_environment


@pytest.mark.spec("OF-AC-005")
def test_timeout_terminates_child_group_and_drains_the_process(settings, credentials, process_stub):
    captured = process_stub(error=subprocess.TimeoutExpired("synthetic-child", 1))
    with pytest.raises(ConnectorError, match="^timeout$"):
        worker.fetch_live(settings, credentials)
    assert captured["kills"] == [(1234567, signal.SIGKILL)]
    assert captured["process"].calls == 2


@pytest.mark.spec("OF-AC-005")
def test_interruption_terminates_child_before_reraising(settings, credentials, process_stub):
    captured = process_stub(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        worker.fetch_live(settings, credentials)
    assert captured["kills"] == [(1234567, signal.SIGKILL)]
    assert captured["process"].calls == 2


@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"error_code":"authentication_required"}', "authentication_required"),
        ('{"error_code":"synthetic-worker-password"}', "connector_failed"),
        ('{"unexpected":"synthetic-worker-password"}', "connector_failed"),
        ("synthetic-worker-password raw traceback", "connector_failed"),
    ],
)
def test_child_failure_only_returns_known_safe_codes(
    settings, credentials, process_stub, output, expected, capsys
):
    process_stub(output=output, returncode=1)
    with pytest.raises(ConnectorError) as failure:
        worker.fetch_live(settings, credentials)
    assert failure.value.code == expected
    captured = capsys.readouterr()
    assert "synthetic-worker-password" not in str(failure.value)
    assert captured.out == captured.err == ""


@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize("output", ["not-json", '{"products":[],"movements":[{}]}'])
def test_success_exit_with_invalid_snapshot_fails_safely(
    settings, credentials, process_stub, output
):
    process_stub(output=output)
    with pytest.raises(ConnectorError, match="^invalid_snapshot$"):
        worker.fetch_live(settings, credentials)


@pytest.mark.spec("OF-AC-001")
def test_adapter_entrypoint_never_prints_raw_exception_or_credentials(monkeypatch, capsys):
    class FailingAdapter:
        def __init__(self, **_options):
            pass

        def fetch(self, _credentials):
            raise RuntimeError("synthetic-worker-password private bank HTML")

    monkeypatch.setattr(connector_process, "SantanderConnector", FailingAdapter)
    monkeypatch.setattr(connector_process, "ensure_process_control", lambda: None)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "headless": True,
                    "timeout_seconds": 30,
                    "credentials": {"rut": "11111111-1", "password": "synthetic-worker-password"},
                }
            )
        ),
    )
    with pytest.raises(SystemExit) as failure:
        connector_process.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error_code": "connector_failed"}
    assert captured.err == ""
    assert "synthetic-worker-password" not in captured.out


@pytest.mark.spec("OF-AC-001")
@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize(
    "stage,expected_stage",
    [
        ("starting", "starting"),
        ("login_page", "login_page"),
        ("login_form", "login_form"),
        ("login_submit", "login_submit"),
        ("accounts", "accounts"),
        ("card_unbilled", "card_unbilled"),
        ("card_billed", "card_billed"),
        (None, None),
        ("synthetic-worker-password private bank HTML", None),
        (["synthetic-worker-password"], None),
        ({"private": "synthetic-worker-password"}, None),
    ],
)
def test_diagnostic_stage_survives_child_and_worker_only_when_allowlisted(
    settings, credentials, process_stub, monkeypatch, capsys, stage, expected_stage
):
    class FailingAdapter:
        def __init__(self, **_options):
            pass

        def fetch(self, _credentials):
            raise ConnectorError("missing_movement_table", stage=stage)

    monkeypatch.setattr(connector_process, "SantanderConnector", FailingAdapter)
    monkeypatch.setattr(connector_process, "ensure_process_control", lambda: None)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "headless": True,
                    "timeout_seconds": 30,
                    "credentials": {
                        "rut": credentials.rut.get_secret_value(),
                        "password": credentials.password.get_secret_value(),
                    },
                }
            )
        ),
    )
    with pytest.raises(SystemExit) as failure:
        connector_process.main()
    assert failure.value.code == 1
    child_output = capsys.readouterr()
    expected_payload = {"error_code": "missing_movement_table"}
    if expected_stage is not None:
        expected_payload["stage"] = expected_stage
    assert json.loads(child_output.out) == expected_payload
    assert child_output.err == ""
    assert credentials.password.get_secret_value() not in child_output.out
    assert credentials.rut.get_secret_value() not in child_output.out

    process_stub(output=child_output.out, returncode=1)
    live_settings = replace(settings, connector_mode="live")
    db = Store(live_settings.database_path, live_settings.encryption_key)
    connection = db.create_connection(CreateConnection(credentials=credentials), mode="live")
    job = db.enqueue(connection["id"], cooldown_seconds=0)
    assert worker.run_once(live_settings) is True
    failed_job = db.sync(job["id"])
    assert failed_job["status"] == "failed"
    assert failed_job["error_code"] == "missing_movement_table"
    expected_message = worker.ERROR_MESSAGES["missing_movement_table"]
    if expected_stage is not None:
        expected_message += " Stage: " + expected_stage + "."
    assert failed_job["error_message"] == expected_message
    assert credentials.password.get_secret_value() not in json.dumps(failed_job)
    assert credentials.rut.get_secret_value() not in json.dumps(failed_job)
    assert capsys.readouterr().out == ""


@pytest.mark.spec("OF-AC-001")
@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize(
    "stage",
    [
        "synthetic-worker-password private bank HTML",
        ["synthetic-worker-password"],
        {"private": "synthetic-worker-password"},
        1234,
        None,
    ],
)
def test_worker_independently_discards_untrusted_child_stage(
    settings, credentials, process_stub, capsys, stage
):
    process_stub(
        output=json.dumps({"error_code": "authentication_required", "stage": stage}),
        returncode=1,
    )
    with pytest.raises(ConnectorError) as failure:
        worker.fetch_live(settings, credentials)
    assert failure.value.code == "authentication_required"
    assert failure.value.stage is None
    assert str(failure.value) == "authentication_required"
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.spec("OF-AC-001")
@pytest.mark.parametrize(
    "inspection_error",
    [PermissionError("synthetic-worker-password denied"), psutil.AccessDenied(pid=1234567)],
)
def test_blocked_process_control_rejects_before_adapter_or_watchdog(
    inspection_error, monkeypatch, capsys
):
    started = []

    class UninspectableProcess:
        def children(self, **_options):
            raise inspection_error

    def forbidden_adapter(**_options):
        started.append("adapter")
        raise AssertionError("No adapter may start without process control")

    def forbidden_watchdog(*_arguments):
        started.append("watchdog")
        raise AssertionError("No watchdog may start before the process-control preflight")

    monkeypatch.setattr(processes.psutil, "Process", UninspectableProcess)
    monkeypatch.setattr(connector_process, "SantanderConnector", forbidden_adapter)
    monkeypatch.setattr(connector_process.threading, "Timer", forbidden_watchdog)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "headless": True,
                    "timeout_seconds": 30,
                    "credentials": {"rut": "11111111-1", "password": "synthetic-worker-password"},
                }
            )
        ),
    )
    with pytest.raises(SystemExit) as failure:
        connector_process.main()
    assert failure.value.code == 1
    assert started == []
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error_code": "process_control_unavailable"}
    assert captured.err == ""
    assert "synthetic-worker-password" not in captured.out
    assert "11111111-1" not in captured.out


def _denied_descendant_inspection(_pid):
    raise PermissionError("Synthetic process-inspection denial")


@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize("session_leader", [True, False])
def test_watchdog_still_terminates_adapter_when_descendant_inspection_fails(
    monkeypatch, session_leader
):
    signals = []
    pid = os.getpid()
    monkeypatch.setattr(connector_process, "stop_descendants", _denied_descendant_inspection)
    monkeypatch.setattr(connector_process.os, "getpgrp", lambda: pid if session_leader else -1)
    monkeypatch.setattr(
        connector_process.os,
        "killpg",
        lambda target, action: signals.append(("group", target, action)),
    )
    monkeypatch.setattr(
        connector_process.os,
        "kill",
        lambda target, action: signals.append(("process", target, action)),
    )
    with pytest.raises(PermissionError):
        connector_process.terminate_group()
    assert signals == [("group" if session_leader else "process", pid, signal.SIGKILL)]


@pytest.mark.spec("OF-AC-005")
def test_parent_cleanup_still_terminates_group_when_descendant_inspection_fails(monkeypatch):
    signals = []
    monkeypatch.setattr(processes, "stop_descendants", _denied_descendant_inspection)
    monkeypatch.setattr(processes.os, "killpg", lambda pid, action: signals.append((pid, action)))
    with pytest.raises(PermissionError):
        processes.stop_group(1234567)
    assert signals == [(1234567, signal.SIGKILL)]


_SYNTHETIC_CHILD = r"""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from open_fin import connector_process

class SyntheticAdapter:
    def __init__(self, **options):
        pass

    def fetch(self, credentials):
        browser = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(sys.argv[1]).write_text(json.dumps({'adapter': os.getpid(), 'browser': browser.pid}))
        time.sleep(60)

payload = json.loads(sys.stdin.read())
payload['timeout_seconds'] = 1
sys.stdin = io.StringIO(json.dumps(payload))
connector_process.SantanderConnector = SyntheticAdapter
real_terminate = connector_process.terminate_group
def report_watchdog_error():
    try:
        real_terminate()
    except BaseException as error:
        Path(sys.argv[1] + '.watchdog-error').write_text(type(error).__name__ + ': ' + str(error))
        raise
connector_process.terminate_group = report_watchdog_error
connector_process.main()
"""

_SYNTHETIC_PARENT = r"""
import fcntl
import os
import sys
from cryptography.fernet import Fernet
from open_fin import worker
from open_fin.config import Settings
from open_fin.models import Credentials

real_popen = worker.subprocess.Popen
def synthetic_popen(command, **options):
    return real_popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], **options)

worker.subprocess.Popen = synthetic_popen
descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
settings = Settings(
    database_path=sys.argv[1] + '.sqlite',
    encryption_key=Fernet.generate_key().decode(),
    api_token='synthetic-parent-token-with-more-than-32-characters',
    connector_mode='live',
    job_timeout_seconds=30,
)
worker.fetch_live(
    settings,
    Credentials(rut='11111111-1', password='synthetic-worker-password'),
    lock_descriptor=descriptor,
)
"""


def _is_executing(pid):
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.spec("OF-AC-005")
def test_orphan_watchdog_holds_lock_until_detached_descendants_stop(tmp_path):
    """SIGKILL a synthetic worker; its child owns a detached synthetic browser."""
    try:
        psutil.Process().children()
    except (PermissionError, psutil.AccessDenied):
        pytest.skip("Host sandbox blocks process inspection; run in the Linux test container")
    marker = tmp_path / "child-ready.json"
    lock_path = tmp_path / "worker.lock"
    parent = subprocess.Popen(
        [sys.executable, "-c", _SYNTHETIC_PARENT, str(lock_path), str(marker), _SYNTHETIC_CHILD],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pids = {}
    descriptor = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "Synthetic adapter did not start"
        pids = json.loads(marker.read_text())
        assert os.getpgid(pids["browser"]) == pids["browser"]
        assert pids["browser"] != pids["adapter"]
        parent.kill()
        parent.wait(timeout=3)
        descriptor = os.open(lock_path, os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # The real child watchdog has a 1-second timeout plus a 5-second cleanup margin.
        deadline = time.monotonic() + 12
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                error_marker = marker.with_name(marker.name + ".watchdog-error")
                detail = error_marker.read_text() if error_marker.exists() else "no watchdog error"
                assert time.monotonic() < deadline, f"Orphan retained worker lock: {detail}"
                time.sleep(0.02)
        assert not _is_executing(pids["browser"]), "Detached browser survived lock release"
        assert not _is_executing(pids["adapter"]), "Adapter survived watchdog cleanup"
        assert unrelated.poll() is None, "Cleanup terminated an unrelated process"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for process in (parent, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        for pid in pids.values():
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
