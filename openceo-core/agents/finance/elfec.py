from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from settings import Settings


_AMOUNT_RE = re.compile(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b")
_FULL_DATE_RE = re.compile(r"\b(20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])\b")
_YEAR_MONTH_RE = re.compile(r"\b(20\d{2})[-/](0[1-9]|1[0-2])\b")
_PAREN_LABEL_RE = re.compile(r"\(([^)]+)\)")
_SPACE_RE = re.compile(r"\s+")
_MONTH_NAME_RE = re.compile(
    r"\b(ene(?:ro)?|feb(?:rero)?|mar(?:zo)?|abr(?:il)?|may(?:o)?|jun(?:io)?|jul(?:io)?|"
    r"ago(?:sto)?|sep(?:tiembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?)\s+(20\d{2})\b",
    flags=re.IGNORECASE,
)

_MONTH_NAME_MAP = {
    "ene": 1,
    "enero": 1,
    "feb": 2,
    "febrero": 2,
    "mar": 3,
    "marzo": 3,
    "abr": 4,
    "abril": 4,
    "may": 5,
    "mayo": 5,
    "jun": 6,
    "junio": 6,
    "jul": 7,
    "julio": 7,
    "ago": 8,
    "agosto": 8,
    "sep": 9,
    "septiembre": 9,
    "oct": 10,
    "octubre": 10,
    "nov": 11,
    "noviembre": 11,
    "dic": 12,
    "diciembre": 12,
}


@dataclass(slots=True)
class PendingInvoice:
    dom_index: int
    row_text: str
    label: str | None
    month_key: tuple[int, int] | None
    due_date_key: tuple[int, int, int] | None
    amount_value: Decimal | None
    amount_text: str | None

    @property
    def month_label(self) -> str | None:
        if self.month_key is None:
            return None
        return f"{self.month_key[0]:04d}-{self.month_key[1]:02d}"

    @property
    def due_date_label(self) -> str | None:
        if self.due_date_key is None:
            return None
        return f"{self.due_date_key[0]:04d}-{self.due_date_key[1]:02d}-{self.due_date_key[2]:02d}"


@dataclass(slots=True)
class CardFormFields:
    card_number: Any
    expiry: Any
    cvc: Any


def parse_pending_invoice_text(row_text: str, dom_index: int) -> PendingInvoice:
    normalized = _normalize_space(row_text)
    full_date_positions = {match.start(): match for match in _FULL_DATE_RE.finditer(normalized)}
    full_dates = [
        (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for match in full_date_positions.values()
    ]
    billing_months = [
        (int(match.group(1)), int(match.group(2)))
        for match in _YEAR_MONTH_RE.finditer(normalized)
        if match.start() not in full_date_positions
    ]

    if not billing_months:
        month_name_match = _MONTH_NAME_RE.search(normalized)
        if month_name_match is not None:
            month_key = _MONTH_NAME_MAP.get(month_name_match.group(1).lower())
            if month_key is not None:
                billing_months = [(int(month_name_match.group(2)), month_key)]

    label_match = _PAREN_LABEL_RE.search(normalized)
    label = label_match.group(1).strip() if label_match else None

    amount_text = None
    amount_value = None
    amount_match = _AMOUNT_RE.search(normalized)
    if amount_match is not None:
        amount_text = amount_match.group(0)
        amount_value = _parse_amount(amount_text)

    return PendingInvoice(
        dom_index=dom_index,
        row_text=normalized,
        label=label,
        month_key=billing_months[0] if billing_months else None,
        due_date_key=full_dates[0] if full_dates else None,
        amount_value=amount_value,
        amount_text=amount_text,
    )


def select_oldest_invoice(rows: list[str]) -> tuple[PendingInvoice, list[PendingInvoice]]:
    invoices = [parse_pending_invoice_text(row, index) for index, row in enumerate(rows)]
    if not invoices:
        raise ValueError("No encontré facturas pendientes para seleccionar.")

    oldest = min(invoices, key=_invoice_sort_key)
    remaining = [invoice for invoice in invoices if invoice.dom_index != oldest.dom_index]
    remaining.sort(key=_invoice_sort_key)
    return oldest, remaining


def run_elfec_house_payment_practice(
    settings: Settings,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow = workflow or {}
    username = (settings.ELFEC_HOUSE_USERNAME or "").strip()
    password = (settings.ELFEC_HOUSE_PASSWORD or "").strip()
    if not username or not password:
        raise RuntimeError(
            "Faltan credenciales ELFEC_HOUSE_USERNAME/ELFEC_HOUSE_PASSWORD en openceo-core/.env."
        )

    artifact_dir = _prepare_artifact_dir(settings, workflow_id=str(workflow.get("id") or "pago_elfec_house"))
    result: dict[str, Any] = {
        "workflow_id": str(workflow.get("id") or "pago_elfec_house"),
        "workflow_name": str(workflow.get("name") or "Pago Elfec House"),
        "practice_mode": True,
        "payment_state": "started",
        "reached_card_form": False,
        "artifact_dir": str(artifact_dir),
        "selected_invoice": None,
        "remaining_invoices": [],
        "remaining_count": 0,
        "total_selected_amount": None,
        "total_remaining_amount": None,
        "total_pending_amount": None,
        "telegram_preview": "",
        "summary": "",
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright no está instalado en openceo-core. Agrega la dependencia y ejecuta "
            "`python -m playwright install --with-deps chromium`."
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=settings.FINANCE_BROWSER_HEADLESS,
                slow_mo=settings.FINANCE_BROWSER_SLOW_MO_MS,
            )
            context = browser.new_context(ignore_https_errors=True, locale="es-BO")
            page = context.new_page()
            page.set_default_timeout(settings.ELFEC_TIMEOUT_MS)

            try:
                _login(page, settings)
                page.goto(settings.ELFEC_PAYMENT_URL, wait_until="domcontentloaded")
                _save_artifact(page, artifact_dir / "01-pagos-add.png")

                checkboxes, invoices = _collect_pending_invoices(page)
                if not invoices:
                    raise RuntimeError("La sección de facturas pendientes no devolvió filas seleccionables.")

                expected_label = str(workflow.get("expected_row_label") or "").strip().lower()
                selected, remaining = _pick_oldest_visible_invoice(invoices)
                if expected_label and selected.label and expected_label not in selected.label.lower():
                    raise RuntimeError(
                        f"La factura más antigua encontrada pertenece a '{selected.label}', no a '{expected_label}'."
                    )

                checkboxes[selected.dom_index].scroll_into_view_if_needed()
                checkboxes[selected.dom_index].check(force=True)

                dom_order = sorted(invoices, key=lambda item: item.dom_index)
                selected_visible_position = next(
                    index + 1
                    for index, invoice in enumerate(dom_order)
                    if invoice.dom_index == selected.dom_index
                )

                _click_pago_tarjeta(page)
                _save_artifact(page, artifact_dir / "02-despues-pago-tarjeta.png")
                card_fields = _wait_for_card_form(page)
                _save_artifact(page, artifact_dir / "03-card-form.png")

                # Tramo final preparado pero deshabilitado a propósito.
                # Se habilita cuando exista conectividad estable al sitio y validación manual del flujo real.
                #
                # _fill_card_form(
                #     card_fields=card_fields,
                #     card_number=settings.ELFEC_CARD_NUMBER,
                #     expiry=settings.ELFEC_CARD_EXPIRY,
                #     cvc=os.environ["ELFEC_CARD_CVV_ONESHOT"],
                # )
                # _click_confirmar_pago(page)
                # _wait_for_payment_result(page)

                selected_amount = selected.amount_value
                remaining_amount = _sum_amounts(remaining)
                result.update(
                    {
                        "payment_state": "practice_stopped_before_card",
                        "reached_card_form": True,
                        "selected_invoice": _serialize_invoice(
                            selected,
                            visible_position=selected_visible_position,
                        ),
                        "remaining_invoices": [
                            _serialize_invoice(invoice, visible_position=index + 1)
                            for index, invoice in enumerate(sorted(remaining, key=_invoice_sort_key))
                        ],
                        "remaining_count": len(remaining),
                        "total_selected_amount": _format_amount(selected_amount),
                        "total_remaining_amount": _format_amount(remaining_amount),
                        "total_pending_amount": _format_amount(
                            (selected_amount or Decimal("0")) + remaining_amount
                        ),
                    }
                )
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        result["payment_state"] = "error"
        result["error"] = str(exc)
        _write_result(artifact_dir / "summary.json", result)
        raise RuntimeError(f"{exc} Artefactos: {artifact_dir}") from exc

    result["telegram_preview"] = _build_telegram_preview(result)
    result["summary"] = _build_summary(result)
    _write_result(artifact_dir / "summary.json", result)
    return result


def _login(page: Any, settings: Settings) -> None:
    page.goto(settings.ELFEC_LOGIN_URL, wait_until="domcontentloaded")
    username = _first_visible(
        page,
        [
            lambda scope: scope.get_by_role("textbox", name=re.compile("Usuario|correo", re.IGNORECASE)),
            lambda scope: scope.locator("input[type='email']"),
            lambda scope: scope.locator("input[name='username'], input[name='email']"),
        ],
        "campo de usuario ELFEC",
        settings.ELFEC_TIMEOUT_MS,
    )
    password = _first_visible(
        page,
        [
            lambda scope: scope.get_by_role("textbox", name=re.compile("Contrase", re.IGNORECASE)),
            lambda scope: scope.locator("input[type='password']"),
            lambda scope: scope.locator("input[name='password']"),
        ],
        "campo de contraseña ELFEC",
        settings.ELFEC_TIMEOUT_MS,
    )
    username.fill(settings.ELFEC_HOUSE_USERNAME)
    password.fill(settings.ELFEC_HOUSE_PASSWORD)
    login_button = _first_visible(
        page,
        [
            lambda scope: scope.get_by_role("button", name=re.compile("Iniciar sesi", re.IGNORECASE)),
            lambda scope: scope.locator("button[type='submit']"),
            lambda scope: scope.locator("input[type='submit']"),
        ],
        "botón de inicio de sesión ELFEC",
        settings.ELFEC_TIMEOUT_MS,
    )
    login_button.click()
    page.wait_for_load_state("domcontentloaded")


def _collect_pending_invoices(page: Any) -> tuple[list[Any], list[PendingInvoice]]:
    search_root = _pending_invoice_root(page)
    raw_checkboxes = search_root.locator("input[type='checkbox']")
    total = raw_checkboxes.count()
    invoices: list[PendingInvoice] = []
    checkboxes: list[Any] = []
    for index in range(total):
        checkbox = raw_checkboxes.nth(index)
        if not checkbox.is_visible():
            continue
        if checkbox.is_disabled():
            continue
        row_text = _extract_row_text(checkbox)
        invoice = parse_pending_invoice_text(row_text, len(checkboxes))
        if invoice.amount_value is None and invoice.month_key is None and invoice.due_date_key is None:
            continue
        invoices.append(invoice)
        checkboxes.append(checkbox)
    return checkboxes, invoices


def _pick_oldest_visible_invoice(invoices: list[PendingInvoice]) -> tuple[PendingInvoice, list[PendingInvoice]]:
    if not invoices:
        raise RuntimeError("No hay facturas pendientes visibles.")
    selected = min(invoices, key=_invoice_sort_key)
    remaining = [invoice for invoice in invoices if invoice.dom_index != selected.dom_index]
    remaining.sort(key=_invoice_sort_key)
    return selected, remaining


def _pending_invoice_root(page: Any) -> Any:
    anchor = page.get_by_text(re.compile("facturas pendientes", re.IGNORECASE))
    if anchor.count():
        candidate = anchor.first.locator(
            "xpath=ancestor::*[self::section or self::div or self::form or self::fieldset or self::table][1]"
        )
        if candidate.count():
            return candidate.first
    return page


def _extract_row_text(checkbox: Any) -> str:
    row = checkbox.locator("xpath=ancestor::tr[1]")
    if row.count():
        text = row.first.inner_text().strip()
        if text:
            return _normalize_space(text)

    container = checkbox.locator(
        "xpath=ancestor::*[self::div or self::li or self::label or self::section][1]"
    )
    if container.count():
        text = container.first.inner_text().strip()
        if text:
            return _normalize_space(text)

    text = checkbox.evaluate(
        "el => (el.closest('tr, div, li, label, section')?.innerText || el.getAttribute('aria-label') || '')"
    )
    return _normalize_space(str(text or ""))


def _click_pago_tarjeta(page: Any) -> None:
    button = _first_visible(
        page,
        [
            lambda scope: scope.get_by_role("link", name=re.compile("Pago Tarjeta", re.IGNORECASE)),
            lambda scope: scope.get_by_role("button", name=re.compile("Pago Tarjeta", re.IGNORECASE)),
            lambda scope: scope.locator("a, button").filter(has_text=re.compile("Pago Tarjeta", re.IGNORECASE)),
        ],
        "botón Pago Tarjeta",
        15000,
    )
    button.click()


def _wait_for_card_form(page: Any) -> CardFormFields:
    scopes = [page, *page.frames]
    card_field = _first_visible_in_scopes(
        scopes,
        [
            lambda scope: scope.get_by_role("textbox", name=re.compile("Tarjeta", re.IGNORECASE)),
            lambda scope: scope.locator("input[placeholder*='Tarjeta'], input[name*='card']"),
        ],
        "campo de número de tarjeta",
        30000,
    )
    expiry_field = _first_visible_in_scopes(
        scopes,
        [
            lambda scope: scope.get_by_role("textbox", name=re.compile("MM|Venc", re.IGNORECASE)),
            lambda scope: scope.locator("input[placeholder*='MM'], input[name*='exp']"),
        ],
        "campo de vencimiento",
        10000,
    )
    cvc_field = _first_visible_in_scopes(
        scopes,
        [
            lambda scope: scope.get_by_role("textbox", name=re.compile("CVC|CVV", re.IGNORECASE)),
            lambda scope: scope.locator("input[placeholder*='CVC'], input[name*='cvc'], input[name*='cvv']"),
        ],
        "campo CVC",
        10000,
    )
    return CardFormFields(card_number=card_field, expiry=expiry_field, cvc=cvc_field)


def _fill_card_form(
    *,
    card_fields: CardFormFields,
    card_number: str,
    expiry: str,
    cvc: str,
) -> None:
    card_fields.card_number.fill(card_number)
    card_fields.expiry.fill(expiry)
    card_fields.cvc.fill(cvc)


def _click_confirmar_pago(page: Any) -> None:
    button = _first_visible(
        page,
        [
            lambda scope: scope.get_by_role("button", name=re.compile("Confirmar Pago", re.IGNORECASE)),
            lambda scope: scope.locator("button, input[type='submit']").filter(
                has_text=re.compile("Confirmar Pago", re.IGNORECASE)
            ),
        ],
        "botón Confirmar Pago",
        15000,
    )
    button.click()


def _wait_for_payment_result(page: Any) -> None:
    # Placeholder deliberado para la sesión futura con VPN.
    # Aquí se validará éxito/error del pago y luego el regreso al comercio.
    page.wait_for_load_state("domcontentloaded")


def _first_visible(scope: Any, builders: list[Any], description: str, timeout_ms: int) -> Any:
    found = _first_visible_in_scopes([scope], builders, description, timeout_ms)
    if found is None:
        raise RuntimeError(f"No encontré {description}.")
    return found


def _first_visible_in_scopes(
    scopes: list[Any],
    builders: list[Any],
    description: str,
    timeout_ms: int,
) -> Any | None:
    if not scopes:
        return None

    per_try = max(1000, int(timeout_ms / max(len(scopes) * max(len(builders), 1), 1)))
    for scope in scopes:
        for builder in builders:
            locator = builder(scope).first
            try:
                locator.wait_for(state="visible", timeout=per_try)
                return locator
            except Exception:
                continue
    raise RuntimeError(f"No encontré {description}.")


def _serialize_invoice(invoice: PendingInvoice, visible_position: int) -> dict[str, Any]:
    return {
        "label": invoice.label,
        "month": invoice.month_label,
        "due_date": invoice.due_date_label,
        "amount": _format_amount(invoice.amount_value),
        "row_text": invoice.row_text,
        "visible_checkbox_position": visible_position,
    }


def _invoice_sort_key(invoice: PendingInvoice) -> tuple[int, int, int, int, int]:
    if invoice.month_key is not None:
        return (0, invoice.month_key[0], invoice.month_key[1], 1, invoice.dom_index)
    if invoice.due_date_key is not None:
        return (1, invoice.due_date_key[0], invoice.due_date_key[1], invoice.due_date_key[2], invoice.dom_index)
    return (2, 9999, 12, 31, invoice.dom_index)


def _parse_amount(raw: str) -> Decimal | None:
    cleaned = raw.strip()
    if not cleaned:
        return None

    decimal_sep = "."
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
    elif "," in cleaned and "." not in cleaned:
        decimal_sep = ","

    if decimal_sep == ",":
        normalized = cleaned.replace(".", "").replace(",", ".")
    else:
        normalized = cleaned.replace(",", "")

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _sum_amounts(invoices: list[PendingInvoice]) -> Decimal:
    total = Decimal("0")
    for invoice in invoices:
        if invoice.amount_value is not None:
            total += invoice.amount_value
    return total


def _format_amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def _build_summary(result: dict[str, Any]) -> str:
    selected = result.get("selected_invoice") or {}
    remaining = result.get("remaining_invoices") or []
    selected_month = selected.get("month") or "mes no detectado"
    selected_amount = selected.get("amount") or "monto no detectado"
    lines = [
        "Pago Elfec House verificado hasta la pantalla de tarjeta.",
        f"Seleccionada la factura más antigua: {selected_month} por Bs. {selected_amount}.",
        f"Checkbox visible marcado: #{selected.get('visible_checkbox_position') or '?'}.",
        f"Facturas pendientes restantes: {len(remaining)}.",
    ]
    if remaining:
        pending_text = ", ".join(
            f"{item.get('month') or 'mes no detectado'} (Bs. {item.get('amount') or 'n/d'})"
            for item in remaining
        )
        lines.append(f"Pendientes: {pending_text}.")
    lines.append("No se ingresó tarjeta ni se confirmó el pago.")
    return " ".join(lines)


def _build_telegram_preview(result: dict[str, Any]) -> str:
    selected = result.get("selected_invoice") or {}
    remaining = result.get("remaining_invoices") or []
    selected_month = selected.get("month") or "mes no detectado"
    selected_amount = selected.get("amount") or "n/d"
    remaining_lines = [
        f"- {item.get('month') or 'mes no detectado'}: Bs. {item.get('amount') or 'n/d'}"
        for item in remaining
    ]
    base_lines = [
        "Pago Elfec House",
        "Estado: practica detenida antes de tarjeta",
        f"Seleccionada: {selected_month} - Bs. {selected_amount}",
        f"Quedan {len(remaining)} factura(s)",
    ]
    return "\n".join(base_lines + remaining_lines)


def _prepare_artifact_dir(settings: Settings, workflow_id: str) -> Path:
    base = Path(settings.FINANCE_ARTIFACTS_DIR).expanduser()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    path = base / workflow_id / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_artifact(page: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_space(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "")).strip()
