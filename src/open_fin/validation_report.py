"""Private, offline checks and a static owner-review report for one bank sync.

This module only handles snapshots passed by the local validation command. It
does not read credentials, make requests, or claim that portal history is complete.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

_CURRENCIES = {"CLP", "USD"}
_SOURCES = {"account", "credit_card_billed", "credit_card_unbilled"}
_REVIEW_KEYS = ("products", "movements", "statement_sources")


@dataclass(frozen=True)
class ValidationResult:
    """Selected private rows plus diagnostics containing no financial details.

    Only ``counts``, ``checks``, ``structural_ok`` and
    ``excluded_cached_rows`` may be used in terminal status summaries. The
    product and movement lists are private and belong in the local report.
    """

    structural_ok: bool
    counts: dict[str, int]
    checks: list[dict[str, str]]
    accounts: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    movements: list[dict[str, Any]]
    excluded_cached_rows: int


def _timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)  # noqa: UP017 - standalone Python 3.9 CLI
    except (ValueError, OverflowError):
        return None


def _decimal(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return str(parsed) if parsed.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _date(value: Any) -> str:
    try:
        if isinstance(value, datetime):
            return ""
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else ""
    except ValueError:
        return ""


def analyse_snapshot(
    job: dict,
    accounts: list[dict],
    cards: list[dict],
    movements: list[dict],
    *,
    connection_id: str,
) -> ValidationResult:
    """Check one successful extraction, excluding rows left by earlier syncs.

    Dates are compared as instants, so RFC3339 ``Z`` and explicit UTC offsets
    are equivalent. Two similar movements with different IDs remain separate.
    Checks use fixed messages and never interpolate bank data or job errors.
    """

    checks: list[dict[str, str]] = []

    def check(code: str, status: str, message: str) -> None:
        checks.append({"code": code, "status": status, "message": message})

    succeeded = job.get("status") == "succeeded"
    matching_connection = bool(connection_id) and job.get("connection_id") == connection_id
    finished = _timestamp(job.get("finished_at"))
    check(
        "sync_succeeded",
        "pass" if succeeded else "fail",
        "La sincronización terminó correctamente."
        if succeeded
        else "La sincronización no terminó correctamente; no se valida su extracción.",
    )
    check(
        "connection_matches",
        "pass" if matching_connection else "fail",
        "La sincronización corresponde a la conexión seleccionada."
        if matching_connection
        else "La sincronización no corresponde a la conexión seleccionada.",
    )
    check(
        "snapshot_timestamp",
        "pass" if finished else "fail",
        "La extracción tiene una fecha de término válida."
        if finished
        else "La fecha de término de la extracción no es válida.",
    )

    excluded = 0
    invalid_timestamp = False
    foreign_connection = False
    newer_rows = False

    def current(rows: list[dict]) -> list[dict]:
        nonlocal excluded, invalid_timestamp, foreign_connection, newer_rows
        selected = []
        if not (succeeded and matching_connection and finished):
            return selected
        for row in rows:
            timestamp = _timestamp(row.get("last_sync_at"))
            if row.get("connection_id") != connection_id:
                foreign_connection = True
                continue
            if timestamp is None:
                invalid_timestamp = True
                continue
            if timestamp != finished:
                excluded += 1
                newer_rows = newer_rows or timestamp > finished
                continue
            selected.append(row)
        return selected

    account_rows, card_rows, movement_rows = current(accounts), current(cards), current(movements)
    if excluded:
        check(
            "cached_rows_excluded",
            "warning",
            "Se excluyeron registros de otras sincronizaciones; no prueban la extracción actual.",
        )
    for condition, code, message in (
        (
            invalid_timestamp,
            "invalid_row_timestamp",
            "Hay registros con fecha de extracción inválida.",
        ),
        (foreign_connection, "foreign_connection", "Hay registros de una conexión distinta."),
        (
            newer_rows,
            "newer_snapshot_present",
            "Hay datos de una sincronización posterior; vuelva a validar una extracción estable.",
        ),
    ):
        if condition:
            check(code, "fail", message)

    products: dict[str, dict] = {}
    invalid_products = False
    selected_accounts: list[dict[str, Any]] = []
    selected_cards: list[dict[str, Any]] = []
    for expected_kind, rows, selected in (
        ("account", account_rows, selected_accounts),
        ("card", card_rows, selected_cards),
    ):
        for row in rows:
            product_id = _text(row.get("id"))
            last4 = _text(row.get("last4"))
            balance = _decimal(row.get("balance"))
            valid = (
                bool(product_id)
                and product_id not in products
                and row.get("kind") == expected_kind
                and _text(row.get("currency")) in _CURRENCIES
                and re.fullmatch(r"[0-9]{4}", last4) is not None
                and (row.get("balance") is None or balance is not None)
            )
            invalid_products = invalid_products or not valid
            selected_row = {
                "id": product_id,
                "kind": _text(row.get("kind")),
                "last4": last4 if re.fullmatch(r"[0-9]{4}", last4) else "????",
                "currency": _text(row.get("currency")),
                "balance": balance,
            }
            selected.append(selected_row)
            if product_id and product_id not in products:
                products[product_id] = selected_row
    check(
        "products_valid",
        "fail" if invalid_products else "pass",
        "Los productos tienen identidad única, tipo, moneda y saldo válidos."
        if not invalid_products
        else "Hay productos con identidad duplicada, tipo, moneda, dígitos o saldo inválidos.",
    )
    any_products = bool(account_rows or card_rows)
    check(
        "products_present",
        "pass" if any_products else "fail",
        "Se obtuvieron productos en esta extracción."
        if any_products
        else "No se obtuvieron productos en esta extracción.",
    )

    movement_ids: set[str] = set()
    selected_movements: list[dict[str, Any]] = []
    invalid_movements = False
    for row in movement_rows:
        movement_id = _text(row.get("id"))
        product_id = _text(row.get("product_id"))
        product = products.get(product_id)
        amount = _decimal(row.get("amount"))
        source = _text(row.get("source"))
        movement_date = _date(row.get("date"))
        expected_kind = "account" if source == "account" else "card"
        valid = (
            bool(movement_id)
            and movement_id not in movement_ids
            and product is not None
            and product.get("kind") == expected_kind
            and product.get("currency") == row.get("currency")
            and _text(row.get("currency")) in _CURRENCIES
            and source in _SOURCES
            and amount is not None
            and bool(movement_date)
            and bool(_text(row.get("description")).strip())
        )
        invalid_movements = invalid_movements or not valid
        movement_ids.add(movement_id)
        selected_movements.append(
            {
                "id": movement_id,
                "product_id": product_id,
                "date": movement_date,
                "description": _text(row.get("description")),
                "amount": amount,
                "currency": _text(row.get("currency")),
                "source": _text(source),
                "installments": _text(row.get("installments")),
            }
        )
    check(
        "movements_valid",
        "fail" if invalid_movements else "pass",
        "Los movimientos tienen identidad única, producto, origen, fecha e importe válidos."
        if not invalid_movements
        else "Hay movimientos con identidad, producto, origen, moneda, fecha o importe inválidos.",
    )
    counts = {
        "accounts": len(selected_accounts),
        "cards": len({row["last4"] for row in selected_cards if row["last4"] != "????"}),
        "card_currency_products": len(selected_cards),
        "movements": len(selected_movements),
        "movements_account": sum(row["source"] == "account" for row in selected_movements),
        "movements_billed": sum(
            row["source"] == "credit_card_billed" for row in selected_movements
        ),
        "movements_unbilled": sum(
            row["source"] == "credit_card_unbilled" for row in selected_movements
        ),
    }
    missing = (
        (not selected_accounts, "no_accounts", "No se extrajeron cuentas; confirme sus productos."),
        (not selected_cards, "no_cards", "No se extrajeron tarjetas; confirme sus productos."),
        (
            bool(selected_accounts) and not counts["movements_account"],
            "no_account_movements",
            "No hay movimientos de cuentas; confirme el período visible en Santander.",
        ),
        (
            bool(selected_cards) and not counts["movements_billed"],
            "no_billed_movements",
            "No hay movimientos facturados; confirme el período visible en Santander.",
        ),
        (
            bool(selected_cards) and not counts["movements_unbilled"],
            "no_unbilled_movements",
            "No hay movimientos por facturar; confirme el período visible en Santander.",
        ),
    )
    for condition, code, message in missing:
        if condition:
            check(code, "warning", message)
    if job.get("warnings"):
        check(
            "connector_warnings",
            "warning",
            "El conector informó limitaciones; una extracción correcta requiere revisión manual.",
        )
    check(
        "history_coverage",
        "warning",
        "La revisión cubre solo la extracción actual y el historial visible; "
        "no prueba historial completo.",
    )
    check(
        "balance_coverage",
        "warning",
        "Un saldo no disponible no equivale a cero; confirme los saldos y cupos en Santander.",
    )
    return ValidationResult(
        structural_ok=all(item["status"] != "fail" for item in checks),
        counts=counts,
        checks=checks,
        accounts=selected_accounts,
        cards=selected_cards,
        movements=selected_movements,
        excluded_cached_rows=excluded,
    )


def render_report(result: ValidationResult, review: dict[str, str] | None = None) -> str:
    """Render a standalone Spanish HTML file with no scripts or remote content."""

    def escape(value: Any) -> str:
        return html.escape(str(value), quote=True)

    review = review or {}
    discrepancy = not result.structural_ok or any(review.get(key) == "no" for key in _REVIEW_KEYS)
    verified = result.structural_ok and all(review.get(key) == "yes" for key in _REVIEW_KEYS)
    state = "discrepancy" if discrepancy else "verified_by_owner" if verified else "needs_review"
    state_labels = {
        "discrepancy": "Discrepancias detectadas en la validación",
        "verified_by_owner": "Verificado por el titular",
        "needs_review": "Pendiente de revisión por el titular",
    }
    parts = [
        '<!doctype html><html lang="es"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'\">",
        '<meta name="referrer" content="no-referrer">',
        "<title>Open Fin · Revisión privada de Santander</title>",
        "<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:36px auto;"
        "padding:0 20px;color:#152a35;background:#fafcfd}h1,h2{line-height:1.2}"
        "table{border-collapse:collapse;width:100%;margin:16px 0 28px}"
        "th,td{text-align:left;padding:10px;border-bottom:1px solid #cbd5dc;"
        "overflow-wrap:anywhere}th{background:#eaf0f4}small{color:#425966}"
        ".notice{padding:16px;background:#eaf0f4;border-radius:8px}"
        ".table-wrap{overflow-x:auto}.number{white-space:nowrap}"
        "</style></head><body><h1>Revisión privada de Santander</h1>",
        "<p>Compare este informe con sus productos y movimientos en el portal de Santander. "
        "Los datos permanecen en este archivo local; contiene información financiera privada.</p>",
        f'<p class="notice" data-review-status="{state}"><strong>{state_labels[state]}</strong>'
        "<br>La validación automática comprueba estructura y consistencia. "
        "Solo el titular puede confirmar que la extracción coincide con el banco.</p>",
        "<h2>Comprobación automática</h2>",
        "<p><strong>"
        + (
            "Estructura correcta"
            if result.structural_ok
            else "Se encontraron errores de estructura"
        )
        + "</strong></p><ul>",
    ]
    status_labels = {"pass": "Correcto", "fail": "Error", "warning": "Revisar"}
    for item in result.checks:
        status = status_labels.get(item.get("status", ""), "Revisar")
        parts.append(f"<li><strong>{status}:</strong> {escape(item.get('message', ''))}</li>")
    parts.append("</ul><h2>Revisión del titular</h2><ul>")
    review_labels = {
        "products": "Cuentas, tarjetas y monedas coinciden con Santander",
        "movements": "Movimientos, fechas e importes coinciden con el período revisado",
        "statement_sources": "Facturados y por facturar se corresponden con sus respectivas vistas",
    }
    answers = {"yes": "Sí", "no": "No; hay diferencias", "unsure": "Sin confirmar"}
    for key in _REVIEW_KEYS:
        parts.append(
            f"<li>{review_labels[key]}: <strong>"
            f"{answers.get(review.get(key, ''), 'Pendiente')}</strong></li>"
        )
    parts.append("</ul>")

    def product_label(product: dict | None) -> str:
        if not product:
            return "Producto no identificado"
        kind = "Cuenta" if product.get("kind") == "account" else "Tarjeta"
        return f"{kind} •••• {product.get('last4', '????')}"

    for title, products in (("Cuentas", result.accounts), ("Tarjetas", result.cards)):
        parts.append(f"<h2>{title}</h2>")
        if not products:
            parts.append("<p>No se obtuvieron productos de este tipo en la extracción actual.</p>")
            continue
        parts.append(
            '<div class="table-wrap"><table><thead><tr><th>Producto</th>'
            "<th>Moneda</th><th>Saldo informado</th></tr></thead><tbody>"
        )
        for row in products:
            balance = row.get("balance")
            balance_label = "No disponible" if balance is None else str(balance)
            parts.append(
                f"<tr><td>{escape(product_label(row))}</td>"
                f"<td>{escape(row.get('currency', ''))}</td>"
                f'<td class="number">{escape(balance_label)}</td></tr>'
            )
        parts.append("</tbody></table></div>")

    parts.append("<h2>Movimientos de la extracción actual</h2>")
    if not result.movements:
        parts.append(
            "<p>No se obtuvieron movimientos. Confirme el período visible en Santander.</p>"
        )
    else:
        by_id = {row["id"]: row for row in result.accounts + result.cards}
        source_labels = {
            "account": "Cuenta",
            "credit_card_billed": "Tarjeta · Facturado",
            "credit_card_unbilled": "Tarjeta · Por facturar",
        }
        parts.append(
            '<div class="table-wrap"><table><thead><tr><th>Producto</th><th>Fecha</th>'
            "<th>Descripción</th><th>Importe</th><th>Moneda</th><th>Origen</th>"
            "<th>Cuotas</th></tr></thead><tbody>"
        )
        for row in result.movements:
            cells = (
                product_label(by_id.get(row.get("product_id"))),
                row.get("date") or "Fecha no válida",
                row.get("description", ""),
                row.get("amount") if row.get("amount") is not None else "Importe no válido",
                row.get("currency", ""),
                source_labels.get(row.get("source", ""), "Origen no válido"),
                row.get("installments") or "—",
            )
            parts.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>")
        parts.append("</tbody></table></div>")
    parts.append(
        "<p><small>Los importes conservan su representación decimal. "
        "Los últimos cuatro dígitos identifican productos para la revisión; "
        "una tarjeta puede tener productos separados por moneda. "
        "Este informe no confirma historial completo ni cobertura de todos los saldos."
        "</small></p></body></html>"
    )
    return "".join(parts)
