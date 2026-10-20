"""Public and connector contracts. Amounts serialize as decimal strings."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

Currency = Literal["CLP", "USD"]
Source = Literal["account", "credit_card_billed", "credit_card_unbilled"]
DIAGNOSTIC_STAGES = frozenset(
    {
        "starting",
        "login_page",
        "login_form",
        "login_submit",
        "accounts",
        "card_unbilled",
        "card_billed",
    }
)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rut: SecretStr
    password: SecretStr

    @field_validator("rut", "password")
    @classmethod
    def nonempty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip() or len(value.get_secret_value()) > 256:
            raise ValueError("A non-empty value of at most 256 characters is required")
        return value


class CreateConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank: Literal["santander"] = "santander"
    label: str = Field(default="Santander", min_length=1, max_length=80)
    credentials: Credentials


class BankProduct(BaseModel):
    """An external product; external_id must distinguish currency and product."""

    model_config = ConfigDict(extra="forbid")
    external_id: str = Field(min_length=1, max_length=200)
    kind: Literal["account", "card"]
    label: str = Field(min_length=1, max_length=120)
    last4: str = Field(pattern=r"^\d{4}$")
    currency: Currency
    balance: Decimal | None = None

    @field_validator("balance")
    @classmethod
    def finite_balance(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("Balance must be finite")
        return value


class BankMovement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_external_id: str
    date: date
    description: str = Field(min_length=1, max_length=1000)
    amount: Decimal
    currency: Currency
    source: Source
    bank_movement_id: str | None = None
    installments: str | None = None

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Amount must be finite")
        return value


class Snapshot(BaseModel):
    products: list[BankProduct]
    movements: list[BankMovement]
    coverage: str = "visible_portal_history"
    warnings: list[str] = Field(default_factory=list)


class ConnectorError(Exception):
    """Only stable, safe codes cross the connector boundary; never raw bank errors."""

    def __init__(self, code: str = "connector_failed", *, stage: str | None = None):
        super().__init__(code)
        self.code = code
        self.stage = stage if isinstance(stage, str) and stage in DIAGNOSTIC_STAGES else None


class ConnectionView(BaseModel):
    id: str
    bank: Literal["santander"]
    label: str
    mode: Literal["live", "demo"]
    status: Literal["created", "connected", "error"]
    created_at: datetime
    last_sync_at: datetime | None
    last_attempt_at: datetime | None
    last_error_code: str | None


class ConnectionList(BaseModel):
    items: list[ConnectionView]


class SyncView(BaseModel):
    id: str
    connection_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    coverage: str | None
    warnings: list[str]


class ProductView(BaseModel):
    id: str
    connection_id: str
    kind: Literal["account", "card"]
    label: str
    last4: str
    currency: Currency
    balance: Decimal | None
    last_sync_at: datetime


class MovementView(BaseModel):
    id: str
    connection_id: str
    product_id: str
    date: date
    description: str
    amount: Decimal
    currency: Currency
    source: Source
    installments: str | None
    last_sync_at: datetime


class ProductPage(BaseModel):
    items: list[ProductView]
    total: int
    limit: int
    offset: int


class MovementPage(BaseModel):
    items: list[MovementView]
    total: int
    limit: int
    offset: int
