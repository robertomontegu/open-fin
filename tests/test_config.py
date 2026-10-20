"""Deployment, model and synthetic-mode boundaries; no bank connections."""

import os
import stat
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from open_fin.api import create_app
from open_fin.config import Settings
from open_fin.connectors.demo import DemoConnector
from open_fin.models import BankMovement, BankProduct, ConnectorError, Credentials


@pytest.fixture
def clean_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("OPEN_FIN_"):
            monkeypatch.delenv(name)


@pytest.fixture
def deployment_values(tmp_path):
    return {
        "database_path": str(tmp_path / "bank.db"),
        "encryption_key": Fernet.generate_key().decode(),
        "api_token": "synthetic-api-token-with-more-than-32-characters",
    }


@pytest.mark.spec("OF-AC-007")
def test_from_env_defaults_require_explicit_secrets(
    clean_environment, monkeypatch, deployment_values
):
    monkeypatch.setenv("OPEN_FIN_ENCRYPTION_KEY", deployment_values["encryption_key"])
    monkeypatch.setenv("OPEN_FIN_API_TOKEN", deployment_values["api_token"])
    settings = Settings.from_env()
    assert settings.database_path == "/data/open-fin.db"
    assert settings.connector_mode == "live"
    assert settings.cooldown_seconds == 3600
    assert settings.job_timeout_seconds == 240
    assert settings.poll_seconds == 2
    assert settings.browser_headless is True
    assert settings.source_url == "https://github.com/robertomontegu/open-fin"
    assert settings.encryption_key == deployment_values["encryption_key"]
    assert settings.api_token == deployment_values["api_token"]


