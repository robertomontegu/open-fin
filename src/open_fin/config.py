"""Deployment configuration. Secrets are required, never generated on startup."""

import math
import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
    database_path: str
    encryption_key: str = field(repr=False)
    api_token: str = field(repr=False)
    connector_mode: str = "live"
    cooldown_seconds: int = 3600
    job_timeout_seconds: int = 240
    poll_seconds: float = 2
    browser_headless: bool = True
    source_url: str = "https://github.com/robertomontegu/open-fin"

    def __post_init__(self) -> None:
        if len(self.api_token) < 32:
            raise ValueError("OPEN_FIN_API_TOKEN must contain at least 32 characters")
        try:
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("OPEN_FIN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        if self.connector_mode not in {"live", "demo"}:
            raise ValueError("OPEN_FIN_MODE must be live or demo")
        if self.cooldown_seconds < 0 or not 30 <= self.job_timeout_seconds <= 900:
            raise ValueError("Invalid cooldown or job timeout")
        if (
            not math.isfinite(self.poll_seconds)
            or self.poll_seconds <= 0
            or not self.database_path
            or self.database_path == ":memory:"
        ):
            raise ValueError(
                "A persistent database path and positive polling interval are required"
            )
        if not self.source_url.startswith("https://github.com/"):
            raise ValueError("Source URL must point to the corresponding GitHub source")

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            key = os.environ["OPEN_FIN_ENCRYPTION_KEY"]
            token = os.environ["OPEN_FIN_API_TOKEN"]
        except KeyError as exc:
            raise ValueError("Run scripts/init-env.py and supply deployment secrets") from exc
        headless = os.getenv("OPEN_FIN_HEADLESS", "true").lower()
        if headless not in {"true", "false"}:
            raise ValueError("OPEN_FIN_HEADLESS must be true or false")
        return cls(
            database_path=os.getenv("OPEN_FIN_DATABASE_PATH", "/data/open-fin.db"),
            encryption_key=key,
            api_token=token,
            connector_mode=os.getenv("OPEN_FIN_MODE", "live"),
            cooldown_seconds=int(os.getenv("OPEN_FIN_COOLDOWN_SECONDS", "3600")),
            job_timeout_seconds=int(os.getenv("OPEN_FIN_JOB_TIMEOUT_SECONDS", "240")),
            poll_seconds=float(os.getenv("OPEN_FIN_POLL_SECONDS", "2")),
            browser_headless=headless == "true",
            source_url=os.getenv(
                "OPEN_FIN_SOURCE_URL", "https://github.com/robertomontegu/open-fin"
            ),
        )
