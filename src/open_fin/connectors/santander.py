"""Bounded, read-only Santander Chile portal adapter.

The selectors and navigation flow are adapted from Fintself (see third_party).
Only the current portal views are read. Live bank compatibility is unverified.
No persistent browser profile, tracing, screenshots or bank-response logging is used.
"""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from open_fin.models import (
    BankMovement,
    BankProduct,
    ConnectorError,
    Credentials,
    Currency,
    Snapshot,
    Source,
)

LOGIN_URL = "https://banco.santander.cl/personas"
DASHBOARD_URL = (
    "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/#/private/home/main/resumen"
)
CARD_URLS: dict[Source, str] = {
    "credit_card_unbilled": (
        "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/#/private/Saldos_TC/main/bill"
    ),
    "credit_card_billed": (
        "https://mibanco.santander.cl/UI.Web.HB/Private_new/frame/#/private/Saldos_TC/main/billed"
    ),
}
EMPTY_MOVEMENTS = re.compile(r"no tienes movimientos|sin movimientos", re.I)
MISSING_AMOUNT = {"", "-", "—", "–"}


def parse_amount(value: str, currency: Currency) -> Decimal:
    """Parse locale-formatted amounts without binary floats or zero fallbacks."""
    text = re.sub(r"\s+", "", value).upper()
    if not text or (currency == "CLP" and re.search(r"USD|US\$", text)):
        raise ConnectorError("invalid_amount")
    if currency == "USD" and "CLP" in text:
        raise ConnectorError("invalid_amount")
    text = re.sub(r"CLP|USD|US\$|\$", "", text)
    negative = text.startswith("-")
    if text.startswith(("-", "+")):
        text = text[1:]
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", text):
        raise ConnectorError("invalid_amount")

    separators = {character for character in text if character in ".,"}
    fractional = ""
    if len(separators) == 2:
        decimal_separator = "." if text.rfind(".") > text.rfind(",") else ","
        grouping_separator = "," if decimal_separator == "." else "."
        integer, fractional = text.rsplit(decimal_separator, 1)
        if decimal_separator in integer or not re.fullmatch(
            rf"\d{{1,3}}(?:{re.escape(grouping_separator)}\d{{3}})+", integer
        ):
            raise ConnectorError("invalid_amount")
        integer = integer.replace(grouping_separator, "")
    elif separators:
        separator = next(iter(separators))
        chunks = text.split(separator)
        # Three trailing digits mean a thousands group, including USD 1,234.
        if all(len(chunk) == 3 for chunk in chunks[1:]) and 1 <= len(chunks[0]) <= 3:
            integer = "".join(chunks)
        elif len(chunks) == 2 and 1 <= len(chunks[1]) <= 2:
            integer, fractional = chunks
        else:
            raise ConnectorError("invalid_amount")
    else:
        integer = text
    if fractional and not re.fullmatch(r"\d{1,2}", fractional):
        raise ConnectorError("invalid_amount")
    result = Decimal(integer + ("." + fractional if fractional else ""))
    return -result if negative else result


def parse_date(value: str, reference_date: date | None = None) -> date:
    """Read DD/MM[/YYYY]; a missing year follows the portal's recent-history convention."""
    match = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\s*", value)
    if not match:
        raise ConnectorError("invalid_date")
    today = reference_date or datetime.now(ZoneInfo("America/Santiago")).date()
    day, month = int(match[1]), int(match[2])
    year = int(match[3]) if match[3] else today.year
    if not match[3] and (month, day) > (today.month, today.day):
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        raise ConnectorError("invalid_date") from None


