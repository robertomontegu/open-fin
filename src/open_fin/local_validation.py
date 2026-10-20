"""Private, interactive live validation. This operator tool supports system Python 3.9+."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from open_fin.validation_report import analyse_snapshot, render_report

SAFE_STAGES = {
    "starting",
    "login_page",
    "login_form",
    "login_submit",
    "accounts",
    "card_unbilled",
    "card_billed",
}
SAFE_BANK_CODES = {
    "bank_access_denied",
    "bank_http_error",
    "connector_failed",
    "authentication_failed",
    "authentication_required",
    "timeout",
    "invalid_snapshot",
    "extraction_failed",
    "ambiguous_product",
    "mode_mismatch",
    "process_control_unavailable",
    "worker_interrupted",
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
    "missing_login_entry",
    "ambiguous_login_entry",
    "missing_login_fields",
    "missing_movement_column",
    "missing_movement_rows",
    "missing_movement_table",
    "untrusted_bank_origin",
    "bank_data_response_timeout",
    "bank_page_timeout",
    "connector_timeout",
    "authentication_or_challenge_failed",
}
COUNT_KEYS = {
    "accounts",
    "cards",
    "card_currency_products",
    "movements",
    "movements_account",
    "movements_billed",
    "movements_unbilled",
}
REVIEW_KEYS = ("products", "movements", "statement_sources")
MESSAGES = {
    "live_required": "La API debe estar en modo real antes de ingresar credenciales.",
    "terminal_required": "Abre este asistente en Terminal; la entrada debe permanecer oculta.",
    "outcome_unknown": (
        "Una solicitud quedó sin respuesta confirmada. No se repetirá automáticamente. "
        "Revisa el estado de la instancia antes de volver a intentar."
    ),
    "authentication_or_challenge_failed": (
        "El ingreso no se completó. Revisa la autenticación o aprobación en tu app Santander."
    ),
    "cooldown": "La conexión debe esperar antes de otro intento. No se enviará otro ingreso.",
    "sync_in_progress": "Ya existe una sincronización activa para esta conexión.",
    "poll_timeout": "El asistente dejó de esperar; al abrirlo otra vez retomará la misma tarea.",
    "unsafe_file": "Se rechazó un archivo o carpeta local con permisos o enlaces no seguros.",
    "wizard_busy": "Ya hay un asistente de validación abierto.",
    "compose_failed": "No se pudieron preparar los servicios locales. Revisa Docker.",
    "service_unavailable": "La API local no respondió. Comprueba que Docker esté funcionando.",
    "origin_mismatch": "Este intento corresponde a otro puerto local. Restaura su configuración.",
    "bank_access_denied": (
        "El portal bancario denegó el acceso desde esta instancia. "
        "No se repetirá el ingreso automáticamente."
    ),
    "bank_http_error": "El portal bancario devolvió un error HTTP. No se reintentará el ingreso.",
}


class FlowError(Exception):
    def __init__(self, code: str, *, retry_after: int | None = None, stage: str | None = None):
        self.code = code
        self.retry_after = retry_after
        self.stage = stage if isinstance(stage, str) and stage in SAFE_STAGES else None
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveClient:
    def __init__(self, port: int, token: str, *, timeout: float = 10):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise FlowError("invalid_configuration")
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise FlowError("invalid_configuration")
        if any(character in token for character in "\r\n"):
            raise FlowError("invalid_configuration")
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise FlowError("invalid_path")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise FlowError("invalid_response")
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise FlowError("redirect_refused") from None
            code = {
                401: "api_authentication_failed",
                404: "not_found",
                409: "sync_in_progress",
                422: "invalid_request",
                429: "cooldown",
            }.get(error.code, "http_error")
            retry_after = error.headers.get("Retry-After", "")
            raise FlowError(
                code, retry_after=int(retry_after) if retry_after.isdigit() else None
            ) from None
        except (OSError, urllib.error.URLError):
            raise FlowError("service_unavailable") from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise FlowError("invalid_response") from None
        if not isinstance(result, dict):
            raise FlowError("invalid_response")
        return result

    def all_pages(self, resource: str, connection_id: str) -> list[dict]:
        if resource not in {"accounts", "cards", "movements"}:
            raise FlowError("invalid_path")
        rows, expected_total, offset = [], None, 0
        while True:
            query = urllib.parse.urlencode(
                {"connection_id": connection_id, "limit": 200, "offset": offset}
            )
            page = self.request("/v1/" + resource + "?" + query)
            items, total = page.get("items"), page.get("total")
            if (
                not isinstance(items, list)
                or any(not isinstance(item, dict) for item in items)
                or isinstance(total, bool)
                or not isinstance(total, int)
                or not 0 <= total <= 20000
                or page.get("offset") != offset
                or page.get("limit") != 200
                or len(items) > 200
                or len(rows) + len(items) > total
                or (expected_total is not None and total != expected_total)
            ):
                raise FlowError("inconsistent_pagination")
            expected_total = total
            rows.extend(items)
            if len(rows) == total:
                return rows
            if not items:
                raise FlowError("inconsistent_pagination")
            offset += len(items)


def ensure_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise FlowError("terminal_required")


def secret_input(prompt: str) -> str:
    ensure_terminal()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError):
        raise FlowError("terminal_required") from None
    if not value.strip() or len(value) > 256:
        raise FlowError("invalid_credentials_input")
    return value


def _check_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise FlowError("unsafe_file")


def private_directory(path: Path) -> None:
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise FlowError("unsafe_file")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise FlowError("unsafe_file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise FlowError("unsafe_file")


def write_private(path: Path, data: str) -> None:
    path = Path(path)
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".open-fin-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private_json(path: Path) -> dict:
    _check_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            result = json.load(handle)
    except (ValueError, UnicodeError):
        raise FlowError("invalid_state") from None
    if not isinstance(result, dict):
        raise FlowError("invalid_state")
    return result


@contextmanager
def wizard_lock(directory: Path):
    private_directory(directory)
    path = directory / "wizard.lock"
    if path.exists() or path.is_symlink():
        _check_file(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FlowError("wizard_busy") from None
        yield


def _status(directory: Path, phase: str, *, counts=None, error=None, review=None) -> dict:
    # This is the only file an agent should inspect after real credentials are entered.
    result = {"phase": phase}
    if counts is not None:
        result["counts"] = {key: int(counts.get(key, 0)) for key in sorted(COUNT_KEYS)}
    if error is not None:
        result["error_code"] = error.code
        if error.stage in SAFE_STAGES:
            result["stage"] = error.stage
        if error.retry_after is not None:
            result["retry_after"] = error.retry_after
    if review is not None:
        result["review"] = {key: review[key] for key in REVIEW_KEYS if key in review}
    write_private(directory / "status.json", json.dumps(result, indent=2))
    return result


def _safe_id(value, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[0-9a-f]{32}", value):
        raise FlowError("invalid_response")
    return value


def _review_answer(value: str) -> str:
    value = value.strip().lower()
    if value in {"s", "si", "sí", "yes", "y"}:
        return "yes"
    if value in {"n", "no"}:
        return "no"
    return "unsure"


def run_validation(
    client,
    directory: Path,
    *,
    prompt_secret=secret_input,
    prompt_review=input,
    show_report=webbrowser.open,
    emit=print,
    sleep=time.sleep,
    clock=time.monotonic,
    timeout_seconds: float = 660,
) -> dict:
    directory = Path(directory)
    private_directory(directory)
    try:
        health = client.request("/health")
        if health.get("mode") != "live" or health.get("status") != "ok":
            raise FlowError("live_required")
        # Authenticate before requesting a bank secret; health itself is intentionally public.
        client.request("/v1/connections")
        state_path = directory / "state.json"
        state = read_private_json(state_path) if state_path.exists() else {}
        if state and state.get("api_origin") != client.base:
            raise FlowError("origin_mismatch")
        state["api_origin"] = client.base
        if state.get("submission_in_flight"):
            raise FlowError("outcome_unknown")
        if not state.get("connection_id"):
            _status(directory, "awaiting_credentials")
            emit(
                "RUT y clave se ingresan con entrada oculta; se guardarán cifrados en tu instancia."
            )
            rut = prompt_secret("RUT Santander (oculto): ")
            password = prompt_secret("Clave Santander (oculta): ")
            if not rut.strip() or not password.strip() or max(len(rut), len(password)) > 256:
                raise FlowError("invalid_credentials_input")
            state["submission_in_flight"] = "connection"
            write_private(state_path, json.dumps(state))
            try:
                connection = client.request(
                    "/v1/connections",
                    "POST",
                    {
                        "bank": "santander",
                        "label": "Validación local Santander",
                        "credentials": {"rut": rut, "password": password},
                    },
                )
            except Exception:
                raise FlowError("outcome_unknown") from None
            finally:
                rut = password = ""
            state["connection_id"] = _safe_id(connection.get("id"), "con")
            state.pop("submission_in_flight")
            write_private(state_path, json.dumps(state))
        connection_id = _safe_id(state["connection_id"], "con")
        connection = client.request("/v1/connections/" + connection_id)
        if connection.get("mode") != "live" or connection.get("bank") != "santander":
            raise FlowError("live_required")
        if not state.get("sync_id"):
            state["submission_in_flight"] = "sync"
            write_private(state_path, json.dumps(state))
            try:
                job = client.request("/v1/connections/" + connection_id + "/sync", "POST")
            except FlowError as error:
                if error.code in {"cooldown", "sync_in_progress"}:
                    state.pop("submission_in_flight")
                    write_private(state_path, json.dumps(state))
                    raise
                raise FlowError("outcome_unknown") from None
            except Exception:
                raise FlowError("outcome_unknown") from None
            state["sync_id"] = _safe_id(job.get("id"), "syn")
            state.pop("submission_in_flight")
            write_private(state_path, json.dumps(state))
        sync_id = _safe_id(state["sync_id"], "syn")
        emit("Sincronizando una sola vez. Ten tu app Santander a mano por si pide aprobación.")
        _status(directory, "syncing")
        deadline = clock() + timeout_seconds
        while True:
            job = client.request("/v1/syncs/" + sync_id)
            if job.get("connection_id") != connection_id:
                raise FlowError("invalid_response")
            if job.get("status") == "succeeded":
                break
            if job.get("status") == "failed":
                code = job.get("error_code")
                code = (
                    code
                    if isinstance(code, str) and code in SAFE_BANK_CODES
                    else "connector_failed"
                )
                match = re.search(r" Stage: ([a-z_]+)\.$", str(job.get("error_message", "")))
                raise FlowError(code, stage=match[1] if match else None)
            if job.get("status") not in {"queued", "running"}:
                raise FlowError("invalid_response")
            if clock() >= deadline:
                raise FlowError("poll_timeout")
            sleep(2)
        result = analyse_snapshot(
            job,
            client.all_pages("accounts", connection_id),
            client.all_pages("cards", connection_id),
            client.all_pages("movements", connection_id),
            connection_id=connection_id,
        )
        report_path = directory / "report.html"
        write_private(report_path, render_report(result))
        _status(directory, "needs_review", counts=result.counts)
        emit("Extracción terminada. Revisa el informe privado en tu navegador.")
        emit("Archivo local: data/validation/santander/report.html")
        show_report(report_path.absolute().as_uri())
        review = {}
        for key, question in [
            ("products", "¿Coinciden todas tus cuentas, tarjetas y monedas? [s/n/no sé]: "),
            (
                "movements",
                "¿Coinciden fechas, signos y montos de hasta 5 movimientos? [s/n/no sé]: ",
            ),
            (
                "statement_sources",
                "¿Coinciden facturados y por facturar donde corresponda? [s/n/no sé]: ",
            ),
        ]:
            review[key] = _review_answer(prompt_review(question))
        state["review"] = review
        write_private(state_path, json.dumps(state))
        write_private(report_path, render_report(result, review))
        phase = (
            "discrepancy"
            if "no" in review.values() or not result.structural_ok
            else "verified_by_owner"
            if all(value == "yes" for value in review.values())
            else "needs_review"
        )
        emit(
            "Revisión guardada localmente. El archivo status.json contiene solo estados y conteos."
        )
        return _status(directory, phase, counts=result.counts, review=review)
    except FlowError as error:
        _status(directory, "failed", error=error)
        raise
    except (KeyboardInterrupt, EOFError):
        _status(directory, "paused")
        raise


def _read_environment(path: Path) -> dict[str, str]:
    _check_file(path)
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("OPEN_FIN_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def prepare_live(project_root: Path) -> LiveClient:
    """Change only this project's mode; retain its token, key and persistent database."""
    env_path = project_root / ".env"
    values = _read_environment(env_path)
    if values.get("OPEN_FIN_MODE") != "live":
        content = env_path.read_text().splitlines()
        content = [
            line
            for line in content
            if not line.startswith(("OPEN_FIN_MODE=", "OPEN_FIN_JOB_TIMEOUT_SECONDS="))
        ]
        content.extend(["OPEN_FIN_MODE=live", "OPEN_FIN_JOB_TIMEOUT_SECONDS=600"])
        # The project directory is not a secret directory; replace only the checked .env atomically.
        descriptor, temporary = tempfile.mkstemp(prefix=".env.live-", dir=project_root)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write("\n".join(content) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, env_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        values = _read_environment(env_path)
    command = [
        "docker",
        "compose",
        "--project-name",
        "open-fin",
        "--project-directory",
        str(project_root),
        "-f",
        str(project_root / "compose.yaml"),
    ]
    for override in ("compose.override.yaml", "compose.override.yml"):
        if (project_root / override).is_file():
            command.extend(["-f", str(project_root / override)])
            break
    environment = os.environ.copy()
    environment.update(values)
    try:
        result = subprocess.run(
            [*command, "up", "--build", "-d", "--wait"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise FlowError("compose_failed") from None
    if result.returncode:
        raise FlowError("compose_failed")
    return LiveClient(
        int(values.get("OPEN_FIN_PORT", "8088")), values.get("OPEN_FIN_API_TOKEN", "")
    )


def main(argv=None, *, project_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validación privada de Santander en tu equipo.")
    parser.add_argument(
        "--prepare-only", action="store_true", help="Preparar Docker sin pedir claves"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="No abrir el informe automáticamente"
    )
    args = parser.parse_args(argv)
    root = Path(project_root or Path.cwd())
    directory = root / "data" / "validation" / "santander"
    try:
        if not args.prepare_only:
            ensure_terminal()
        with wizard_lock(directory):
            print("Open Fin · validación local de Santander")
            print(
                "Preparando servicios locales; tus credenciales se pedirán únicamente en Terminal."
            )
            client = prepare_live(root)
            if args.prepare_only:
                health = client.request("/health")
                if health.get("mode") != "live" or health.get("status") != "ok":
                    raise FlowError("live_required")
                _status(directory, "ready_for_credentials")
                print("Instancia en modo real, lista para el ingreso local de credenciales.")
                return 0
            result = run_validation(
                client,
                directory,
                show_report=(lambda _url: None) if args.no_open else webbrowser.open,
            )
            return 0 if result["phase"] == "verified_by_owner" else 2
    except FlowError as error:
        print(MESSAGES.get(error.code, "La validación se detuvo de forma segura."), file=sys.stderr)
        print("Código: " + error.code, file=sys.stderr)
        if error.stage:
            print("Etapa: " + error.stage, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nAsistente interrumpido. Al abrirlo otra vez retomará la misma tarea.")
        return 130
    except Exception:
        # Never render an unexpected exception: it can contain a response or bank values.
        try:
            _status(directory, "failed", error=FlowError("local_validation_failed"))
        except Exception:
            pass
        print("La validación local se detuvo de forma segura.", file=sys.stderr)
        print("Código: local_validation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
