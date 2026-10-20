"""Private validation acceptance cases, using only synthetic Santander data."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from open_fin.validation_report import analyse_snapshot, render_report

pytestmark = pytest.mark.spec("OF-AC-011")

CONNECTION = "synthetic-connection"
FINISHED = "2026-10-20T12:30:00.123456Z"


def snapshot():
    job = {
        "connection_id": CONNECTION,
        "status": "succeeded",
        "finished_at": FINISHED,
        "coverage": "visible_portal_history",
        "warnings": [],
    }
    products = [
        {
            "id": "account-clp",
            "kind": "account",
            "last4": "1111",
            "currency": "CLP",
            "balance": "15000",
        },
        {"id": "card-clp", "kind": "card", "last4": "2222", "currency": "CLP", "balance": None},
        {"id": "card-usd", "kind": "card", "last4": "2222", "currency": "USD", "balance": None},
    ]
    movements = [
        {
            "id": "movement-1",
            "product_id": "account-clp",
            "date": "2026-10-01",
            "description": "Synthetic salary",
            "amount": "15000",
            "currency": "CLP",
            "source": "account",
            "installments": None,
        },
        {
            "id": "movement-2",
            "product_id": "card-clp",
            "date": "2026-10-02",
            "description": "Synthetic purchase",
            "amount": "-2500",
            "currency": "CLP",
            "source": "credit_card_billed",
            "installments": "01/03",
        },
        {
            "id": "movement-3",
            "product_id": "card-usd",
            "date": "2026-10-03",
            "description": "Synthetic subscription",
            "amount": "-14.82",
            "currency": "USD",
            "source": "credit_card_unbilled",
            "installments": None,
        },
    ]
    for row in products + movements:
        row.update(connection_id=CONNECTION, last_sync_at=FINISHED)
    return job, products[:1], products[1:], movements


def analyse(parts):
    return analyse_snapshot(*parts, connection_id=CONNECTION)


def test_counts_are_safe_and_distinguish_physical_cards_from_currency_products():
    result = analyse(snapshot())
    assert result.structural_ok
    assert result.counts == {
        "accounts": 1,
        "cards": 1,
        "card_currency_products": 2,
        "movements": 3,
        "movements_account": 1,
        "movements_billed": 1,
        "movements_unbilled": 1,
    }
    assert all(type(value) is int for value in result.counts.values())
    safe_summary = str(result.counts) + str(result.checks)
    for private_value in ("1111", "2222", "15000", "-14.82", "Synthetic salary", CONNECTION):
        assert private_value not in safe_summary


def test_filters_cached_rows_and_does_not_claim_their_products_or_movements():
    parts = snapshot()
    old_account = deepcopy(parts[1][0])
    old_account.update(id="old-product", last_sync_at="2026-10-19T12:30:00Z")
    old_movement = deepcopy(parts[3][0])
    old_movement.update(id="old-movement", last_sync_at="2026-10-19T12:30:00Z")
    parts[1].append(old_account)
    parts[3].append(old_movement)
    result = analyse(parts)
    assert result.structural_ok
    assert result.excluded_cached_rows == 2
    assert result.counts["accounts"] == 1
    assert result.counts["movements"] == 3
    assert "old-product" not in str(result.accounts)
    assert "old-movement" not in str(result.movements)
    assert any(row["code"] == "cached_rows_excluded" for row in result.checks)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-20T12:30:00.123456+00:00",
        "2026-10-20T09:30:00.123456-03:00",
        datetime(2026, 10, 20, 12, 30, 0, 123456, tzinfo=timezone.utc),  # noqa: UP017
    ],
)
def test_timestamp_comparison_uses_instants(timestamp):
    parts = snapshot()
    for row in parts[1] + parts[2] + parts[3]:
        row["last_sync_at"] = timestamp
    result = analyse(parts)
    assert result.structural_ok
    assert result.excluded_cached_rows == 0
    assert result.counts["movements"] == 3


@pytest.mark.parametrize("timestamp", [None, "not-a-date", "2026-10-20T12:30:00", "2026-10-20"])
def test_missing_or_naive_finished_timestamp_fails(timestamp):
    parts = snapshot()
    parts[0]["finished_at"] = timestamp
    result = analyse(parts)
    assert not result.structural_ok
    assert not result.accounts and not result.cards and not result.movements


@pytest.mark.parametrize("status", ["failed", "running", "queued", None])
def test_unsuccessful_job_does_not_validate_cached_data(status):
    parts = snapshot()
    parts[0]["status"] = status
    result = analyse(parts)
    assert not result.structural_ok
    assert not result.accounts and not result.cards and not result.movements


def test_wrong_job_connection_fails_without_selecting_rows():
    parts = snapshot()
    parts[0]["connection_id"] = "different"
    result = analyse(parts)
    assert not result.structural_ok
    assert not result.accounts and not result.cards and not result.movements


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_sync_at", "invalid"),
        ("last_sync_at", "2026-10-21T12:30:00Z"),
        ("connection_id", "different"),
    ],
)
def test_invalid_or_foreign_snapshot_rows_fail(field, value):
    parts = snapshot()
    parts[3][0][field] = value
    assert not analyse(parts).structural_ok


def test_legitimate_duplicate_rows_with_distinct_ids_are_preserved():
    parts = snapshot()
    repeated = deepcopy(parts[3][0])
    repeated["id"] = "movement-repeat"
    parts[3].append(repeated)
    result = analyse(parts)
    assert result.structural_ok
    assert result.counts["movements"] == 4
    assert render_report(result).count("Synthetic salary") == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "movement-2"),
        ("id", ""),
        ("product_id", "missing-product"),
        ("product_id", "card-clp"),
        ("currency", "USD"),
        ("source", "unknown"),
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", "-Infinity"),
        ("amount", None),
        ("amount", True),
        ("date", "2026-02-30"),
        ("description", ""),
    ],
)
def test_invalid_movement_identity_reference_source_date_or_amount_fails(field, value):
    parts = snapshot()
    parts[3][0][field] = value
    assert not analyse(parts).structural_ok


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "card-clp"),
        ("id", ""),
        ("kind", "card"),
        ("currency", "EUR"),
        ("last4", "12345678"),
        ("balance", "NaN"),
    ],
)
def test_invalid_product_identity_kind_currency_or_balance_fails(field, value):
    parts = snapshot()
    parts[1][0][field] = value
    assert not analyse(parts).structural_ok


def test_empty_products_fail_instead_of_looking_like_a_successful_empty_bank():
    result = analyse_snapshot(snapshot()[0], [], [], [], connection_id=CONNECTION)
    assert not result.structural_ok
    assert all(value == 0 for value in result.counts.values())


def test_absent_sources_warn_and_complete_history_is_never_inferred():
    job, accounts, _, movements = snapshot()
    result = analyse_snapshot(job, accounts, [], movements[:1], connection_id=CONNECTION)
    assert result.structural_ok
    warnings = {row["code"] for row in result.checks if row["status"] == "warning"}
    assert {"no_cards", "history_coverage", "balance_coverage"} <= warnings
    no_movements = analyse_snapshot(job, accounts, [], [], connection_id=CONNECTION)
    assert no_movements.structural_ok
    assert any(row["code"] == "no_account_movements" for row in no_movements.checks)


def test_report_escapes_bank_text_and_never_copies_unselected_fields_or_raw_warnings():
    parts = snapshot()
    parts[0].update(warnings=["private raw bank error"], error_message="secret exception")
    for row in parts[1] + parts[2] + parts[3]:
        row.update(credentials={"password": "secret-password"}, rut="private-rut")
        row["raw_json"] = {"internal": "private raw JSON"}
        row["label"] = "1234 5678 9012 1111"
    parts[3][0]["description"] = '<script>alert("private")</script>&<img src="https://remote">'
    report = render_report(analyse(parts))
    assert "<script>" not in report and "<img" not in report
    assert "&lt;script&gt;" in report and "&quot;private&quot;" in report
    for secret in (
        "secret-password",
        "private-rut",
        "private raw JSON",
        "secret exception",
        "private raw bank error",
        "1234 5678 9012 1111",
    ):
        assert secret not in report
    assert "•••• 1111" in report and "•••• 2222" in report
    assert "No disponible" in report
    assert "-14.82" in report
    assert "Facturado" in report and "Por facturar" in report
    assert "default-src &#x27;none&#x27;" not in report
    assert "default-src 'none'" in report
    assert "form-action 'none'" in report


def test_selected_rows_contain_only_documented_fields_and_do_not_mutate_input():
    parts = snapshot()
    original = deepcopy(parts)
    result = analyse(parts)
    assert parts == original
    assert set(result.accounts[0]) == {"id", "kind", "last4", "currency", "balance"}
    assert set(result.movements[0]) == {
        "id",
        "product_id",
        "date",
        "description",
        "amount",
        "currency",
        "source",
        "installments",
    }


def test_decimal_amount_is_not_rounded():
    parts = snapshot()
    parts[3][2]["amount"] = Decimal("-14.8200")
    result = analyse(parts)
    assert result.structural_ok
    assert result.movements[2]["amount"] == "-14.8200"
    assert "-14.8200" in render_report(result)


@pytest.mark.parametrize(
    "review,state",
    [
        (None, "needs_review"),
        ({"products": "yes"}, "needs_review"),
        ({"products": "yes", "movements": "unsure", "statement_sources": "yes"}, "needs_review"),
        ({"products": "yes", "movements": "yes", "statement_sources": "yes"}, "verified_by_owner"),
        ({"products": "yes", "movements": "no", "statement_sources": "yes"}, "discrepancy"),
    ],
)
def test_owner_review_status_is_separate_from_structure(review, state):
    report = render_report(analyse(snapshot()), review)
    assert f'data-review-status="{state}"' in report
    assert "Estructura correcta" in report


def test_owner_confirmation_does_not_override_structural_failure():
    parts = snapshot()
    parts[3][0]["amount"] = "NaN"
    report = render_report(
        analyse(parts),
        dict.fromkeys(
            ("products", "movements", "statement_sources"),
            "yes",
        ),
    )
    assert 'data-review-status="discrepancy"' in report
    assert "Se encontraron errores de estructura" in report


def test_structural_failure_is_a_discrepancy_even_before_manual_review():
    parts = snapshot()
    parts[3][0]["amount"] = "NaN"
    report = render_report(analyse(parts))
    assert 'data-review-status="discrepancy"' in report
    assert "Discrepancias detectadas en la validación" in report


def test_static_report_contains_all_products_movements_and_manual_review_questions():
    class ReportParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tables = 0
            self.text = []
            self.external_resources = []
            self.scripts = 0

        def handle_starttag(self, tag, attrs):
            self.tables += tag == "table"
            self.scripts += tag == "script"
            self.external_resources.extend(
                value for name, value in attrs if name in {"src", "href", "action"}
            )

        def handle_data(self, text):
            self.text.append(text)

    parser = ReportParser()
    parser.feed(render_report(analyse(snapshot())))
    text = " ".join(parser.text)
    assert parser.tables == 3  # Accounts, card/currency products, all current movements.
    assert not parser.external_resources and not parser.scripts
    for required in (
        "Cuenta •••• 1111",
        "Tarjeta •••• 2222",
        "CLP",
        "USD",
        "No disponible",
        "Synthetic salary",
        "Synthetic purchase",
        "Synthetic subscription",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "15000",
        "-2500",
        "-14.82",
        "01/03",
        "Tarjeta · Facturado",
        "Tarjeta · Por facturar",
        "Importe",
        "Origen",
        "Cuotas",
        "Cuentas, tarjetas y monedas coinciden con Santander",
        "Movimientos, fechas e importes coinciden con el período revisado",
        "Facturados y por facturar se corresponden con sus respectivas vistas",
        "Pendiente de revisión por el titular",
        "Estructura correcta",
        "no prueba historial completo",
    ):
        assert required in text