def parse_rows(
    rows: list[dict[str, str]],
    product: BankProduct,
    source: Source,
    reference_date: date | None = None,
) -> list[BankMovement]:
    """Normalize rows, retaining each occurrence and its statement source."""
    movements = []
    previous_date = ""
    for row in rows:
        previous_date = row.get("date", "").strip() or previous_date
        description = row.get("description", "").strip()
        if not description:
            raise ConnectorError("invalid_movement_description")
        if source == "credit_card_billed":
            amount = -parse_amount(row.get("amount", ""), product.currency)
        else:
            charge_text = row.get("charge", "").strip()
            payment_text = row.get("payment", "").strip()
            charge = (
                parse_amount(charge_text, product.currency)
                if charge_text not in MISSING_AMOUNT
                else None
            )
            payment = (
                parse_amount(payment_text, product.currency)
                if payment_text not in MISSING_AMOUNT
                else None
            )
            if charge is None and payment is None:
                raise ConnectorError("invalid_amount")
            if charge and payment:
                raise ConnectorError("ambiguous_movement_amount")
            amount = -abs(charge) if charge else abs(payment or Decimal(0))
        movements.append(
            BankMovement(
                product_external_id=product.external_id,
                date=parse_date(previous_date, reference_date),
                description=description,
                amount=amount,
                currency=product.currency,
                source=source,
                bank_movement_id=row.get("bank_movement_id") or None,
                installments=row.get("installments") or None,
            )
        )
    return movements


def _last4(reference: str) -> str:
    digits = re.sub(r"\D", "", reference)
    if len(digits) < 4:
        raise ConnectorError("ambiguous_product_identity")
    return digits[-4:]


def _currency(text: str) -> Currency:
    if re.search(r"d[oó]lar|\bUSD\b|US\$", text, re.I):
        return "USD"
    if re.search(r"peso|\bCLP\b|\$", text, re.I):
        return "CLP"
    raise ConnectorError("ambiguous_product_currency")


def _product(kind: str, reference: str, currency: Currency) -> BankProduct:
    last4 = _last4(reference)
    fingerprint = hashlib.sha256(reference.encode()).hexdigest()[:24]
    return BankProduct(
        external_id=f"santander:{kind}:{fingerprint}:{currency}",
        kind=kind,
        label=f"{'Cuenta' if kind == 'account' else 'Tarjeta'} Santander •••• {last4}",
        last4=last4,
        currency=currency,
        balance=None,
    )


@dataclass(frozen=True)
class _Account:
    reference: str
    currency: Currency


