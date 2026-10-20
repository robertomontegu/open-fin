"""Isolated integration fixtures; all credentials and bank data are synthetic."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from open_fin.api import create_app
from open_fin.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=str(tmp_path / "db.sqlite"),
        encryption_key=Fernet.generate_key().decode(),
        api_token="integration-test-token-that-is-longer-than-32-characters",
        connector_mode="demo",
        cooldown_seconds=0,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.headers["Authorization"] = f"Bearer {settings.api_token}"
        yield test_client
