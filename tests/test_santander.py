"""Synthetic contract evidence for OF-REQ-005; never connects to a bank."""

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from open_fin.connectors import santander
from open_fin.connectors.santander import (
    SantanderConnector,
    _product,
    parse_amount,
    parse_date,
    parse_rows,
)
from open_fin.models import ConnectorError, Credentials, Snapshot

pytestmark = pytest.mark.spec("OF-AC-008")


@pytest.fixture
def rows():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "santander_visible_rows.json").read_text()
    )


@pytest.fixture
def connector():
    result = SantanderConnector()
    result._deadline = time.monotonic() + 10
    return result


def _login_entries(page, *, current_count=0, legacy_count=1):
    current, legacy = MagicMock(), MagicMock()
    current.filter.return_value = current
    legacy.filter.return_value = legacy
    current.count.return_value = current_count
    legacy.count.return_value = legacy_count
    page.get_by_role.side_effect = lambda role, **_options: current if role == "link" else legacy
    return current, legacy


@pytest.mark.parametrize(
    "text,currency,expected",
    [
        ("$ 1.234.567", "CLP", "1234567"),
        ("-5.000", "CLP", "-5000"),
        ("CLP 0", "CLP", "0"),
        ("US$ 14,82", "USD", "14.82"),
        ("USD 14.82", "USD", "14.82"),
        ("1.234,56", "USD", "1234.56"),
        ("1,234.56", "USD", "1234.56"),
        ("-0,01", "USD", "-0.01"),
        ("1,234", "USD", "1234"),
        ("1.234", "USD", "1234"),
        ("+42", "CLP", "42"),
    ],
)
def test_strict_decimal_parsing(text, currency, expected):
    result = parse_amount(text, currency)
    assert result == Decimal(expected)
    assert isinstance(result, Decimal)


@pytest.mark.parametrize(
    "text,currency",
    [
        ("", "CLP"),
        ("—", "USD"),
        ("NaN", "USD"),
        ("Infinity", "USD"),
        ("1e3", "CLP"),
        ("1.2.3", "CLP"),
        ("12,3456", "USD"),
        ("--100", "CLP"),
        ("USD 100", "CLP"),
        ("CLP 100", "USD"),
        ("12,34.56", "USD"),
    ],
)
def test_invalid_money_never_becomes_zero(text, currency):
    with pytest.raises(ConnectorError, match="^invalid_amount$"):
        parse_amount(text, currency)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20/10/2026", date(2026, 10, 20)),
        ("01-02-2025", date(2025, 2, 1)),
        ("19/10", date(2026, 10, 19)),
        ("31/12", date(2025, 12, 31)),
    ],
)
def test_strict_dates_and_bounded_recent_year_inference(text, expected):
    assert parse_date(text, date(2026, 10, 20)) == expected


@pytest.mark.parametrize("text", ["", "31/02/2026", "2026-10-20", "01/13", "yesterday"])
def test_invalid_dates_fail(text):
    with pytest.raises(ConnectorError, match="^invalid_date$"):
        parse_date(text, date(2026, 10, 20))


def test_usd_sources_installments_and_repeated_occurrences_survive(rows):
    product = _product("card", "1234", "USD")
    unbilled = parse_rows(rows["unbilled_usd"], product, "credit_card_unbilled")
    billed = parse_rows(rows["billed_usd"], product, "credit_card_billed")
    assert [row.amount for row in unbilled] == [
        Decimal("-14.82"),
        Decimal("-14.82"),
        Decimal("2.50"),
    ]
    assert unbilled[0] == unbilled[1]
    assert len(unbilled) == 3
    assert unbilled[0].installments == "1/3"
    assert {row.source for row in unbilled} == {"credit_card_unbilled"}
    assert {row.source for row in billed} == {"credit_card_billed"}
    assert [row.amount for row in billed] == [Decimal("-1234.56"), Decimal("10.25")]
    assert all(row.currency == "USD" for row in unbilled + billed)
    assert unbilled[0].model_dump(mode="json")["amount"] == "-14.82"