class SantanderConnector:
    def __init__(self, headless: bool = True, timeout_seconds: int = 240):
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self._deadline = 0.0
        self._stage = "starting"

    def _timeout(self, maximum: int = 20_000) -> int:
        remaining = int((self._deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise ConnectorError("connector_timeout")
        return min(maximum, remaining)

    @staticmethod
    def _trusted_url(url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            raise ConnectorError("untrusted_bank_origin") from None
        if (
            parsed.scheme != "https"
            or not (host == "santander.cl" or host.endswith(".santander.cl"))
            or port not in (None, 443)
            or parsed.username is not None
        ):
            raise ConnectorError("untrusted_bank_origin")

    def _goto(self, page: Any, url: str) -> None:
        self._trusted_url(url)
        page.goto(url, wait_until="domcontentloaded", timeout=self._timeout(60_000))
        self._trusted_url(page.url)

    @classmethod
    def _route_bank_request(cls, route: Any) -> None:
        # Bank pages can include third-party scripts. Keep browser HTTP traffic
        # inside Santander's HTTPS origin family, not only the initial login URL.
        try:
            cls._trusted_url(route.request.url)
        except ConnectorError:
            route.abort("blockedbyclient")
        else:
            route.continue_()

    @staticmethod
    def _data_response(response: Any) -> bool:
        """Recognize transaction responses, including observed card service families."""
        parsed = urlparse(response.url)
        if parsed.scheme != "https":
            return False
        if parsed.hostname == "openbanking.santander.cl":
            return parsed.path.endswith("/current-accounts/transactions")
        if parsed.hostname == "api-dsk.santander.cl":
            path = parsed.path.lower()
            return path.startswith("/perdsk/tarjetasdecredito/") and (
                "movimiento" in path or "estadocuenta" in path
            )
        return False

    def _change_product_view(self, page: Any, action: Callable[[], None]) -> None:
        # A changed active tab alone does not establish that its rows refreshed.
        new_requests: set[Any] = set()

        def remember_request(request: Any) -> None:
            new_requests.add(request)

        page.on("request", remember_request)
        try:
            with page.expect_response(
                lambda response: response.request in new_requests and self._data_response(response),
                timeout=self._timeout(30_000),
            ) as pending:
                action()
            response = pending.value
            if not response.ok or response.finished() is not None:
                raise ConnectorError("bank_data_request_failed")
        except PlaywrightTimeoutError:
            raise ConnectorError("bank_data_response_timeout") from None
        finally:
            page.remove_listener("request", remember_request)
        # Allow the response consumer and DOM render to finish before reading rows.
        page.evaluate(
            """() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            })"""
        )
        for indicator in page.locator("mat-progress-spinner, .loading-overlay").all():
            if indicator.is_visible():
                indicator.wait_for(state="hidden", timeout=self._timeout())

    def fetch(self, credentials: Credentials) -> Snapshot:
        self._stage = "starting"
        self._deadline = time.monotonic() + self.timeout_seconds
        browser = None
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(
                        headless=self.headless, chromium_sandbox=True
                    )
                    context = browser.new_context(
                        locale="es-CL", timezone_id="America/Santiago", service_workers="block"
                    )
                    context.route("**/*", self._route_bank_request)
                    page = context.new_page()
                    page.set_default_timeout(self._timeout())
                    self._login(page, credentials)
                    return self._snapshot(page)
                finally:
                    if browser is not None:
                        browser.close()
        except ConnectorError as error:
            raise ConnectorError(error.code, stage=error.stage or self._stage) from None
        except PlaywrightTimeoutError:
            raise ConnectorError("bank_page_timeout", stage=self._stage) from None
        except Exception:
            # Raw Playwright errors can include DOM content, URLs and input values.
            raise ConnectorError("connector_failed", stage=self._stage) from None

    def _login(self, page: Any, credentials: Credentials) -> None:
        self._stage = "login_page"
        self._goto(page, LOGIN_URL)
        page.get_by_role("button", name="Ingresar al sitio privado").click(timeout=self._timeout())
        self._stage = "login_form"
        iframe = page.locator("#login-frame")
        iframe.wait_for(state="visible", timeout=self._timeout())
        frame = iframe.element_handle().content_frame()
        if frame is None:
            raise ConnectorError("missing_login_frame")
        rut_field = frame.get_by_role("textbox", name="RUT", exact=True)
        rut_field.wait_for(state="visible", timeout=self._timeout())
        self._trusted_url(frame.url)
        rut_field.fill(credentials.rut.get_secret_value(), timeout=self._timeout())
        self._trusted_url(frame.url)
        # Password inputs do not necessarily expose the ARIA textbox role.
        password_field = frame.get_by_label("Clave", exact=True)
        if password_field.count() != 1:
            password_field = frame.get_by_role("textbox", name="Clave", exact=True)
        if password_field.count() != 1:
            raise ConnectorError("missing_login_fields")
        password_field.fill(credentials.password.get_secret_value(), timeout=self._timeout())
        self._trusted_url(frame.url)
        self._trusted_url(page.url)
        self._stage = "login_submit"
        frame.get_by_role("button", name="Ingresar", exact=True).click(timeout=self._timeout())
        try:
            page.locator("h3:has-text('Hola')").wait_for(
                state="visible", timeout=self._timeout(90_000)
            )
        except PlaywrightTimeoutError:
            raise ConnectorError("authentication_or_challenge_failed") from None
        self._trusted_url(page.url)

    @staticmethod
    def _visible_message(page: Any, pattern: re.Pattern[str]) -> bool:
        return any(message.is_visible() for message in page.get_by_text(pattern).all())

    def _accounts(self, page: Any) -> list[_Account]:
        self._goto(page, DASHBOARD_URL)
        page.locator("h3:has-text('Hola')").wait_for(state="visible", timeout=self._timeout())
        summaries = page.locator("#cuentas div.box-product")
        try:
            summaries.first.wait_for(state="visible", timeout=self._timeout(10_000))
        except PlaywrightTimeoutError:
            if self._visible_message(
                page, re.compile(r"no tienes cuentas|no cuentas con cuentas", re.I)
            ):
                return []
            raise ConnectorError("missing_account_products") from None
        accounts = []
        for summary in summaries.all():
            paragraphs = summary.locator("div.datos p")
            if paragraphs.count() < 2:
                raise ConnectorError("ambiguous_product_identity")
            reference = re.sub(r"\D", "", paragraphs.nth(1).inner_text())
            _last4(reference)
            account = _Account(reference, _currency(summary.inner_text()))
            if account in accounts:
                raise ConnectorError("ambiguous_product_identity")
            accounts.append(account)
        return accounts

    def _account_movements(self, page: Any, account: _Account) -> list[BankMovement]:
        self._goto(page, DASHBOARD_URL)
        summaries = page.locator("#cuentas div.box-product")
        summaries.first.wait_for(state="visible", timeout=self._timeout())
        candidates = []
        for summary in summaries.all():
            paragraphs = summary.locator("div.datos p")
            if paragraphs.count() >= 2:
                reference = re.sub(r"\D", "", paragraphs.nth(1).inner_text())
                if (
                    reference == account.reference
                    and _currency(summary.inner_text()) == account.currency
                ):
                    candidates.append(summary)
        if len(candidates) != 1:
            raise ConnectorError("ambiguous_product_identity")
        self._change_product_view(page, lambda: candidates[0].click(timeout=self._timeout()))
        page.get_by_text("Mis movimientos", exact=False).first.wait_for(
            state="visible", timeout=self._timeout()
        )
        return self._movements(
            page, _product("account", account.reference, account.currency), "account"
        )

    def _cards(self, page: Any) -> list[str]:
        slides = page.locator("lib-carousel .swiper-slide:not(.swiper-slide-duplicate)")
        try:
            slides.first.wait_for(state="visible", timeout=self._timeout(10_000))
        except PlaywrightTimeoutError:
            if self._visible_message(
                page, re.compile(r"no tienes tarjetas|no cuentas con tarjetas", re.I)
            ):
                return []
            raise ConnectorError("missing_card_products") from None
        cards = []
        for slide in slides.all():
            match = re.search(r"\*+\s*(\d{4})\b", slide.locator("p.product").inner_text())
            if not match or match[1] in cards:
                raise ConnectorError("ambiguous_product_identity")
            cards.append(match[1])
        return cards

    def _select_card(self, page: Any, last4: str) -> None:
        cards = self._cards(page)
        if last4 not in cards:
            raise ConnectorError("missing_card_product")
        # The position only chooses a control. The active product identity must agree.
        active = page.locator("lib-carousel .swiper-slide-active p.product")
        already_active = (
            active.count() == 1 and re.search(rf"\*+\s*{last4}\b", active.inner_text()) is not None
        )
        if not already_active:
            position = cards.index(last4) + 1
            control = page.locator(
                f'lib-carousel .swiper-pagination-bullet[aria-label="Go to slide {position}"]'
            )
            self._change_product_view(
                page, lambda: control.click(force=True, timeout=self._timeout())
            )
        page.wait_for_function(
            """(last4) => {
                const active = document.querySelectorAll(
                    'lib-carousel .swiper-slide-active p.product'
                );
                if (active.length !== 1) return false;
                return /\\*+\\s*(\\d{4})\\b/.exec(active[0].textContent)?.[1] === last4;
            }""",
            arg=last4,
            timeout=self._timeout(),
        )
        self._verify_card(page, last4)

    @staticmethod
    def _verify_card(page: Any, last4: str) -> None:
        active = page.locator("lib-carousel .swiper-slide-active p.product")
        if active.count() != 1 or not re.search(rf"\*+\s*{last4}\b", active.inner_text()):
            raise ConnectorError("ambiguous_product_identity")

    def _card_currencies(self, page: Any) -> list[Currency]:
        currencies: list[Currency] = []
        for currency, label in (("CLP", "Pesos"), ("USD", "Dólares")):
            if page.get_by_role("button", name=label, exact=True).count():
                currencies.append(currency)
        if not currencies:
            raise ConnectorError("missing_currency_controls")
        return currencies

    def _select_currency(self, page: Any, currency: Currency) -> None:
        label = "Dólares" if currency == "USD" else "Pesos"
        toggle = page.locator(f'mat-button-toggle:has-text("{label}")')
        if toggle.count() != 1:
            raise ConnectorError("ambiguous_currency_control")
        if not re.search(r"mat-button-toggle-checked|actived", toggle.get_attribute("class") or ""):
            self._change_product_view(
                page,
                lambda: page.get_by_role("button", name=label, exact=True).click(
                    timeout=self._timeout()
                ),
            )
        page.wait_for_function(
            """(label) => Array.from(document.querySelectorAll('mat-button-toggle'))
                .some(el => el.textContent.trim() === label &&
                    /mat-button-toggle-checked|actived/.test(el.className))""",
            arg=label,
            timeout=self._timeout(),
        )

    def _movements(self, page: Any, product: BankProduct, source: Source) -> list[BankMovement]:
        selector = (
            "div.container-tabla"
            if source == "credit_card_billed"
            else "div.card.table-container.show"
        )
        container = page.locator(f"{selector}:visible")
        try:
            container.first.wait_for(state="visible", timeout=self._timeout())
        except PlaywrightTimeoutError:
            if self._visible_message(page, EMPTY_MOVEMENTS):
                return []
            raise ConnectorError("missing_movement_table") from None
        if container.count() != 1:
            raise ConnectorError("ambiguous_movement_table")
        rows = container.locator("table.mat-table tbody tr.mat-row")
        if not rows.count():
            if self._visible_message(page, EMPTY_MOVEMENTS):
                return []
            raise ConnectorError("missing_movement_rows")
        raw_rows = []
        for row in rows.all():
            self._timeout()
            data = {
                "date": self._cell(row, "td.mat-column-date"),
                "description": self._cell(row, "td.mat-column-detail"),
            }
            if source == "credit_card_billed":
                data["amount"] = self._cell(row, "td.mat-column-amount")
            else:
                data["charge"] = self._cell(row, "td.mat-column-amountCharge")
                data["payment"] = self._cell(row, "td.mat-column-paymentAmount")
            installment = row.locator("td.mat-column-installments, td.mat-column-cuotas")
            if installment.count() == 1:
                data["installments"] = installment.inner_text().strip()
            raw_rows.append(data)
        return parse_rows(raw_rows, product, source)

    def _cell(self, row: Any, selector: str) -> str:
        cell = row.locator(selector)
        if cell.count() != 1:
            raise ConnectorError("missing_movement_column")
        return cell.inner_text(timeout=self._timeout()).strip()

    def _snapshot(self, page: Any) -> Snapshot:
        products: dict[str, BankProduct] = {}
        movements = []
        self._stage = "accounts"
        for account in self._accounts(page):
            product = _product("account", account.reference, account.currency)
            products[product.external_id] = product
            movements.extend(self._account_movements(page, account))
        expected_cards = None
        for source, url in CARD_URLS.items():
            self._stage = "card_unbilled" if source == "credit_card_unbilled" else "card_billed"
            self._goto(page, url)
            cards = self._cards(page)
            if expected_cards is not None and set(cards) != set(expected_cards):
                raise ConnectorError("inconsistent_card_products")
            expected_cards = cards
            for last4 in cards:
                self._goto(page, url)
                self._select_card(page, last4)
                for currency in self._card_currencies(page):
                    self._select_currency(page, currency)
                    product = _product("card", last4, currency)
                    products[product.external_id] = product
                    movements.extend(self._movements(page, product, source))
                    self._verify_card(page, last4)
        return Snapshot(
            products=list(products.values()),
            movements=movements,
            coverage="visible_portal_history",
            warnings=[
                "live_bank_compatibility_unverified",
                "visible_portal_rows_only_no_historical_backfill",
                "balances_unavailable",
                "short_dates_use_recent_history_year_inference",
            ],
        )
