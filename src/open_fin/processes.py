"""Terminate adapter descendants even when Chromium starts a separate session."""

import os
import signal

import psutil

from open_fin.models import ConnectorError


def ensure_process_control() -> None:
    """Reject unsupported hosts before a bank browser can be started."""
    try:
        psutil.Process().children(recursive=True)
    except (PermissionError, psutil.AccessDenied):
        raise ConnectorError("process_control_unavailable") from None


def stop_descendants(pid: int) -> None:
    """Keep the adapter (and its inherited lock) alive until browsers have exited."""
    try:
        parent = psutil.Process(pid)
        descendants = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    # Process instances remember creation time, guarding against PID reuse when signaling.
    for process in reversed(descendants):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(descendants, timeout=2)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=2)


def stop_group(pid: int) -> None:
    try:
        stop_descendants(pid)
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