def test_account_charge_payment_and_zero_are_preserved(rows):
    product = _product("account", "00012345678", "CLP")
    result = parse_rows(rows["account_clp"], product, "account")
    assert [row.amount for row in result] == [Decimal("1234567"), Decimal("-5000"), Decimal(0)]
    assert all(row.product_external_id == product.external_id for row in result)


@pytest.mark.parametrize(
    "row,code",
    [
        (
            {"date": "20/10/2026", "description": "Test", "charge": "", "payment": ""},
            "invalid_amount",
        ),
        (
            {"date": "20/10/2026", "description": "Test", "charge": "1", "payment": "2"},
            "ambiguous_movement_amount",
        ),
        ({"date": "20/10/2026", "description": "", "charge": "1"}, "invalid_movement_description"),
        ({"date": "", "description": "Test", "charge": "1"}, "invalid_date"),
        ({"date": "20/10/2026", "description": "Test", "charge": "broken"}, "invalid_amount"),
    ],
)
def test_bad_rows_fail_the_snapshot_instead_of_skipping(row, code):
    with pytest.raises(ConnectorError, match=f"^{code}$"):
        parse_rows([row], _product("account", "1234", "CLP"), "account")


def test_products_distinguish_currency_kind_and_full_account_reference():
    products = [
        _product("account", "11111234", "CLP"),
        _product("account", "22221234", "CLP"),
        _product("card", "1234", "CLP"),
        _product("card", "1234", "USD"),
    ]
    assert len({product.external_id for product in products}) == 4
    assert all(product.last4 == "1234" and product.balance is None for product in products)
    assert all("11111234" not in product.model_dump_json() for product in products)


@pytest.mark.parametrize(
    "url",
    [
        "https://santander.cl/login",
        "https://banco.santander.cl/personas",
        "https://mibanco.santander.cl:443/private",
    ],
)
def test_credentials_only_allow_santander_https_origins(url):
    SantanderConnector._trusted_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://banco.santander.cl",
        "https://santander.cl.example.com",
        "https://evil.example/?next=santander.cl",
        "https://user@banco.santander.cl",
        "https://banco.santander.cl:444",
        "https://banco.santander.cl:bad",
    ],
)
def test_untrusted_origins_fail_before_credentials_are_filled(url):
    with pytest.raises(ConnectorError, match="^untrusted_bank_origin$"):
        SantanderConnector._trusted_url(url)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "bank_access_denied"),
        (403, "bank_access_denied"),
        (400, "bank_http_error"),
        (404, "bank_http_error"),
        (429, "bank_http_error"),
        (500, "bank_http_error"),
        (503, "bank_http_error"),
    ],
)
def test_navigation_classifies_failed_http_responses_before_reading_portal(connector, status, code):
    page = MagicMock()
    page.url = santander.LOGIN_URL
    page.goto.return_value.status = status
    with pytest.raises(ConnectorError, match=f"^{code}$"):
        connector._goto(page, santander.LOGIN_URL)
    page.locator.assert_not_called()
    page.get_by_role.assert_not_called()


@pytest.mark.parametrize("status", [200, 204, 302, 304, None])
def test_navigation_accepts_successful_responses_and_no_response(connector, status):
    page = MagicMock()
    page.url = santander.LOGIN_URL
    if status is None:
        page.goto.return_value = None
    else:
        page.goto.return_value.status = status
    connector._goto(page, santander.LOGIN_URL)
    page.goto.assert_called_once()


def test_navigation_checks_final_origin_before_http_status(connector):
    page = MagicMock()
    page.url = "https://untrusted.example/private"
    page.goto.return_value.status = 403
    with pytest.raises(ConnectorError, match="^untrusted_bank_origin$"):
        connector._goto(page, santander.LOGIN_URL)


