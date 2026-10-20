"""Local live-validation safeguards using only synthetic loopback HTTP responses."""

import json
import os
import stat
import warnings
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from subprocess import CompletedProcess, TimeoutExpired
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from open_fin import local_validation as validation

SYNTHETIC_TOKEN = "synthetic-owner-token-that-is-never-a-real-deployment-token"
SYNTHETIC_SECRET = "SYNTHETIC_PRIVATE_VALUE_6a72"


@contextmanager
def local_server(responder):
    """Expose a synthetic HTTP responder and record only synthetic request fixtures."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            request = {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
            requests.append(request)
            status, headers, payload = responder(request)
            if isinstance(payload, dict):
                payload = json.dumps(payload).encode()
            elif isinstance(payload, str):
                payload = payload.encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def client_for(origin):
    return validation.LiveClient(urlsplit(origin).port, SYNTHETIC_TOKEN, timeout=2)


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirects_never_forward_authorization_or_credential_body(status, method):
    with local_server(lambda _request: (200, {}, {"unexpected": "target"})) as (
        target,
        forwarded,
    ):
        with local_server(
            lambda _request: (status, {"Location": target + "/receive"}, SYNTHETIC_SECRET)
        ) as (origin, sent):
            payload = {"credentials": {"password": SYNTHETIC_SECRET}} if method == "POST" else None
            with pytest.raises(validation.FlowError) as caught:
                client_for(origin).request("/v1/connections", method, payload)
            assert len(sent) == 1
            assert forwarded == []
            assert SYNTHETIC_SECRET not in str(caught.value)
            assert SYNTHETIC_TOKEN not in str(caught.value)


@pytest.mark.spec("OF-AC-009")
def test_environment_proxies_are_bypassed_for_private_requests(monkeypatch):
    with local_server(lambda _request: (200, {}, {"proxy": True})) as (proxy, proxied):
        with local_server(lambda _request: (200, {}, {"status": "ok"})) as (origin, direct):
            for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "ALL_PROXY"):
                monkeypatch.setenv(name, proxy)
            monkeypatch.setenv("no_proxy", "")
            monkeypatch.setenv("NO_PROXY", "")
            assert client_for(origin).request("/health") == {"status": "ok"}
            assert len(direct) == 1
            assert direct[0]["headers"]["Authorization"] == "Bearer " + SYNTHETIC_TOKEN
            assert proxied == []


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize("status", [400, 401, 422, 429, 500])
def test_http_errors_do_not_echo_private_response_bodies(status, capsys):
    with local_server(
        lambda _request: (status, {"Retry-After": "3600"}, {"detail": SYNTHETIC_SECRET})
    ) as (origin, _requests):
        with pytest.raises(validation.FlowError) as caught:
            client_for(origin).request("/v1/connections")
        assert SYNTHETIC_SECRET not in str(caught.value)
        assert SYNTHETIC_TOKEN not in str(caught.value)
        if status == 429:
            assert caught.value.retry_after == 3600
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize("payload", [SYNTHETIC_SECRET, "[]", "null", "{incomplete"])
def test_malformed_or_nonobject_json_fails_without_response_echo(payload):
    with local_server(lambda _request: (200, {}, payload)) as (origin, _requests):
        with pytest.raises(validation.FlowError) as caught:
            client_for(origin).request("/health")
        assert payload not in str(caught.value)


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize("port", [0, -1, 65536, "https://example.invalid", True])
def test_api_transport_accepts_only_a_valid_local_port(port):
    with pytest.raises((validation.FlowError, ValueError, TypeError)):
        validation.LiveClient(port, SYNTHETIC_TOKEN)


@pytest.mark.spec("OF-AC-009")
def test_noninteractive_terminal_is_rejected(monkeypatch):
    monkeypatch.setattr(validation.sys, "stdin", StringIO(SYNTHETIC_SECRET))
    with pytest.raises(validation.FlowError):
        validation.ensure_terminal()


@pytest.mark.spec("OF-AC-010")
def test_all_pages_fetches_every_item_with_a_limit_of_200():
    rows = [{"id": str(index)} for index in range(405)]

    def respond(request):
        params = parse_qs(urlsplit(request["path"]).query)
        offset = int(params["offset"][0])
        limit = int(params["limit"][0])
        assert params["connection_id"] == ["synthetic-connection"]
        assert limit == 200
        return (
            200,
            {},
            {
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            },
        )

    with local_server(respond) as (origin, requests):
        assert client_for(origin).all_pages("movements", "synthetic-connection") == rows
        assert [parse_qs(urlsplit(row["path"]).query)["offset"] for row in requests] == [
            ["0"],
            ["200"],
            ["400"],
        ]


@pytest.mark.spec("OF-AC-010")
@pytest.mark.parametrize("fault", ["total_changed", "truncated", "wrong_offset", "too_many"])
def test_inconsistent_or_truncated_pages_fail(fault):
    def respond(request):
        params = parse_qs(urlsplit(request["path"]).query)
        offset = int(params["offset"][0])
        page = {
            "items": [{"id": str(offset + index)} for index in range(200 if not offset else 1)],
            "total": 201,
            "limit": 200,
            "offset": offset,
        }
        if offset:
            if fault == "total_changed":
                page["total"] = 202
            elif fault == "truncated":
                page["items"] = []
            elif fault == "wrong_offset":
                page["offset"] = 0
            else:
                page["items"] = [{"id": str(index)} for index in range(201)]
        return 200, {}, page

    with local_server(respond) as (origin, _requests):
        with pytest.raises(validation.FlowError):
            client_for(origin).all_pages("accounts", "synthetic-connection")


@pytest.mark.spec("OF-AC-010")
def test_oversized_collection_is_rejected_before_unbounded_pagination():
    response = {"items": [{"id": "one"}], "total": 20001, "limit": 200, "offset": 0}
    with local_server(lambda _request: (200, {}, response)) as (origin, requests):
        with pytest.raises(validation.FlowError):
            client_for(origin).all_pages("movements", "synthetic-connection")
        assert len(requests) == 1


@pytest.mark.spec("OF-AC-011")
def test_local_artifacts_are_private_and_unsafe_links_are_refused(tmp_path):
    directory = tmp_path / "owner-only"
    validation.private_directory(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    state = directory / "state.json"
    validation.write_private(state, json.dumps({"phase": "queued"}))
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert validation.read_private_json(state) == {"phase": "queued"}
    target = directory / "untouched.json"
    target.write_text("untouched")
    link = directory / "linked.json"
    link.symlink_to(target)
    with pytest.raises(validation.FlowError):
        validation.write_private(link, SYNTHETIC_SECRET)
    with pytest.raises(validation.FlowError):
        validation.read_private_json(link)
    assert target.read_text() == "untouched"
    hardlink = directory / "hardlinked.json"
    os.link(state, hardlink)
    with pytest.raises(validation.FlowError):
        validation.write_private(hardlink, SYNTHETIC_SECRET)
    assert target.read_text() == "untouched"


@pytest.mark.spec("OF-AC-009")
def test_getpass_echo_warning_fails_closed(monkeypatch):
    monkeypatch.setattr(validation, "ensure_terminal", lambda: None)

    def echo_fallback(_prompt):
        warnings.warn(SYNTHETIC_SECRET, validation.getpass.GetPassWarning, stacklevel=2)
        return SYNTHETIC_SECRET

    monkeypatch.setattr(validation.getpass, "getpass", echo_fallback)
    with pytest.raises(validation.FlowError) as caught:
        validation.secret_input("Clave Santander: ")
    assert SYNTHETIC_SECRET not in str(caught.value)


class SyntheticLiveClient:
    """One synthetic owner connection/job; never reads a deployment or contacts a bank."""

    port = 8088
    base = "http://127.0.0.1:8088"
    connection_id = "con_" + "c" * 32
    job_id = "syn_" + "d" * 32
    finished_at = "2026-10-20T12:00:00+00:00"

    def __init__(self, *, health=None, queued=False, uncertain=None, failed=False):
        self.health = health if health is not None else {"status": "ok", "mode": "live"}
        self.queued = queued
        self.uncertain = uncertain
        self.failed = failed
        self.requests = []
        self.page_requests = []

    def job(self, status):
        return {
            "id": self.job_id,
            "connection_id": self.connection_id,
            "status": status,
            "requested_at": "2026-10-20T11:59:00+00:00",
            "started_at": "2026-10-20T11:59:01+00:00",
            "finished_at": self.finished_at if status in {"succeeded", "failed"} else None,
            "error_code": "connector_failed" if status == "failed" else None,
            "error_message": SYNTHETIC_SECRET if status == "failed" else None,
            "coverage": "visible_portal_history",
            "warnings": [],
        }

    def request(self, path, method="GET", body=None):
        self.requests.append((method, path, body))
        connection = {
            "id": self.connection_id,
            "bank": "santander",
            "mode": "live",
            "status": "connected",
            "label": "Synthetic Santander",
        }
        if path == "/health":
            return self.health
        if path == "/v1/connections" and method == "GET":
            return {"items": []}
        if path == "/v1/connections" and method == "POST":
            if self.uncertain == "connection":
                raise validation.FlowError("http_error")
            return connection
        if path == f"/v1/connections/{self.connection_id}" and method == "GET":
            return connection
        if path == f"/v1/connections/{self.connection_id}/sync" and method == "POST":
            if self.uncertain == "sync":
                raise validation.FlowError("http_error")
            return self.job("queued")
        if path == f"/v1/syncs/{self.job_id}" and method == "GET":
            return self.job("queued" if self.queued else "failed" if self.failed else "succeeded")
        raise AssertionError(f"Unexpected synthetic request: {method} {path}")

    def all_pages(self, resource, connection_id):
        assert connection_id == self.connection_id
        self.page_requests.append(resource)
        common = {"connection_id": self.connection_id, "last_sync_at": self.finished_at}
        if resource == "accounts":
            return [
                dict(
                    common,
                    id="synthetic-account",
                    kind="account",
                    label="Synthetic account",
                    last4="3141",
                    currency="CLP",
                    balance=None,
                )
            ]
        if resource == "cards":
            return []
        if resource == "movements":
            return [
                dict(
                    common,
                    id="synthetic-movement",
                    product_id="synthetic-account",
                    date="2026-10-19",
                    description=SYNTHETIC_SECRET,
                    amount="-1482.75",
                    currency="CLP",
                    source="account",
                    installments=None,
                )
            ]
        raise AssertionError("Unknown synthetic resource")


def run_synthetic(client, directory, **kwargs):
    secrets = iter(["11111111-1", SYNTHETIC_SECRET])
    options = {
        "prompt_secret": lambda _prompt: next(secrets),
        "prompt_review": lambda _prompt: "s",
        "show_report": lambda _path: None,
        "emit": lambda _message: None,
        "sleep": lambda _seconds: None,
    }
    options.update(kwargs)
    return validation.run_validation(client, directory, **options)


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize(
    "health", [{"status": "ok", "mode": "demo"}, {"status": "bad", "mode": "live"}, {}]
)
def test_live_health_is_checked_before_collecting_credentials_or_mutating(health, tmp_path):
    client = SyntheticLiveClient(health=health)

    def reject_prompt(_prompt):
        raise AssertionError("Credentials must not be requested before healthy live mode")

    with pytest.raises(validation.FlowError):
        run_synthetic(client, tmp_path / "trial", prompt_secret=reject_prompt)
    assert [(method, path) for method, path, _body in client.requests] == [("GET", "/health")]


@pytest.mark.spec("OF-AC-009")
def test_api_authentication_is_checked_before_collecting_bank_credentials(tmp_path):
    client = SyntheticLiveClient()
    original_request = client.request

    def unauthenticated(path, method="GET", body=None):
        if path != "/health":
            raise validation.FlowError("api_authentication_failed")
        return original_request(path, method, body)

    client.request = unauthenticated

    def reject_prompt(_prompt):
        raise AssertionError("Bank credentials must not be requested before API authentication")

    with pytest.raises(validation.FlowError) as caught:
        run_synthetic(client, tmp_path / "trial", prompt_secret=reject_prompt)
    assert caught.value.code == "api_authentication_failed"
    assert all(method == "GET" for method, _path, _body in client.requests)


@pytest.mark.spec("OF-AC-010")
def test_one_sync_is_requested_and_rerun_reuses_the_saved_job(tmp_path):
    client = SyntheticLiveClient()
    directory = tmp_path / "trial"
    run_synthetic(client, directory)

    def reject_prompt(_prompt):
        raise AssertionError("Resuming a saved job must not collect credentials")

    run_synthetic(client, directory, prompt_secret=reject_prompt)
    posts = [path for method, path, _body in client.requests if method == "POST"]
    assert posts == ["/v1/connections", f"/v1/connections/{client.connection_id}/sync"]
    state = json.loads((directory / "state.json").read_text())
    assert client.connection_id in json.dumps(state)
    assert client.job_id in json.dumps(state)
    assert SYNTHETIC_SECRET not in json.dumps(state)
    assert SYNTHETIC_TOKEN not in json.dumps(state)
    assert "11111111-1" not in json.dumps(state)
    assert {"accounts", "cards", "movements"}.issubset(set(client.page_requests))


@pytest.mark.spec("OF-AC-010")
def test_saved_attempt_cannot_resume_against_a_different_local_api_origin(tmp_path):
    client = SyntheticLiveClient()
    directory = tmp_path / "trial"
    run_synthetic(client, directory)
    previous_posts = sum(method == "POST" for method, _path, _body in client.requests)
    client.port = 8089
    client.base = "http://127.0.0.1:8089"
    with pytest.raises(validation.FlowError):
        run_synthetic(client, directory)
    assert sum(method == "POST" for method, _path, _body in client.requests) == previous_posts


@pytest.mark.spec("OF-AC-010")
def test_interrupting_polling_resumes_without_another_bank_attempt(tmp_path):
    client = SyntheticLiveClient(queued=True)
    directory = tmp_path / "trial"

    def interrupt(_seconds):
        raise KeyboardInterrupt

    with pytest.raises((KeyboardInterrupt, validation.FlowError)):
        run_synthetic(client, directory, sleep=interrupt)
    assert (directory / "state.json").is_file()
    client.queued = False

    def reject_prompt(_prompt):
        raise AssertionError("An interrupted job must resume without bank credentials")

    run_synthetic(client, directory, prompt_secret=reject_prompt)
    assert sum(method == "POST" for method, _path, _body in client.requests) == 2


@pytest.mark.spec("OF-AC-010")
@pytest.mark.parametrize("mutation", ["connection", "sync"])
def test_uncertain_mutation_outcome_is_never_repeated_automatically(mutation, tmp_path):
    client = SyntheticLiveClient(uncertain=mutation)
    directory = tmp_path / "trial"
    with pytest.raises(validation.FlowError):
        run_synthetic(client, directory)
    previous_posts = sum(method == "POST" for method, _path, _body in client.requests)
    client.uncertain = None
    with pytest.raises(validation.FlowError) as caught:
        run_synthetic(client, directory)
    assert caught.value.code == "outcome_unknown"
    assert sum(method == "POST" for method, _path, _body in client.requests) == previous_posts


@pytest.mark.spec("OF-AC-010")
def test_failed_job_does_not_retry_login_or_disclose_bank_error(tmp_path, capsys):
    client = SyntheticLiveClient(failed=True)
    directory = tmp_path / "trial"
    emitted = []
    with pytest.raises(validation.FlowError):
        run_synthetic(client, directory, emit=emitted.append)
    previous_posts = sum(method == "POST" for method, _path, _body in client.requests)
    with pytest.raises(validation.FlowError):
        run_synthetic(client, directory, emit=emitted.append)
    assert sum(method == "POST" for method, _path, _body in client.requests) == previous_posts == 2
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err + json.dumps(emitted)


@pytest.mark.spec("OF-AC-012")
def test_unrecognized_api_error_codes_are_not_copied_to_shareable_status(tmp_path):
    client = SyntheticLiveClient(failed=True)
    original_job = client.job

    def untrusted_job(status):
        return dict(original_job(status), error_code=SYNTHETIC_SECRET)

    client.job = untrusted_job
    directory = tmp_path / "trial"
    emitted = []
    with pytest.raises(validation.FlowError) as caught:
        run_synthetic(client, directory, emit=emitted.append)
    public = str(caught.value) + json.dumps(emitted)
    if (directory / "status.json").exists():
        public += (directory / "status.json").read_text()
    assert SYNTHETIC_SECRET not in public


@pytest.mark.spec("OF-AC-012")
def test_shareable_status_and_terminal_output_omit_private_values(tmp_path, capsys):
    client = SyntheticLiveClient()
    directory = tmp_path / "trial"
    emitted = []
    result = run_synthetic(client, directory, emit=emitted.append)
    status = (directory / "status.json").read_text()
    public = json.dumps(result) + status + json.dumps(emitted)
    for value in (
        SYNTHETIC_SECRET,
        SYNTHETIC_TOKEN,
        "11111111-1",
        client.connection_id,
        client.job_id,
        "3141",
        "1482.75",
        "2026-10-19",
        "synthetic-account",
        "synthetic-movement",
    ):
        assert value not in public
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err


@pytest.mark.spec("OF-AC-012")
def test_cli_unexpected_error_is_safe_without_a_traceback(monkeypatch, tmp_path, capsys):
    def fail_preparation(_project_root):
        raise ValueError(SYNTHETIC_SECRET)

    monkeypatch.setattr(validation, "prepare_live", fail_preparation)
    assert validation.main(["--prepare-only"], project_root=tmp_path) == 1
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def synthetic_environment(project_root, *, mode="demo", timeout="240"):
    values = {
        "OPEN_FIN_API_TOKEN": SYNTHETIC_TOKEN,
        "OPEN_FIN_ENCRYPTION_KEY": "SYNTHETIC_DEPLOYMENT_ENCRYPTION_KEY",
        "OPEN_FIN_MODE": mode,
        "OPEN_FIN_JOB_TIMEOUT_SECONDS": timeout,
        "OPEN_FIN_COOLDOWN_SECONDS": "3900",
        "OPEN_FIN_DATABASE_PATH": "/data/synthetic-open-fin.db",
        "OPEN_FIN_PORT": "18765",
        "OPEN_FIN_HEADLESS": "true",
    }
    environment_path = project_root / ".env"
    environment_path.write_text(
        "# Synthetic owner configuration, never a real deployment\n"
        + "\n".join(key + "=" + value for key, value in values.items())
        + "\n"
    )
    environment_path.chmod(0o600)
    (project_root / "compose.yaml").write_text("name: open-fin\n")
    return environment_path, values


@pytest.mark.spec("OF-AC-009")
@pytest.mark.parametrize("override", [None, "compose.override.yaml", "compose.override.yml"])
def test_prepare_live_preserves_secrets_and_cooldown_and_builds_explicit_project(
    monkeypatch, tmp_path, capsys, override
):
    environment_path, values = synthetic_environment(tmp_path)
    if override:
        (tmp_path / override).write_text("services: {}\n")
    calls = []

    def compose(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, stdout=SYNTHETIC_SECRET, stderr=SYNTHETIC_SECRET)

    monkeypatch.setattr(validation.subprocess, "run", compose)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "unrelated-synthetic-project")
    monkeypatch.setenv("COMPOSE_FILE", "/unrelated-synthetic/compose.yaml")
    client = validation.prepare_live(tmp_path)
    assert client.base == "http://127.0.0.1:18765"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ["docker", "compose"]
    assert args[args.index("--project-name") + 1] == "open-fin"
    assert args[args.index("--project-directory") + 1] == str(tmp_path)
    compose_files = [args[index + 1] for index, value in enumerate(args) if value == "-f"]
    assert compose_files == [str(tmp_path / "compose.yaml")] + (
        [str(tmp_path / override)] if override else []
    )
    assert {"up", "--build", "-d", "--wait"}.issubset(args)
    assert kwargs["cwd"] == tmp_path
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] > 0
    for name in (
        "OPEN_FIN_API_TOKEN",
        "OPEN_FIN_ENCRYPTION_KEY",
        "OPEN_FIN_COOLDOWN_SECONDS",
        "OPEN_FIN_DATABASE_PATH",
    ):
        assert kwargs["env"][name] == values[name]
        assert name + "=" + values[name] in environment_path.read_text()
    assert kwargs["env"]["OPEN_FIN_MODE"] == "live"
    assert kwargs["env"]["OPEN_FIN_JOB_TIMEOUT_SECONDS"] == "600"
    assert "OPEN_FIN_MODE=live" in environment_path.read_text()
    assert "OPEN_FIN_JOB_TIMEOUT_SECONDS=600" in environment_path.read_text()
    assert stat.S_IMODE(environment_path.stat().st_mode) == 0o600
    public_command = json.dumps(args)
    assert SYNTHETIC_TOKEN not in public_command
    assert values["OPEN_FIN_ENCRYPTION_KEY"] not in public_command
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err
    assert SYNTHETIC_TOKEN not in output.out + output.err


@pytest.mark.spec("OF-AC-009")
def test_prepare_live_is_idempotent_for_an_existing_live_configuration(monkeypatch, tmp_path):
    environment_path, _values = synthetic_environment(tmp_path, mode="live", timeout="600")
    original = environment_path.read_bytes()
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        lambda args, **_kwargs: CompletedProcess(args, 0, stdout=b"", stderr=b""),
    )
    validation.prepare_live(tmp_path)
    validation.prepare_live(tmp_path)
    assert environment_path.read_bytes() == original
    assert stat.S_IMODE(environment_path.stat().st_mode) == 0o600


@pytest.mark.spec("OF-AC-012")
@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_prepare_live_redacts_compose_failures(monkeypatch, tmp_path, capsys, failure):
    synthetic_environment(tmp_path)

    def compose(args, **_kwargs):
        if failure == "timeout":
            raise TimeoutExpired(args, 1, output=SYNTHETIC_SECRET, stderr=SYNTHETIC_SECRET)
        if failure == "oserror":
            raise OSError(SYNTHETIC_SECRET)
        return CompletedProcess(args, 1, stdout=SYNTHETIC_SECRET, stderr=SYNTHETIC_SECRET)

    monkeypatch.setattr(validation.subprocess, "run", compose)
    with pytest.raises(validation.FlowError) as caught:
        validation.prepare_live(tmp_path)
    assert caught.value.code == "compose_failed"
    assert SYNTHETIC_SECRET not in str(caught.value)
    output = capsys.readouterr()
    assert SYNTHETIC_SECRET not in output.out + output.err


@pytest.mark.spec("OF-AC-009")
def test_prepare_live_rejects_an_environment_file_with_public_permissions(monkeypatch, tmp_path):
    environment_path, _values = synthetic_environment(tmp_path)
    environment_path.chmod(0o644)

    def reject_subprocess(*_args, **_kwargs):
        raise AssertionError("Unsafe configuration must be rejected before invoking Docker")

    monkeypatch.setattr(validation.subprocess, "run", reject_subprocess)
    with pytest.raises(validation.FlowError) as caught:
        validation.prepare_live(tmp_path)
    assert caught.value.code == "unsafe_file"
