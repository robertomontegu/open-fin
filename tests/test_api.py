"""API/database/worker acceptance tests against a synthetic connector boundary."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from open_fin.api import create_app
from open_fin.models import BankMovement, BankProduct, ConnectorError, Snapshot
from open_fin.storage import Store
from open_fin.worker import run_once


def create_connection(client, label="My Santander"):
    response = client.post(
        "/v1/connections",
        json={
            "bank": "santander",
            "label": label,
            "credentials": {"rut": "11111111-1", "password": "demo"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def queue_sync(client, connection_id):
    response = client.post(f"/v1/connections/{connection_id}/sync")
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    return job


def sample_snapshot():
    products = [
        BankProduct(
            external_id="account-1111-CLP",
            kind="account",
            label="Cuenta corriente CLP",
            last4="1111",
            currency="CLP",
            balance=Decimal("5000"),
        ),
        BankProduct(
            external_id="account-2222-USD",
            kind="account",
            label="Cuenta USD",
            last4="2222",
            currency="USD",
            balance=Decimal("20.75"),
        ),
        BankProduct(
            external_id="card-3333-CLP",
            kind="card",
            label="Tarjeta CLP",
            last4="3333",
            currency="CLP",
        ),
        BankProduct(
            external_id="card-3333-USD",
            kind="card",
            label="Tarjeta USD",
            last4="3333",
            currency="USD",
            balance=Decimal("30.25"),
        ),
    ]
    repeated = BankMovement(
        product_external_id="account-1111-CLP",
        date=date(2026, 10, 1),
        description="Compra repetida",
        amount=Decimal("-100"),
        currency="CLP",
        source="account",
    )
    return Snapshot(
        products=products,
        movements=[
            repeated,
            repeated.model_copy(),
            BankMovement(
                product_external_id="account-1111-CLP",
                date=date(2026, 10, 2),
                description="Abono",
                amount=Decimal("500"),
                currency="CLP",
                source="account",
                bank_movement_id="abono-1",
            ),
            BankMovement(
                product_external_id="account-2222-USD",
                date=date(2026, 10, 3),
                description="Compra USD",
                amount=Decimal("-1.25"),
                currency="USD",
                source="account",
            ),
            BankMovement(
                product_external_id="card-3333-CLP",
                date=date(2026, 10, 3),
                description="Notebook",
                amount=Decimal("-12000"),
                currency="CLP",
                source="credit_card_billed",
                installments="01/03",
            ),
            BankMovement(
                product_external_id="card-3333-USD",
                date=date(2026, 10, 4),
                description="Servicio",
                amount=Decimal("-12.34"),
                currency="USD",
                source="credit_card_unbilled",
            ),
        ],
        coverage="visible_portal_history",
        warnings=["Synthetic fixture; history is intentionally bounded"],
    )


class StaticConnector:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot if snapshot is not None else sample_snapshot()
        self.calls = 0

    def fetch(self, credentials):
        assert credentials.rut.get_secret_value() == "11111111-1"
        assert credentials.password.get_secret_value() == "demo"
        self.calls += 1
        return self.snapshot


def import_snapshot(client, settings, connection_id, connector=None):
    job = queue_sync(client, connection_id)
    assert run_once(settings, connector=connector or StaticConnector()) is True
    response = client.get(f"/v1/syncs/{job['id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded", response.text
    return response.json()


def list_items(client, endpoint, **params):
    response = client.get(f"/v1/{endpoint}", params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    assert isinstance(page["items"], list)
    assert page["total"] >= len(page["items"])
    assert isinstance(page["limit"], int)
    assert isinstance(page["offset"], int)
    return page


@pytest.mark.spec("OF-AC-001")
def test_health_is_public_and_exposes_no_bank_data(client):
    client.headers.pop("Authorization")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo", "version": "0.1.0"}


@pytest.mark.spec("OF-AC-001")
@pytest.mark.parametrize("authorization", [None, "Bearer incorrect-token", "Basic example"])
@pytest.mark.parametrize("path", ["/v1/connections", "/docs", "/openapi.json"])
def test_banking_and_documentation_require_valid_token(client, authorization, path):
    client.headers.pop("Authorization")
    if authorization is not None:
        client.headers["Authorization"] = authorization
    assert client.get(path).status_code == 401


@pytest.mark.spec("OF-AC-001")
def test_authenticated_documentation_is_available(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/v1/connections" in schema.json()["paths"]


@pytest.mark.spec("OF-AC-001")
def test_non_ascii_authorization_header_returns_401(client):
    response = client.get(
        "/v1/connections", headers=[(b"authorization", b"Bearer \xffinvalid-token")]
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.spec("OF-AC-001")
@pytest.mark.parametrize(
    "credentials",
    [
        {"rut": "sensitive-rut-sentinel", "password": ""},
        {"rut": "", "password": "sensitive-password-sentinel"},
        {"rut": "sensitive-rut-sentinel", "password": "sensitive-password-sentinel", "extra": 1},
    ],
)
def test_validation_errors_do_not_echo_credentials(client, credentials):
    response = client.post(
        "/v1/connections", json={"bank": "santander", "credentials": credentials}
    )
    assert response.status_code == 422
    assert "sensitive-rut-sentinel" not in response.text
    assert "sensitive-password-sentinel" not in response.text


@pytest.mark.spec("OF-AC-001")
@pytest.mark.parametrize(
    "override",
    [
        {"bank": "unsupported"},
        {"label": ""},
        {"credentials": None},
        {"credentials": {"rut": "sensitive-rut-sentinel"}},
        {"extra": "unsupported-field"},
    ],
)
def test_invalid_connection_payload_is_rejected_without_request_echo(client, override):
    payload = {
        "bank": "santander",
        "credentials": {
            "rut": "sensitive-rut-sentinel",
            "password": "sensitive-password-sentinel",
        },
    }
    payload.update(override)
    response = client.post("/v1/connections", json=payload)
    assert response.status_code == 422
    assert "sensitive-rut-sentinel" not in response.text
    assert "sensitive-password-sentinel" not in response.text


@pytest.mark.spec("OF-AC-007")
def test_demo_mode_rejects_real_credentials_without_echoing_them(client):
    response = client.post(
        "/v1/connections",
        json={
            "bank": "santander",
            "credentials": {"rut": "12345678-5", "password": "real-secret-sentinel"},
        },
    )
    assert response.status_code == 422
    assert "12345678-5" not in response.text
    assert "real-secret-sentinel" not in response.text
    assert client.get("/v1/connections").json()["items"] == []


@pytest.mark.spec("OF-AC-002")
def test_connections_are_encrypted_at_rest_and_responses_are_redacted(client, settings):
    connection = create_connection(client)
    assert connection["id"]
    assert connection["bank"] == "santander"
    assert connection["label"] == "My Santander"
    response = client.get("/v1/connections")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [connection["id"]]
    assert "11111111-1" not in response.text
    assert "password" not in response.text
    assert "credentials" not in response.text
    assert "11111111-1" not in str(connection)
    for data_file in Path(settings.database_path).parent.glob("db.sqlite*"):
        persisted = data_file.read_bytes()
        assert b"11111111-1" not in persisted
    with sqlite3.connect(settings.database_path) as database:
        ciphertext = database.execute(
            "SELECT credentials FROM connections WHERE id = ?", (connection["id"],)
        ).fetchone()[0]
    assert b'"password"' not in ciphertext
    assert b'"rut"' not in ciphertext
    assert json.loads(Fernet(settings.encryption_key.encode()).decrypt(ciphertext)) == {
        "rut": "11111111-1",
        "password": "demo",
    }


@pytest.mark.spec("OF-AC-003")
def test_queued_job_is_durable_and_demo_worker_stores_data(client, settings):
    connection = create_connection(client)
    job = queue_sync(client, connection["id"])
    assert client.get(f"/v1/syncs/{job['id']}").json()["status"] == "queued"
    assert run_once(settings) is True
    result = client.get(f"/v1/syncs/{job['id']}").json()
    assert result["status"] == "succeeded"
    assert list_items(client, "accounts")["total"] > 0
    assert list_items(client, "cards")["total"] > 0
    assert list_items(client, "movements")["total"] > 0
    assert run_once(settings) is False


@pytest.mark.spec("OF-AC-003")
def test_products_preserve_currency_identity_and_decimal_amounts(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    accounts = list_items(client, "accounts")["items"]
    cards = list_items(client, "cards")["items"]
    assert len(accounts) == 2
    assert len(cards) == 2
    products = {product["id"]: product for product in accounts + cards}
    assert len(products) == 4
    assert {card["currency"] for card in cards} == {"CLP", "USD"}
    assert {card["last4"] for card in cards} == {"3333"}
    for movement in list_items(client, "movements")["items"]:
        assert movement["product_id"] in products
        assert movement["currency"] == products[movement["product_id"]]["currency"]
        assert isinstance(movement["amount"], str)
    usd_movement = next(
        movement
        for movement in list_items(client, "movements")["items"]
        if movement["description"] == "Servicio"
    )
    assert usd_movement["amount"] == "-12.34"
    assert usd_movement["source"] == "credit_card_unbilled"
    notebook = next(
        movement
        for movement in list_items(client, "movements")["items"]
        if movement["description"] == "Notebook"
    )
    assert notebook["installments"] == "01/03"
    assert notebook["source"] == "credit_card_billed"


@pytest.mark.spec("OF-AC-003")
def test_cached_data_survives_application_restart(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    before = list_items(client, "movements")["items"]
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = f"Bearer {settings.api_token}"
        after = list_items(restarted, "movements")["items"]
        assert after == before


@pytest.mark.spec("OF-AC-004")
def test_repeated_import_is_idempotent_and_keeps_legitimate_duplicates(client, settings):
    connection = create_connection(client)
    connector = StaticConnector()
    import_snapshot(client, settings, connection["id"], connector)
    before = list_items(client, "movements")["items"]
    product_ids = {
        product["id"]
        for endpoint in ("accounts", "cards")
        for product in list_items(client, endpoint)["items"]
    }
    repeated = [movement for movement in before if movement["description"] == "Compra repetida"]
    assert len(before) == 6
    assert len(repeated) == 2
    assert len({movement["id"] for movement in repeated}) == 2
    import_snapshot(client, settings, connection["id"], connector)
    after = list_items(client, "movements")["items"]
    stable_fields = ("id", "product_id", "date", "description", "amount", "currency", "source")
    assert [{key: item[key] for key in stable_fields} for item in after] == [
        {key: item[key] for key in stable_fields} for item in before
    ]
    assert connector.calls == 2
    assert {
        product["id"]
        for endpoint in ("accounts", "cards")
        for product in list_items(client, endpoint)["items"]
    } == product_ids


@pytest.mark.spec("OF-AC-004")
def test_bank_movement_id_deduplicates_captures_and_preserves_identity_on_correction(
    client, settings
):
    connection = create_connection(client)
    snapshot = sample_snapshot()
    product = snapshot.products[2]
    movement = snapshot.movements[4].model_copy(update={"bank_movement_id": "purchase-001"})
    captured_twice = Snapshot(products=[product], movements=[movement, movement.model_copy()])
    import_snapshot(client, settings, connection["id"], StaticConnector(captured_twice))
    before = list_items(client, "movements")
    assert before["total"] == 1
    movement_id = before["items"][0]["id"]
    corrected = movement.model_copy(
        update={
            "date": date(2026, 10, 10),
            "installments": "02/03",
            "description": "Notebook corrected",
            "amount": Decimal("-13000"),
        }
    )
    corrected_snapshot = Snapshot(products=[product], movements=[corrected, corrected.model_copy()])
    connector = StaticConnector(corrected_snapshot)
    import_snapshot(client, settings, connection["id"], connector)
    import_snapshot(client, settings, connection["id"], connector)
    after = list_items(client, "movements")
    assert after["total"] == 1
    assert after["items"][0]["id"] == movement_id
    assert after["items"][0]["date"] == "2026-10-10"
    assert after["items"][0]["installments"] == "02/03"
    assert after["items"][0]["description"] == "Notebook corrected"
    assert after["items"][0]["amount"] == "-13000"


@pytest.mark.spec("OF-AC-005")
def test_only_one_job_can_be_queued_per_connection(client):
    connection = create_connection(client)
    queue_sync(client, connection["id"])
    response = client.post(f"/v1/connections/{connection['id']}/sync")
    assert response.status_code == 409


@pytest.mark.spec("OF-AC-005")
def test_invalid_snapshot_rolls_back_products_and_movements(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    before = {
        endpoint: list_items(client, endpoint)["items"]
        for endpoint in ("accounts", "cards", "movements")
    }
    invalid = Snapshot(
        products=[
            BankProduct(
                external_id="new-account-4444-CLP",
                kind="account",
                label="Must roll back",
                last4="4444",
                currency="CLP",
                balance=Decimal("9"),
            )
        ],
        movements=[
            BankMovement(
                product_external_id="missing-product",
                date=date(2026, 10, 10),
                description="Must not import",
                amount=Decimal("99"),
                currency="CLP",
                source="account",
            )
        ],
    )
    job = queue_sync(client, connection["id"])
    assert run_once(settings, connector=StaticConnector(invalid)) is True
    failed = client.get(f"/v1/syncs/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "invalid_snapshot"
    for endpoint, items in before.items():
        assert list_items(client, endpoint)["items"] == items


@pytest.mark.spec("OF-AC-005")
def test_unexpected_connector_error_is_safe_and_does_not_erase_cache(client, settings, caplog):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    before = list_items(client, "movements")["items"]

    class FailingConnector:
        def fetch(self, credentials):
            raise RuntimeError("sensitive-password-sentinel 11111111-1")

    job = queue_sync(client, connection["id"])
    assert run_once(settings, connector=FailingConnector()) is True
    response = client.get(f"/v1/syncs/{job['id']}")
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "connector_failed"
    assert "sensitive-password-sentinel" not in response.text
    assert "11111111-1" not in response.text
    assert "sensitive-password-sentinel" not in caplog.text
    assert list_items(client, "movements")["items"] == before


@pytest.mark.spec("OF-AC-005")
def test_empty_snapshot_is_successful_and_distinguishable_from_failure(client, settings):
    connection = create_connection(client)
    job = import_snapshot(
        client, settings, connection["id"], StaticConnector(Snapshot(products=[], movements=[]))
    )
    assert job["status"] == "succeeded"
    assert job["error_code"] is None
    assert list_items(client, "movements")["total"] == 0


@pytest.mark.spec("OF-AC-005")
def test_worker_recovers_abandoned_job_without_retrying_connector(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    cached = list_items(client, "movements")["items"]
    abandoned = queue_sync(client, connection["id"])
    # Simulate a process dying after claiming its durable job and losing its file lock.
    with sqlite3.connect(settings.database_path) as database:
        database.execute(
            "UPDATE syncs SET status = 'running', started_at = ? WHERE id = ?",
            ("2026-10-20T00:00:00+00:00", abandoned["id"]),
        )
    connector = StaticConnector()
    assert run_once(settings, connector=connector) is False
    assert connector.calls == 0
    recovered = client.get(f"/v1/syncs/{abandoned['id']}").json()
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "worker_interrupted"
    assert recovered["finished_at"] is not None
    assert list_items(client, "movements")["items"] == cached


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize("created_mode,worker_mode", [("demo", "live"), ("live", "demo")])
def test_worker_mode_mismatch_fails_before_decrypting_or_fetching(
    settings, monkeypatch, created_mode, worker_mode
):
    created_settings = replace(settings, connector_mode=created_mode)
    with TestClient(create_app(created_settings)) as creator:
        creator.headers["Authorization"] = f"Bearer {settings.api_token}"
        connection = create_connection(creator)
        job = Store(settings.database_path, settings.encryption_key).enqueue(connection["id"], 0)
        assert connection["mode"] == created_mode
        credential_reads = []

        def forbidden_credentials(_store, connection_id):
            credential_reads.append(connection_id)
            raise AssertionError("Check mode mismatch before opening the credential vault")

        monkeypatch.setattr(Store, "credentials", forbidden_credentials)
        connector = StaticConnector()
        changed_settings = replace(settings, connector_mode=worker_mode)
        assert run_once(changed_settings, connector=connector) is True
        result = creator.get(f"/v1/syncs/{job['id']}").json()
        assert result["status"] == "failed"
        assert result["error_code"] == "mode_mismatch"
        assert credential_reads == []
        assert connector.calls == 0
        assert list_items(creator, "movements")["items"] == []


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize("live_error", [None, "authentication_required"])
def test_live_worker_uses_live_boundary_and_never_demo_connector(settings, monkeypatch, live_error):
    live_settings = replace(settings, connector_mode="live")
    live_calls, demo_calls = [], []

    def synthetic_live_fetch(configured, credentials, lock_descriptor=None):
        assert configured.connector_mode == "live"
        assert credentials.rut.get_secret_value() == "11111111-1"
        assert credentials.password.get_secret_value() == "demo"
        live_calls.append(True)
        if live_error is not None:
            raise ConnectorError(live_error)
        return sample_snapshot()

    class ForbiddenDemoConnector:
        def __init__(self):
            demo_calls.append(True)
            raise AssertionError("Live deployments must never instantiate the demo connector")

    monkeypatch.setattr("open_fin.worker.fetch_live", synthetic_live_fetch)
    monkeypatch.setattr("open_fin.worker.DemoConnector", ForbiddenDemoConnector)
    with TestClient(create_app(live_settings)) as live:
        live.headers["Authorization"] = f"Bearer {settings.api_token}"
        connection = create_connection(live)
        job = queue_sync(live, connection["id"])
        assert run_once(live_settings) is True
        result = live.get(f"/v1/syncs/{job['id']}").json()
        assert live_calls == [True]
        assert demo_calls == []
        if live_error is None:
            assert result["status"] == "succeeded"
            assert list_items(live, "movements")["total"] == 6
        else:
            assert result["status"] == "failed"
            assert result["error_code"] == live_error
            assert list_items(live, "movements")["total"] == 0


@pytest.mark.spec("OF-AC-005")
@pytest.mark.parametrize(
    "movement_override", [{"currency": "USD"}, {"source": "credit_card_billed"}]
)
def test_product_currency_and_source_mismatch_preserves_previous_snapshot(
    client, settings, movement_override
):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    before = {
        endpoint: list_items(client, endpoint)["items"]
        for endpoint in ("accounts", "cards", "movements")
    }
    invalid = sample_snapshot()
    invalid.products[0] = invalid.products[0].model_copy(update={"balance": Decimal("999999")})
    invalid.movements[0] = invalid.movements[0].model_copy(update=movement_override)
    job = queue_sync(client, connection["id"])
    assert run_once(settings, connector=StaticConnector(invalid)) is True
    result = client.get(f"/v1/syncs/{job['id']}").json()
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_snapshot"
    for endpoint, items in before.items():
        assert list_items(client, endpoint)["items"] == items


@pytest.mark.spec("OF-AC-005")
def test_cooldown_returns_retry_after(client, settings):
    throttled_settings = replace(settings, cooldown_seconds=3600)
    with TestClient(create_app(throttled_settings)) as throttled:
        throttled.headers["Authorization"] = f"Bearer {settings.api_token}"
        connection = create_connection(throttled)
        import_snapshot(throttled, throttled_settings, connection["id"])
        response = throttled.post(f"/v1/connections/{connection['id']}/sync")
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 3600


@pytest.mark.spec("OF-AC-005")
def test_cooldown_starts_when_worker_claims_a_delayed_queued_job(settings):
    throttled_settings = replace(settings, cooldown_seconds=3600)
    with TestClient(create_app(throttled_settings)) as throttled:
        throttled.headers["Authorization"] = f"Bearer {settings.api_token}"
        connection = create_connection(throttled)
        job = queue_sync(throttled, connection["id"])
        old_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with sqlite3.connect(settings.database_path) as database:
            database.execute(
                "UPDATE syncs SET requested_at = ? WHERE id = ?", (old_time, job["id"])
            )
            database.execute(
                "UPDATE connections SET last_attempt_at = ? WHERE id = ?",
                (old_time, connection["id"]),
            )
        assert run_once(throttled_settings, connector=StaticConnector()) is True
        result = throttled.get(f"/v1/syncs/{job['id']}").json()
        assert result["status"] == "succeeded"
        response = throttled.post(f"/v1/connections/{connection['id']}/sync")
        assert response.status_code == 429
        assert 3500 <= int(response.headers["Retry-After"]) <= 3600


@pytest.mark.spec("OF-AC-007")
def test_live_api_hides_demo_cache_and_rejects_explicit_cross_mode_queries(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    product_id = list_items(client, "accounts")["items"][0]["id"]
    live_settings = replace(settings, connector_mode="live")
    with TestClient(create_app(live_settings)) as live:
        live.headers["Authorization"] = f"Bearer {settings.api_token}"
        for endpoint in ("accounts", "cards", "movements"):
            page = list_items(live, endpoint)
            assert page["total"] == 0
            assert page["items"] == []
            explicit = live.get(f"/v1/{endpoint}", params={"connection_id": connection["id"]})
            assert explicit.status_code == 409
            assert explicit.json()["error_code"] == "mode_mismatch"
        product_query = live.get("/v1/movements", params={"product_id": product_id})
        assert product_query.status_code == 409
        assert product_query.json()["error_code"] == "mode_mismatch"
        synchronization = live.post(f"/v1/connections/{connection['id']}/sync")
        assert synchronization.status_code == 409
        assert synchronization.json()["error_code"] == "mode_mismatch"
        listed = live.get("/v1/connections").json()["items"]
        assert listed[0]["id"] == connection["id"]
        assert listed[0]["mode"] == "demo"
        assert live.delete(f"/v1/connections/{connection['id']}").status_code == 204


@pytest.mark.spec("OF-AC-006")
def test_running_job_blocks_new_sync_and_connection_deletion(client, settings):
    connection = create_connection(client)
    job = queue_sync(client, connection["id"])
    entered, release = Event(), Event()

    class BlockingConnector:
        def fetch(self, credentials):
            entered.set()
            assert release.wait(timeout=10), "Test did not release connector"
            return sample_snapshot()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(run_once, settings, BlockingConnector())
        try:
            assert entered.wait(timeout=5), "Worker did not start"
            assert client.get(f"/v1/syncs/{job['id']}").json()["status"] == "running"
            assert client.post(f"/v1/connections/{connection['id']}/sync").status_code == 409
            assert client.delete(f"/v1/connections/{connection['id']}").status_code == 409
        finally:
            release.set()
        assert running.result(timeout=5) is True


@pytest.mark.spec("OF-AC-006")
def test_list_filters_keep_connections_and_products_separate(client, settings):
    first = create_connection(client, "First")
    second = create_connection(client, "Second")
    import_snapshot(client, settings, first["id"])
    import_snapshot(client, settings, second["id"])
    for endpoint, expected_count in (("accounts", 2), ("cards", 2), ("movements", 6)):
        page = list_items(client, endpoint, connection_id=first["id"])
        assert page["total"] == expected_count
        assert {item["connection_id"] for item in page["items"]} == {first["id"]}
    product = list_items(client, "cards", connection_id=first["id"])["items"][0]
    movements = list_items(client, "movements", product_id=product["id"])["items"]
    assert movements
    assert {movement["product_id"] for movement in movements} == {product["id"]}
    wrong_connection = client.get(
        "/v1/movements",
        params={"connection_id": second["id"], "product_id": product["id"]},
    )
    assert wrong_connection.status_code == 404


@pytest.mark.spec("OF-AC-006")
def test_movement_filters_preserve_dates_currency_and_source(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    page = list_items(
        client,
        "movements",
        from_date="2026-10-03",
        to_date="2026-10-04",
        currency="USD",
        source="credit_card_unbilled",
    )
    assert page["total"] == 1
    assert page["items"][0]["description"] == "Servicio"
    assert page["items"][0]["date"] == "2026-10-04"
    inclusive = list_items(client, "movements", from_date="2026-10-01", to_date="2026-10-01")
    assert inclusive["total"] == 2


@pytest.mark.spec("OF-AC-006")
def test_movement_pagination_is_stable_and_reports_filtered_total(client, settings):
    connection = create_connection(client)
    import_snapshot(client, settings, connection["id"])
    all_rows = list_items(client, "movements")["items"]
    first = list_items(client, "movements", limit=2, offset=0)
    second = list_items(client, "movements", limit=2, offset=2)
    assert first["limit"] == second["limit"] == 2
    assert first["offset"] == 0
    assert second["offset"] == 2
    assert first["total"] == second["total"] == 6
    assert first["items"] == all_rows[:2]
    assert second["items"] == all_rows[2:4]
    assert list_items(client, "movements", limit=2, offset=100)["items"] == []


@pytest.mark.spec("OF-AC-006")
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 201},
        {"limit": 0},
        {"offset": -1},
        {"from_date": "2026-10-05", "to_date": "2026-10-01"},
        {"from_date": "invalid-date"},
        {"currency": "EUR"},
        {"source": "invalid-source"},
    ],
)
def test_invalid_movement_filters_return_422(client, params):
    assert client.get("/v1/movements", params=params).status_code == 422


@pytest.mark.spec("OF-AC-006")
@pytest.mark.parametrize("endpoint", ["accounts", "cards", "movements"])
def test_unknown_connection_filter_returns_404(client, endpoint):
    assert client.get(f"/v1/{endpoint}", params={"connection_id": "missing"}).status_code == 404


@pytest.mark.spec("OF-AC-006")
def test_unknown_product_sync_and_delete_return_404(client):
    assert client.get("/v1/movements", params={"product_id": "missing"}).status_code == 404
    assert client.get("/v1/syncs/missing").status_code == 404
    assert client.post("/v1/connections/missing/sync").status_code == 404
    assert client.delete("/v1/connections/missing").status_code == 404


@pytest.mark.spec("OF-AC-002")
def test_delete_cascades_cache_and_jobs_but_preserves_other_connections(client, settings):
    first = create_connection(client, "Delete")
    second = create_connection(client, "Keep")
    job = import_snapshot(client, settings, first["id"])
    import_snapshot(client, settings, second["id"])
    assert client.delete(f"/v1/connections/{first['id']}").status_code == 204
    assert [item["id"] for item in client.get("/v1/connections").json()["items"]] == [second["id"]]
    assert client.get(f"/v1/syncs/{job['id']}").status_code == 404
    for endpoint in ("accounts", "cards", "movements"):
        items = list_items(client, endpoint)["items"]
        assert items
        assert {item["connection_id"] for item in items} == {second["id"]}