@pytest.mark.parametrize("status", [401, 403])
def test_login_http_denial_reports_login_page_without_filling_or_submitting(monkeypatch, status):
    manager = MagicMock()
    browser = manager.__enter__.return_value.chromium.launch.return_value
    page = browser.new_context.return_value.new_page.return_value
    page.url = santander.LOGIN_URL
    page.goto.return_value.status = status
    monkeypatch.setattr(santander, "sync_playwright", lambda: manager)
    connector = SantanderConnector()
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    with pytest.raises(ConnectorError) as failure:
        connector.fetch(credentials)
    assert failure.value.code == "bank_access_denied"
    assert failure.value.stage == "login_page"
    assert str(failure.value) == "bank_access_denied"
    page.get_by_role.assert_not_called()
    page.get_by_label.assert_not_called()
    page.locator.assert_not_called()
    browser.close.assert_called_once()


def test_login_does_not_fill_credentials_into_redirected_iframe(connector, monkeypatch):
    page = MagicMock()
    _login_entries(page)
    frame = page.locator.return_value.element_handle.return_value.content_frame.return_value
    frame.url = "https://untrusted.example/login"
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    with pytest.raises(ConnectorError, match="^untrusted_bank_origin$"):
        connector._login(page, credentials)
    frame.get_by_role.return_value.fill.assert_not_called()
    frame.get_by_label.return_value.fill.assert_not_called()


@pytest.mark.parametrize(
    "current_count,legacy_count,expected",
    [(1, 1, "current"), (1, 2, "current"), (0, 1, "legacy")],
)
def test_login_uses_current_link_or_unique_visible_legacy_button(
    connector, monkeypatch, current_count, legacy_count, expected
):
    page = MagicMock()
    page.url = santander.LOGIN_URL
    current, legacy = _login_entries(page, current_count=current_count, legacy_count=legacy_count)
    frame = page.locator.return_value.element_handle.return_value.content_frame.return_value
    frame.url = "https://banco.santander.cl/login"
    frame.get_by_label.return_value.count.return_value = 1
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    connector._login(page, credentials)
    page.get_by_role.assert_any_call("link", name="Abrir panel de ingreso", exact=True)
    page.get_by_role.assert_any_call("button", name="Ingresar al sitio privado")
    current.filter.assert_called_once_with(visible=True)
    legacy.filter.assert_called_once_with(visible=True)
    selected, unselected = (current, legacy) if expected == "current" else (legacy, current)
    selected.click.assert_called_once()
    unselected.click.assert_not_called()
    frame.get_by_role.return_value.fill.assert_called_once()
    assert frame.get_by_role.return_value.fill.call_args.args == ("synthetic-owner",)
    frame.get_by_label.return_value.fill.assert_called_once()