@pytest.mark.spec("OF-AC-007")
def test_from_env_applies_explicit_deployment_configuration(
    clean_environment, monkeypatch, deployment_values
):
    environment = {
        "OPEN_FIN_ENCRYPTION_KEY": deployment_values["encryption_key"],
        "OPEN_FIN_API_TOKEN": deployment_values["api_token"],
        "OPEN_FIN_DATABASE_PATH": deployment_values["database_path"],
        "OPEN_FIN_MODE": "demo",
        "OPEN_FIN_COOLDOWN_SECONDS": "0",
        "OPEN_FIN_JOB_TIMEOUT_SECONDS": "900",
        "OPEN_FIN_POLL_SECONDS": "0.25",
        "OPEN_FIN_HEADLESS": "FALSE",
        "OPEN_FIN_SOURCE_URL": "https://github.com/example/open-fin-fork",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.database_path == deployment_values["database_path"]
    assert settings.connector_mode == "demo"
    assert settings.cooldown_seconds == 0
    assert settings.job_timeout_seconds == 900
    assert settings.poll_seconds == 0.25
    assert settings.browser_headless is False
    assert settings.source_url == environment["OPEN_FIN_SOURCE_URL"]


@pytest.mark.spec("OF-AC-001")
@pytest.mark.parametrize("missing", ["OPEN_FIN_API_TOKEN", "OPEN_FIN_ENCRYPTION_KEY", "both"])
def test_missing_secrets_fail_without_generating_replacements(
    clean_environment, monkeypatch, deployment_values, missing
):
    if missing not in {"OPEN_FIN_API_TOKEN", "both"}:
        monkeypatch.setenv("OPEN_FIN_API_TOKEN", deployment_values["api_token"])
    if missing not in {"OPEN_FIN_ENCRYPTION_KEY", "both"}:
        monkeypatch.setenv("OPEN_FIN_ENCRYPTION_KEY", deployment_values["encryption_key"])
    before = {name: value for name, value in os.environ.items() if name.startswith("OPEN_FIN_")}
    with pytest.raises(ValueError, match="supply deployment secrets") as error:
        Settings.from_env()
    assert deployment_values["api_token"] not in str(error.value)
    assert deployment_values["encryption_key"] not in str(error.value)
    assert {
        name: value for name, value in os.environ.items() if name.startswith("OPEN_FIN_")
    } == before


@pytest.mark.spec("OF-AC-001")
def test_settings_repr_does_not_disclose_deployment_secrets(deployment_values):
    representation = repr(Settings(**deployment_values))
    assert deployment_values["api_token"] not in representation
    assert deployment_values["encryption_key"] not in representation


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize(
    "override",
    [
        {"api_token": "a" * 31},
        {"encryption_key": "invalid-synthetic-key"},
        {"connector_mode": "automatic"},
        {"cooldown_seconds": -1},
        {"job_timeout_seconds": 29},
        {"job_timeout_seconds": 901},
        {"poll_seconds": 0},
        {"poll_seconds": -0.1},
        {"poll_seconds": float("nan")},
        {"poll_seconds": float("inf")},
        {"database_path": ""},
        {"database_path": ":memory:"},
        {"source_url": "http://github.com/example/open-fin"},
        {"source_url": "https://example.com/open-fin"},
    ],
)
def test_invalid_deployment_configuration_fails_before_startup(deployment_values, override):
    with pytest.raises(ValueError) as error:
        Settings(**(deployment_values | override))
    assert deployment_values["api_token"] not in str(error.value)
    assert deployment_values["encryption_key"] not in str(error.value)


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize("timeout", [30, 900])
def test_documented_timeout_and_cooldown_boundaries_are_accepted(deployment_values, timeout):
    settings = Settings(
        **(deployment_values | {"job_timeout_seconds": timeout, "cooldown_seconds": 0})
    )
    assert settings.job_timeout_seconds == timeout
    assert settings.cooldown_seconds == 0


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize(
    "name,value",
    [
        ("OPEN_FIN_COOLDOWN_SECONDS", "not-an-integer"),
        ("OPEN_FIN_JOB_TIMEOUT_SECONDS", "3.5"),
        ("OPEN_FIN_POLL_SECONDS", "not-a-number"),
        ("OPEN_FIN_POLL_SECONDS", "nan"),
        ("OPEN_FIN_POLL_SECONDS", "inf"),
        ("OPEN_FIN_HEADLESS", "misspelled-boolean"),
    ],
)
def test_invalid_environment_values_are_not_silently_accepted(
    clean_environment, monkeypatch, deployment_values, name, value
):
    monkeypatch.setenv("OPEN_FIN_ENCRYPTION_KEY", deployment_values["encryption_key"])
    monkeypatch.setenv("OPEN_FIN_API_TOKEN", deployment_values["api_token"])
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.spec("OF-AC-001")
def test_api_boot_reads_environment_and_refuses_missing_secrets(clean_environment):
    with pytest.raises(ValueError, match="supply deployment secrets"):
        with TestClient(create_app()):
            pytest.fail("An API without deployment secrets must not start")


@pytest.mark.spec("OF-AC-007")
def test_worker_cli_refuses_missing_boot_configuration(clean_environment, tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "open_fin.worker"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "supply deployment secrets" in result.stderr
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.spec("OF-AC-001")
@pytest.mark.spec("OF-AC-007")
def test_init_env_cli_creates_private_secrets_and_never_overwrites(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "init-env.py"
    output = tmp_path / "deployment.env"
    command = [sys.executable, str(script), "--output", str(output)]
    first = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert first.returncode == 0
    original = output.read_text()
    environment = dict(
        line.split("=", 1) for line in original.splitlines() if line and not line.startswith("#")
    )
    assert len(environment["OPEN_FIN_API_TOKEN"]) >= 32
    Fernet(environment["OPEN_FIN_ENCRYPTION_KEY"].encode())
    assert environment["OPEN_FIN_MODE"] == "demo"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    second = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert second.returncode == 1
    assert output.read_text() == original
    for secret in (environment["OPEN_FIN_API_TOKEN"], environment["OPEN_FIN_ENCRYPTION_KEY"]):
        assert secret not in first.stdout + first.stderr + second.stdout + second.stderr


@pytest.mark.spec("OF-AC-008")
@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_money_cannot_enter_connector_models(amount):
    with pytest.raises(ValidationError):
        BankMovement(
            product_external_id="synthetic-card-usd",
            date=date(2026, 10, 20),
            description="Synthetic transaction",
            amount=amount,
            currency="USD",
            source="credit_card_unbilled",
        )
    with pytest.raises(ValidationError):
        BankProduct(
            external_id="synthetic-account-usd",
            kind="account",
            label="Synthetic account",
            last4="1234",
            currency="USD",
            balance=amount,
        )


@pytest.mark.spec("OF-AC-001")
def test_credential_models_redact_secret_representations():
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    assert "synthetic-owner" not in repr(credentials)
    assert "synthetic-password" not in repr(credentials)
    assert "synthetic-password" not in credentials.model_dump_json()


@pytest.mark.spec("OF-AC-007")
@pytest.mark.parametrize(
    "rut,password",
    [("11111111-1", "other-password"), ("other-owner", "demo"), ("real-looking-owner", "secret")],
)
def test_demo_connector_rejects_every_non_demo_credential_pair(rut, password):
    with pytest.raises(ConnectorError, match="^demo_credentials_required$") as error:
        DemoConnector().fetch(Credentials(rut=rut, password=password))
    if password != "demo":
        assert password not in str(error.value)
    assert rut not in str(error.value)


@pytest.mark.spec("OF-AC-007")
def test_demo_snapshot_is_explicitly_synthetic_and_financially_typed():
    snapshot = DemoConnector().fetch(Credentials(rut="11111111-1", password="demo"))
    assert snapshot.coverage == "synthetic_demo"
    assert "No bank connection was made" in " ".join(snapshot.warnings)
    products = {product.external_id: product for product in snapshot.products}
    assert len(products) == len(snapshot.products) == 4
    for movement in snapshot.movements:
        assert isinstance(movement.amount, Decimal)
        assert products[movement.product_external_id].currency == movement.currency
    assert snapshot.model_dump(mode="json")["movements"][-1]["amount"] == "-14.82"
