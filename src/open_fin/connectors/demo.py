"""Explicitly synthetic data for local acceptance and onboarding."""

from datetime import date
from decimal import Decimal

from open_fin.models import BankMovement, BankProduct, ConnectorError, Credentials, Snapshot


class DemoConnector:
    def fetch(self, credentials: Credentials) -> Snapshot:
        if (credentials.rut.get_secret_value(), credentials.password.get_secret_value()) != (
            "11111111-1",
            "demo",
        ):
            raise ConnectorError("demo_credentials_required")
        products = [
            BankProduct(
                external_id="account:1111:CLP",
                kind="account",
                label="Demo checking ****1111 CLP",
                last4="1111",
                currency="CLP",
                balance=Decimal("1250000"),
            ),
            BankProduct(
                external_id="account:2222:USD",
                kind="account",
                label="Demo checking ****2222 USD",
                last4="2222",
                currency="USD",
                balance=Decimal("120.75"),
            ),
            BankProduct(
                external_id="card:3333:CLP",
                kind="card",
                label="Demo credit ****3333 CLP",
                last4="3333",
                currency="CLP",
            ),
            BankProduct(
                external_id="card:3333:USD",
                kind="card",
                label="Demo credit ****3333 USD",
                last4="3333",
                currency="USD",
            ),
        ]
        movements = [
            BankMovement(
                product_external_id="account:1111:CLP",
                date=date(2026, 10, 1),
                description="DEMO salary",
                amount=Decimal("1500000"),
                currency="CLP",
                source="account",
            ),
            BankMovement(
                product_external_id="account:2222:USD",
                date=date(2026, 10, 2),
                description="DEMO USD deposit",
                amount=Decimal("120.75"),
                currency="USD",
                source="account",
            ),
            BankMovement(
                product_external_id="card:3333:CLP",
                date=date(2026, 10, 3),
                description="DEMO installment",
                amount=Decimal("-25000"),
                currency="CLP",
                source="credit_card_billed",
                installments="01/03",
            ),
            BankMovement(
                product_external_id="card:3333:USD",
                date=date(2026, 10, 4),
                description="DEMO subscription",
                amount=Decimal("-14.82"),
                currency="USD",
                source="credit_card_unbilled",
            ),
        ]
        return Snapshot(
            products=products,
            movements=movements,
            coverage="synthetic_demo",
            warnings=["Synthetic demonstration data. No bank connection was made."],
        )