@pytest.mark.parametrize(
    "current_count,legacy_count,code",
    [
        (2, 1, "ambiguous_login_entry"),
        (0, 2, "ambiguous_login_entry"),
        (0, 0, "missing_login_entry"),
    ],
)
def test_login_missing_or_ambiguous_entry_never_opens_or_fills_credentials(
    connector, monkeypatch, current_count, legacy_count, code
):
    page = MagicMock()
    current, legacy = _login_entries(page, current_count=current_count, legacy_count=legacy_count)
    if not current_count and not legacy_count:
        current.or_.return_value.first.wait_for.side_effect = PlaywrightTimeoutError(
            "synthetic page content must not escape"
        )
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    with pytest.raises(ConnectorError, match=f"^{code}$"):
        connector._login(page, Credentials(rut="synthetic-owner", password="synthetic-password"))
    assert connector._stage == "login_page"
    current.click.assert_not_called()
    legacy.click.assert_not_called()
    page.locator.assert_not_called()


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://api-dsk.santander.cl/perdsk/transactions", True),
        ("https://third-party.example/collect", False),
        ("http://banco.santander.cl/collect", False),
    ],
)
def test_browser_requests_cannot_send_credentials_to_external_services(url, allowed):
    route = MagicMock()
    route.request.url = url
    SantanderConnector._route_bank_request(route)
    if allowed:
        route.continue_.assert_called_once()
        route.abort.assert_not_called()
    else:
        route.abort.assert_called_once_with("blockedbyclient")
        route.continue_.assert_not_called()


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "safe_failure",
        "unexpected_failure",
        "timeout",
        "login_failure",
        "context_failure",
    ],
)
def test_fetch_closes_browser_and_sanitizes_failures(monkeypatch, outcome):
    manager = MagicMock()
    browser = manager.__enter__.return_value.chromium.launch.return_value
    if outcome == "context_failure":
        browser.new_context.side_effect = RuntimeError("sensitive context setup error")
    monkeypatch.setattr(santander, "sync_playwright", lambda: manager)
    adapter = SantanderConnector()

    def login(*_):
        if outcome == "login_failure":
            raise ConnectorError("authentication_or_challenge_failed")

    monkeypatch.setattr(adapter, "_login", login)
    expected = Snapshot(products=[], movements=[])

    def snapshot(_page):
        if outcome == "safe_failure":
            raise ConnectorError("missing_movement_table")
        if outcome == "unexpected_failure":
            raise RuntimeError("sensitive-bank-password-must-not-escape")
        if outcome == "timeout":
            raise PlaywrightTimeoutError("sensitive DOM and input value")
        return expected

    monkeypatch.setattr(adapter, "_snapshot", snapshot)
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    if outcome == "success":
        assert adapter.fetch(credentials) == expected
    else:
        with pytest.raises(ConnectorError) as error:
            adapter.fetch(credentials)
        assert (
            str(error.value)
            == {
                "safe_failure": "missing_movement_table",
                "unexpected_failure": "connector_failed",
                "timeout": "bank_page_timeout",
                "login_failure": "authentication_or_challenge_failed",
                "context_failure": "connector_failed",
            }[outcome]
        )
    browser.close.assert_called_once()
    manager.__enter__.return_value.chromium.launch.assert_called_once_with(
        headless=True, chromium_sandbox=True
    )


@pytest.mark.parametrize(
    "stage,code",
    [
        ("starting", "connector_failed"),
        ("login_page", "bank_page_timeout"),
        ("login_form", "bank_page_timeout"),
        ("login_submit", "authentication_or_challenge_failed"),
        ("accounts", "missing_account_products"),
        ("card_unbilled", "missing_card_products"),
        ("card_billed", "missing_card_products"),
    ],
)
def test_fetch_reports_only_safe_failure_stage_and_closes_browser(monkeypatch, stage, code):
    manager = MagicMock()
    browser = manager.__enter__.return_value.chromium.launch.return_value
    page = browser.new_context.return_value.new_page.return_value
    page.url = "https://banco.santander.cl/personas"
    page.goto.return_value.status = 200
    _login_entries(page)
    iframe = MagicMock()
    frame = iframe.element_handle.return_value.content_frame.return_value
    frame.url = "https://banco.santander.cl/login"
    frame.get_by_label.return_value.count.return_value = 1
    greeting = MagicMock()
    page.locator.side_effect = lambda selector: iframe if selector == "#login-frame" else greeting
    monkeypatch.setattr(santander, "sync_playwright", lambda: manager)
    adapter = SantanderConnector()
    sensitive_text = "synthetic-sensitive-rut-password-DOM-https://example.com/private"
    if stage == "starting":
        browser.new_context.side_effect = RuntimeError(sensitive_text)
    elif stage == "login_page":
        page.goto.side_effect = PlaywrightTimeoutError(sensitive_text)
    elif stage == "login_form":
        iframe.wait_for.side_effect = PlaywrightTimeoutError(sensitive_text)
    elif stage == "login_submit":
        greeting.wait_for.side_effect = PlaywrightTimeoutError(sensitive_text)
    else:
        monkeypatch.setattr(adapter, "_login", lambda *_: None)
        if stage == "accounts":
            monkeypatch.setattr(adapter, "_accounts", MagicMock(side_effect=ConnectorError(code)))
        else:
            monkeypatch.setattr(adapter, "_goto", lambda *_: None)
            monkeypatch.setattr(adapter, "_accounts", lambda *_: [])
            monkeypatch.setattr(
                adapter,
                "_cards",
                MagicMock(
                    side_effect=[ConnectorError(code)]
                    if stage == "card_unbilled"
                    else [[], ConnectorError(code)]
                ),
            )
    credentials = Credentials(rut="synthetic-owner", password="synthetic-password")
    with pytest.raises(ConnectorError) as error:
        adapter.fetch(credentials)
    assert error.value.code == code
    assert str(error.value) == code
    assert error.value.stage == stage
    assert sensitive_text not in repr(error.value)
    assert error.value.__suppress_context__ is True
    browser.close.assert_called_once()


def test_login_challenge_wait_is_bounded_and_does_not_resubmit(connector, monkeypatch):
    page = MagicMock()
    page.url = "https://banco.santander.cl/personas"
    _login_entries(page)
    frame = page.locator.return_value.element_handle.return_value.content_frame.return_value
    frame.url = "https://banco.santander.cl/login"
    frame.get_by_label.return_value.count.return_value = 1
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    connector._deadline = time.monotonic() + 600
    connector._login(page, Credentials(rut="synthetic-owner", password="synthetic-password"))
    page.locator.assert_any_call("h3:has-text('Hola')")
    assert page.locator.return_value.wait_for.call_args.kwargs["timeout"] == 90_000
    frame.get_by_role.assert_any_call("button", name="Ingresar", exact=True)
    frame.get_by_role.return_value.click.assert_called_once()
    assert connector._stage == "login_submit"


def test_missing_table_cannot_be_empty_success(connector):
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("synthetic")
    page.get_by_text.return_value.all.return_value = []
    with pytest.raises(ConnectorError, match="^missing_movement_table$"):
        connector._movements(page, _product("account", "1234", "CLP"), "account")


def test_explicit_bank_empty_state_is_success(connector):
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("synthetic")
    empty = MagicMock()
    empty.is_visible.return_value = True
    page.get_by_text.return_value.all.return_value = [empty]
    assert connector._movements(page, _product("card", "1234", "USD"), "credit_card_billed") == []


@pytest.mark.parametrize(
    "method,code",
    [("_accounts", "missing_account_products"), ("_cards", "missing_card_products")],
)
def test_hidden_empty_state_cannot_hide_missing_products(connector, monkeypatch, method, code):
    page = MagicMock()
    page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("synthetic")
    empty = MagicMock()
    empty.is_visible.return_value = False
    page.get_by_text.return_value.all.return_value = [empty]
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    with pytest.raises(ConnectorError, match=f"^{code}$"):
        getattr(connector, method)(page)


@pytest.mark.parametrize("active_text,count", [("* 5678", 1), ("* 1234", 2), ("unknown", 1)])
def test_card_identity_must_be_verified_not_assumed_from_position(active_text, count):
    page = MagicMock()
    page.locator.return_value.count.return_value = count
    page.locator.return_value.inner_text.return_value = active_text
    with pytest.raises(ConnectorError, match="^ambiguous_product_identity$"):
        SantanderConnector._verify_card(page, "1234")


def test_duplicate_card_last4_is_ambiguous(connector):
    page = MagicMock()
    slides = [MagicMock(), MagicMock()]
    for slide in slides:
        slide.locator.return_value.inner_text.return_value = "* 1234"
    page.locator.return_value.all.return_value = slides
    with pytest.raises(ConnectorError, match="^ambiguous_product_identity$"):
        connector._cards(page)


def test_deadline_is_explicit_and_never_retries_login(connector):
    connector._deadline = time.monotonic() - 1
    with pytest.raises(ConnectorError, match="^connector_timeout$"):
        connector._timeout()


@pytest.mark.spec("OF-AC-003")
def test_snapshot_keeps_multiple_products_currencies_and_sources(connector, monkeypatch):
    page = MagicMock()
    accounts = [santander._Account("11111234", "CLP"), santander._Account("22221234", "CLP")]
    monkeypatch.setattr(connector, "_goto", lambda *_: None)
    monkeypatch.setattr(connector, "_accounts", lambda *_: accounts)
    monkeypatch.setattr(connector, "_account_movements", lambda *_: [])
    monkeypatch.setattr(connector, "_cards", lambda *_: ["1234", "5678"])
    monkeypatch.setattr(connector, "_select_card", lambda *_: None)
    monkeypatch.setattr(connector, "_verify_card", lambda *_: None)
    monkeypatch.setattr(connector, "_card_currencies", lambda *_: ["CLP", "USD"])
    monkeypatch.setattr(connector, "_select_currency", lambda *_: None)

    def movements(_page, product, source):
        row = {"date": "20/10/2026", "description": "Synthetic repeated purchase"}
        if source == "credit_card_billed":
            row["amount"] = "10,25"
        else:
            row.update(charge="10,25", payment="")
        return parse_rows([row, row.copy()], product, source)

    monkeypatch.setattr(connector, "_movements", movements)
    snapshot = connector._snapshot(page)
    assert len(snapshot.products) == 6
    assert len({product.external_id for product in snapshot.products}) == 6
    assert len(snapshot.movements) == 16
    assert {movement.source for movement in snapshot.movements} == {
        "credit_card_billed",
        "credit_card_unbilled",
    }
    assert {movement.currency for movement in snapshot.movements} == {"CLP", "USD"}
    assert snapshot.coverage == "visible_portal_history"
    assert all(product.balance is None for product in snapshot.products)


def test_table_extraction_keeps_duplicate_dom_rows(connector):
    page = MagicMock()
    container = page.locator.return_value
    container.count.return_value = 1
    rows = container.locator.return_value
    rows.count.return_value = 2
    row = MagicMock()

    def cell(selector):
        result = MagicMock()
        text = {
            "td.mat-column-date": "20/10/2026",
            "td.mat-column-detail": "Synthetic duplicate",
            "td.mat-column-amount": "14,82",
        }.get(selector)
        result.count.return_value = 1 if text is not None else 0
        result.inner_text.return_value = text
        return result

    row.locator.side_effect = cell
    rows.all.return_value = [row, row]
    product = _product("card", "1234", "USD")
    result = connector._movements(page, product, "credit_card_billed")
    assert len(result) == 2
    assert result[0] == result[1]
    assert result[0].amount == Decimal("-14.82")


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://openbanking.santander.cl/account_balances_transactions_and_withholdings_retail/v1/current-accounts/transactions",
            True,
        ),
        ("https://api-dsk.santander.cl/perdsk/tarjetasDeCredito/consultaUltimosMovimientos", True),
        ("https://api-dsk.santander.cl/perdsk/tarjetasDeCredito/estadoCuentaNacional", True),
        ("https://api-dsk.santander.cl/perdsk/tarjetasDeCredito/summary", False),
        ("https://example.com/perdsk/tarjetasDeCredito/estadoCuentaNacional", False),
        ("http://api-dsk.santander.cl/perdsk/tarjetasDeCredito/consultaUltimosMovimientos", False),
    ],
)
def test_refresh_wait_only_accepts_bank_transaction_responses(url, expected):
    response = MagicMock(url=url)
    assert SantanderConnector._data_response(response) is expected


def test_failed_bank_response_cannot_produce_success(connector):
    page = MagicMock()
    response = page.expect_response.return_value.__enter__.return_value.value
    response.ok = False
    action = MagicMock()
    with pytest.raises(ConnectorError, match="^bank_data_request_failed$"):
        connector._change_product_view(page, action)
    action.assert_called_once()
