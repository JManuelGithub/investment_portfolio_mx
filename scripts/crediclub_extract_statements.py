
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pandas as pd

from crediclub_utils import (
    detect_layout,
    filename_period,
    list_pdfs,
    load_config,
    normalize_text,
    open_pdf,
    parse_compact_date,
    parse_money,
    parse_percent,
    parse_period,
    project_path,
    row_text,
    safe_round,
    words_by_row,
    write_table,
)


CREDICLUB_PARSER_VERSION = "2026-11-08.1"
TOLERANCE_MXN = 1.00


def _extract_common_header(doc, layout: str) -> dict:
    page1 = doc[0]
    text = page1.get_text("text")

    if layout == "modern_2026_06_plus":
        period_match = re.search(
            r"Periodo:\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        account_match = re.search(
            r"N[úu]m\.\s*de\s*Cuenta:\s*([0-9]+)",
            text,
            flags=re.IGNORECASE,
        )
        clabe_match = re.search(
            r"CLABE:\s*([0-9]{18})",
            text,
            flags=re.IGNORECASE,
        )
        rfc_match = re.search(
            r"RFC:\s*([A-Z0-9]{12,13})",
            text,
            flags=re.IGNORECASE,
        )

        if not period_match or not account_match:
            raise RuntimeError("Could not parse modern Crediclub statement header.")

        period_start, period_end = parse_period(period_match.group(1))

        return {
            "statement_start": period_start,
            "statement_end": period_end,
            "statement_account_number": account_match.group(1),
            "clabe": clabe_match.group(1) if clabe_match else None,
            "rfc_in_statement": rfc_match.group(1) if rfc_match else None,
            "currency": "MXN",
        }

    # Legacy: the page text extraction is not in visual reading order, but the
    # important identifiers can still be located reliably by exact patterns.
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    period_line = next(
        (
            line for line in lines
            if re.search(
                r"\d{1,2}\s+al\s+\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóú]+\s+del?\s+\d{4}",
                line,
                flags=re.IGNORECASE,
            )
        ),
        None,
    )
    if not period_line:
        raise RuntimeError("Could not locate legacy Crediclub statement period.")

    period_start, period_end = parse_period(period_line)

    clabe = next(
        (line for line in lines if re.fullmatch(r"\d{18}", line)),
        None,
    )

    # Account number in the observed legacy format is a standalone numeric
    # token distinct from the 18-digit CLABE.
    numeric_lines = [
        line for line in lines
        if re.fullmatch(r"\d{7,12}", line)
    ]
    account_number = numeric_lines[0] if numeric_lines else None

    rfc = next(
        (
            line for line in lines
            if re.fullmatch(r"[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}", line.upper())
        ),
        None,
    )

    if not account_number:
        raise RuntimeError("Could not parse legacy Crediclub account number.")

    return {
        "statement_start": period_start,
        "statement_end": period_end,
        "statement_account_number": account_number,
        "clabe": clabe,
        "rfc_in_statement": rfc,
        "currency": "MXN",
    }


def _money_from_row(
    row: list[tuple],
    *,
    x_min: float | None = None,
    x_max: float | None = None,
) -> float | None:
    values = []
    for word in row:
        x = word[0]
        if x_min is not None and x < x_min:
            continue
        if x_max is not None and x >= x_max:
            continue
        value = parse_money(word[4])
        if value is not None:
            values.append(value)
    return values[-1] if values else None


def _legacy_sight_summary(page) -> dict:
    rows = words_by_row(page)

    def find_row(label: str):
        target = normalize_text(label)
        for row in rows:
            if target in normalize_text(row_text(row)):
                return row
        return None

    mapping = {
        "opening_balance_mxn": ("SALDO INICIAL", 180, 290),
        "deposits_mxn": ("ABONOS", 180, 290),
        "withdrawals_mxn": ("CARGOS", 180, 290),
        "closing_balance_mxn": ("SALDO FINAL", 180, 290),
        "average_balance_mxn": ("SALDO PROMEDIO", 480, 560),
        "interest_gross_mxn": ("INTERESES BRUTOS", 480, 560),
        "isr_mxn": ("RETENCION ISR", 480, 560),
        "interest_net_mxn": ("INTERESES NETOS", 480, 560),
    }

    result = {}
    for key, (label, x0, x1) in mapping.items():
        row = find_row(label)
        result[key] = _money_from_row(row, x_min=x0, x_max=x1) if row else None

    # Some legacy statements leave Saldo Inicial visually blank when it is
    # zero. If deposits, withdrawals and closing balance are present, recover
    # opening balance algebraically instead of treating it as missing.
    if (
        result["opening_balance_mxn"] is None
        and result["deposits_mxn"] is not None
        and result["withdrawals_mxn"] is not None
        and result["closing_balance_mxn"] is not None
    ):
        result["opening_balance_mxn"] = safe_round(
            result["closing_balance_mxn"]
            - result["deposits_mxn"]
            + result["withdrawals_mxn"]
        )

    result["commissions_mxn"] = 0.0
    result["iva_commissions_mxn"] = 0.0

    return result


def _modern_distribution(page) -> dict:
    text = page.get_text("text")

    patterns = {
        "reported_debit_card_mxn": r"Cuenta D[ée]bito Crediclub\s*\n\s*\$\s*([0-9,]+\.\d{2})",
        "reported_sight_card_mxn": r"Inversi[óo]n a la Vista\s*\n\s*\$\s*([0-9,]+\.\d{2})",
        "reported_fixed_term_card_mxn": r"Inversi[óo]n a Plazo Fijo\s*\n\s*\$\s*([0-9,]+\.\d{2})",
    }

    out = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, flags=re.IGNORECASE)
        out[key] = parse_money(m.group(1)) if m else None
    return out


def _modern_debit_summary(page) -> dict:
    text = page.get_text("text")

    def after_label(label: str) -> float | None:
        m = re.search(
            re.escape(label) + r"\s*\n\s*(?:[-+]\s*)?\$?\s*([0-9,]+\.\d{2})",
            text,
            flags=re.IGNORECASE,
        )
        return parse_money(m.group(1)) if m else None

    deposits = after_label("Depósitos")
    withdrawals = after_label("Retiros")
    commissions = after_label("Comisiones cobradas")
    iva = after_label("IVA de comisiones")
    closing = after_label("Saldo al cierre del periodo")

    avg_m = re.search(
        r"Saldo promedio del periodo:\s*\$\s*([0-9,]+\.\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    average = parse_money(avg_m.group(1)) if avg_m else None

    if None not in (deposits, withdrawals, commissions, iva, closing):
        opening = (
            closing
            - deposits
            + withdrawals
            + commissions
            + iva
        )
    else:
        opening = None

    return {
        "opening_balance_mxn": safe_round(opening),
        "deposits_mxn": deposits,
        "withdrawals_mxn": withdrawals,
        "commissions_mxn": commissions,
        "iva_commissions_mxn": iva,
        "closing_balance_mxn": closing,
        "average_balance_mxn": average,
        "interest_gross_mxn": 0.0,
        "isr_mxn": 0.0,
        "interest_net_mxn": 0.0,
    }


def _modern_sight_interest(page) -> dict:
    text = page.get_text("text")

    patterns = {
        "interest_gross_mxn": r"Intereses generados antes de impuestos\s*\n\s*\+\s*\$\s*([0-9,]+\.\d{2})",
        "isr_mxn": r"ISR retenido\s*\n\s*-\s*\$\s*([0-9,]+\.\d{2})",
        "interest_net_mxn": r"Intereses netos abonados este mes\s*\n\s*\$\s*([0-9,]+\.\d{2})",
    }

    out = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, flags=re.IGNORECASE)
        out[key] = parse_money(m.group(1)) if m else None
    return out


def _parse_new_movement_rows(
    page,
    *,
    product_type: str,
    statement_end: date,
) -> list[dict]:
    rows = words_by_row(page, y_tolerance=1.5)

    # Locate movement table header and its logical end.
    start_y = None
    end_y = None

    for row in rows:
        norm = normalize_text(row_text(row))

        if start_y is None and "DESCRIPCION DE LA OPERACION" in norm:
            # Require table columns on the same row so we do not select the
            # section title alone.
            if "SALDO" in norm and (
                "CARGO" in norm
                or "RETIRO" in norm
            ):
                start_y = min(w[1] for w in row)
                continue

        if start_y is not None:
            if product_type == "DEBIT" and "SALDO PROMEDIO DEL PERIODO" in norm:
                end_y = min(w[1] for w in row)
                break
            if product_type == "SIGHT" and "III. INVERSION A PLAZO FIJO" in norm:
                end_y = min(w[1] for w in row)
                break

    if start_y is None:
        return []

    if end_y is None:
        end_y = page.rect.height - 20

    tx_rows = []
    current = None

    for row in rows:
        y = min(w[1] for w in row)
        if y <= start_y + 3 or y >= end_y:
            continue

        words = row
        date_words = [w[4] for w in words if w[0] < 75]
        date_text = " ".join(date_words).strip()

        date_value = None
        try:
            if date_text:
                date_value = parse_compact_date(
                    date_text,
                    statement_year=statement_end.year,
                )
        except ValueError:
            date_value = None

        desc_words = [w[4] for w in words if 80 <= w[0] < 390]
        description = " ".join(desc_words).strip()

        amount_debit = None
        amount_credit = None
        balance = None

        for w in words:
            value = parse_money(w[4])
            if value is None:
                continue
            x = w[0]

            if 390 <= x < 455:
                amount_debit = value
            elif 455 <= x < 520:
                amount_credit = value
            elif x >= 520:
                balance = value

        if date_value is not None:
            signed_amount = 0.0
            if amount_credit is not None:
                signed_amount = amount_credit
            elif amount_debit is not None:
                signed_amount = -amount_debit

            current = {
                "transaction_date": date_value.isoformat(),
                "product_type": product_type,
                "description": description,
                "signed_amount_mxn": safe_round(signed_amount),
                "deposit_mxn": safe_round(max(signed_amount, 0.0)),
                "withdrawal_mxn": safe_round(max(-signed_amount, 0.0)),
                "balance_mxn": balance,
            }
            tx_rows.append(current)
        elif current is not None and description:
            current["description"] = (
                current["description"] + " " + description
            ).strip()

    return tx_rows


def _parse_legacy_movement_rows(page, statement_end: date) -> list[dict]:
    rows = words_by_row(page, y_tolerance=1.8)

    start_y = None
    end_y = None

    for row in rows:
        norm = normalize_text(row_text(row))
        if start_y is None and "DETALLE DE MOVIMIENTOS EN CUENTA A LA VISTA" in norm:
            start_y = max(w[3] for w in row)
            continue
        if start_y is not None and "RESUMEN DE COMISIONES EN INVERSION A PLAZO" in norm:
            end_y = min(w[1] for w in row)
            break

    if start_y is None:
        return []
    if end_y is None:
        end_y = page.rect.height

    output = []

    for row in rows:
        y = min(w[1] for w in row)
        if y <= start_y or y >= end_y:
            continue

        date_text = " ".join(w[4] for w in row if w[0] < 75).strip()
        try:
            tx_date = parse_compact_date(
                date_text,
                statement_year=statement_end.year,
            )
        except ValueError:
            continue

        description = " ".join(
            w[4] for w in row if 145 <= w[0] < 310
        ).strip()

        reference = " ".join(
            w[4] for w in row if 310 <= w[0] < 375
        ).strip()

        cargo = _money_from_row(row, x_min=375, x_max=440)
        abono = _money_from_row(row, x_min=440, x_max=510)
        balance = _money_from_row(row, x_min=510)

        signed = (abono or 0.0) - (cargo or 0.0)

        output.append(
            {
                "transaction_date": tx_date.isoformat(),
                "product_type": "SIGHT",
                "description": description,
                "reference": reference or None,
                "signed_amount_mxn": safe_round(signed),
                "deposit_mxn": safe_round(max(signed, 0.0)),
                "withdrawal_mxn": safe_round(max(-signed, 0.0)),
                "balance_mxn": balance,
            }
        )

    return output


def _legacy_fixed_positions(page, statement_end: date) -> list[dict]:
    rows = words_by_row(page, y_tolerance=1.8)
    positions = []

    for row in rows:
        text = row_text(row)
        if not re.search(r"\d{4}-\d{2}-\d{2}", text):
            continue

        # The legacy position row has stable visual columns.
        principal = _money_from_row(row, x_min=20, x_max=90)
        term_days = None
        for w in row:
            if 90 <= w[0] < 120 and re.fullmatch(r"\d{1,4}", w[4]):
                term_days = int(w[4])
                break

        date_tokens = [
            w[4] for w in row
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w[4])
        ]
        if len(date_tokens) < 2 or principal is None:
            continue

        start_date = parse_compact_date(date_tokens[0])
        maturity_date = parse_compact_date(date_tokens[1])

        gross_interest = _money_from_row(row, x_min=220, x_max=275)
        isr = _money_from_row(row, x_min=275, x_max=325)
        net_interest = _money_from_row(row, x_min=325, x_max=395)
        capital_maturity = _money_from_row(row, x_min=395, x_max=465)

        rate = None
        gat_nominal = None
        gat_real = None
        for w in row:
            if 465 <= w[0] < 515:
                v = parse_percent(w[4])
                if v is not None:
                    rate = v
            elif 515 <= w[0] < 550:
                v = parse_percent(w[4])
                if v is not None:
                    gat_nominal = v
            elif w[0] >= 550:
                v = parse_percent(w[4])
                if v is not None:
                    gat_real = v

        status = (
            "ACTIVE_AT_CUTOFF"
            if maturity_date > statement_end
            else "MATURED_BY_CUTOFF"
        )

        positions.append(
            {
                "principal_mxn": principal,
                "term_days": term_days,
                "start_date": start_date.isoformat(),
                "maturity_date": maturity_date.isoformat(),
                "gross_interest_at_maturity_mxn": gross_interest,
                "isr_at_maturity_mxn": isr,
                "net_interest_at_maturity_mxn": net_interest,
                "capital_at_maturity_mxn": capital_maturity,
                "annual_gross_rate_pct": rate,
                "gat_nominal_pct": gat_nominal,
                "gat_real_pct": gat_real,
                "status_at_cutoff": status,
                "include_in_closing_portfolio": status == "ACTIVE_AT_CUTOFF",
            }
        )

    return positions


def _modern_fixed_positions(page, statement_end: date) -> list[dict]:
    rows = words_by_row(page, y_tolerance=1.5)
    positions = []

    # Rows containing both compact dates are position rows.
    for row in rows:
        text = row_text(row)
        dates = re.findall(r"\d{2}/\d{2}/\d{2}", text)
        if len(dates) < 2:
            continue

        principal = _money_from_row(row, x_min=20, x_max=90)
        if principal is None:
            continue

        start_date = parse_compact_date(dates[0])
        maturity_date = parse_compact_date(dates[1])

        term_days = None
        for w in row:
            if 90 <= w[0] < 135 and re.fullmatch(r"\d{1,4}", w[4]):
                term_days = int(w[4])
                break

        rate = None
        gat_nominal = None
        gat_real = None

        for w in row:
            if 255 <= w[0] < 315:
                v = parse_percent(w[4])
                if v is not None:
                    rate = v
            elif 315 <= w[0] < 370:
                v = parse_percent(w[4])
                if v is not None:
                    gat_nominal = v
            elif 370 <= w[0] < 420:
                v = parse_percent(w[4])
                if v is not None:
                    gat_real = v

        capital = _money_from_row(row, x_min=420, x_max=490)
        net_interest = _money_from_row(row, x_min=490, x_max=545)
        isr = _money_from_row(row, x_min=545)
        gross_interest = (
            safe_round((net_interest or 0.0) + (isr or 0.0))
            if net_interest is not None and isr is not None
            else None
        )

        status = (
            "ACTIVE_AT_CUTOFF"
            if maturity_date > statement_end
            else "MATURED_BY_CUTOFF"
        )

        positions.append(
            {
                "principal_mxn": principal,
                "term_days": term_days,
                "start_date": start_date.isoformat(),
                "maturity_date": maturity_date.isoformat(),
                "gross_interest_at_maturity_mxn": gross_interest,
                "isr_at_maturity_mxn": isr,
                "net_interest_at_maturity_mxn": net_interest,
                "capital_at_maturity_mxn": capital,
                "annual_gross_rate_pct": rate,
                "gat_nominal_pct": gat_nominal,
                "gat_real_pct": gat_real,
                "status_at_cutoff": status,
                "include_in_closing_portfolio": status == "ACTIVE_AT_CUTOFF",
            }
        )

    return positions


def _classify_movement(row: dict) -> tuple[str, str]:
    text = normalize_text(row.get("description", ""))
    product = row["product_type"]
    amount = float(row.get("signed_amount_mxn") or 0.0)

    if product == "SIGHT" and "PAGO INVERSION" in text:
        return "fixed_term_maturity", "internal_crediclub_candidate"

    if "B.LINEA.CTA" in text:
        return "internal_account_transfer", "internal_crediclub_candidate"

    if "TRANSFERENCIA ENVIADA" in text:
        return "transfer_out", "external_candidate"

    if "TRANSFERENCIA" in text and amount > 0:
        return "transfer_in", "external_candidate"

    if "INTERES" in text and amount > 0:
        return "interest", "earnings"

    if "ISR" in text or "RETENCION" in text:
        return "tax_withholding", "tax"

    if amount > 0:
        return "other_inflow", "external_candidate"
    if amount < 0:
        return "other_outflow", "external_candidate"
    return "other", "not_applicable"


def _pair_internal_movements(movements: list[dict], positions: list[dict]) -> None:
    # Pair SIGHT <-> DEBIT transfers with same date and opposite amount.
    candidates = [
        (i, row)
        for i, row in enumerate(movements)
        if row.get("flow_scope") == "internal_crediclub_candidate"
        and row.get("category") == "internal_account_transfer"
    ]

    used = set()
    pair_counter = 1

    for i, left in candidates:
        if i in used:
            continue

        best = None
        for j, right in candidates:
            if j == i or j in used:
                continue
            if left["transaction_date"] != right["transaction_date"]:
                continue
            if left["product_type"] == right["product_type"]:
                continue
            if abs(
                float(left["signed_amount_mxn"])
                + float(right["signed_amount_mxn"])
            ) <= TOLERANCE_MXN:
                best = j
                break

        if best is not None:
            pair_id = f"CREDICLUB_INTERNAL_{pair_counter:04d}"
            movements[i]["flow_scope"] = "internal_crediclub"
            movements[i]["internal_pair_id"] = pair_id
            movements[best]["flow_scope"] = "internal_crediclub"
            movements[best]["internal_pair_id"] = pair_id
            used.update({i, best})
            pair_counter += 1

    # A maturity credit to SIGHT is internal to the institution even though the
    # source is represented by the fixed-term position rather than another cash
    # movement row.
    maturity_values = {
        (
            p["maturity_date"],
            round(float(p["capital_at_maturity_mxn"]), 2),
        )
        for p in positions
        if p.get("capital_at_maturity_mxn") is not None
    }

    for row in movements:
        if row.get("category") != "fixed_term_maturity":
            continue

        key = (
            row["transaction_date"],
            round(abs(float(row["signed_amount_mxn"])), 2),
        )
        if key in maturity_values:
            row["flow_scope"] = "internal_crediclub"
            row["internal_pair_id"] = "FIXED_TERM_MATURITY"


def parse_statement(path: Path, config: dict[str, str]) -> tuple[dict, list[dict], list[dict], list[dict]]:
    doc = open_pdf(path, config)
    try:
        page1_text = doc[0].get_text("text")
        layout = detect_layout(page1_text)
        header = _extract_common_header(doc, layout)
        statement_end = header["statement_end"]

        relationship_id = config.get(
            "crediclub_relationship_id",
            "CREDICLUB_PRIMARY",
        )

        snapshots: list[dict] = []
        movements: list[dict] = []

        if layout == "legacy_pre_2026_06":
            sight = _legacy_sight_summary(doc[0])
            positions = _legacy_fixed_positions(doc[0], statement_end)
            movements = _parse_legacy_movement_rows(doc[0], statement_end)

            snapshots.append(
                {
                    "product_type": "SIGHT",
                    **sight,
                }
            )

            reported_debit_card = None
            reported_sight_card = sight.get("closing_balance_mxn")
            reported_fixed_card = safe_round(
                sum(float(p["principal_mxn"]) for p in positions)
            )

        else:
            distribution = _modern_distribution(doc[0])
            debit = _modern_debit_summary(doc[0])
            sight_interest = _modern_sight_interest(doc[1])

            debit_movements = _parse_new_movement_rows(
                doc[0],
                product_type="DEBIT",
                statement_end=statement_end,
            )
            sight_movements = _parse_new_movement_rows(
                doc[1],
                product_type="SIGHT",
                statement_end=statement_end,
            )

            movements = debit_movements + sight_movements
            positions = _modern_fixed_positions(doc[1], statement_end)

            sight_deposits = sum(
                float(r["deposit_mxn"])
                for r in sight_movements
            )
            sight_withdrawals = sum(
                float(r["withdrawal_mxn"])
                for r in sight_movements
            )
            sight_closing = distribution["reported_sight_card_mxn"]
            if sight_closing is not None:
                sight_opening = safe_round(
                    sight_closing
                    - sight_deposits
                    + sight_withdrawals
                )
            else:
                sight_opening = None

            snapshots.append(
                {
                    "product_type": "DEBIT",
                    **debit,
                }
            )

            snapshots.append(
                {
                    "product_type": "SIGHT",
                    "opening_balance_mxn": sight_opening,
                    "deposits_mxn": safe_round(sight_deposits),
                    "withdrawals_mxn": safe_round(sight_withdrawals),
                    "commissions_mxn": 0.0,
                    "iva_commissions_mxn": 0.0,
                    "closing_balance_mxn": sight_closing,
                    "average_balance_mxn": None,
                    **sight_interest,
                }
            )

            reported_debit_card = distribution["reported_debit_card_mxn"]
            reported_sight_card = distribution["reported_sight_card_mxn"]
            reported_fixed_card = distribution["reported_fixed_term_card_mxn"]

        # Classify cash movements and pair institution-internal transfers.
        for row in movements:
            category, scope = _classify_movement(row)
            row["category"] = category
            row["flow_scope"] = scope
            row["internal_pair_id"] = None

        _pair_internal_movements(movements, positions)

        active_fixed = safe_round(
            sum(
                float(p["principal_mxn"])
                for p in positions
                if p["include_in_closing_portfolio"]
            )
        )

        snapshots.append(
            {
                "product_type": "FIXED_TERM",
                "opening_balance_mxn": None,
                "deposits_mxn": None,
                "withdrawals_mxn": None,
                "commissions_mxn": 0.0,
                "iva_commissions_mxn": 0.0,
                "closing_balance_mxn": active_fixed,
                "average_balance_mxn": None,
                "interest_gross_mxn": None,
                "isr_mxn": None,
                "interest_net_mxn": None,
            }
        )

        common = {
            "source_file": path.name,
            "parser_version": CREDICLUB_PARSER_VERSION,
            "layout_version": layout,
            "statement_start": header["statement_start"].isoformat(),
            "statement_end": header["statement_end"].isoformat(),
            "year_month": header["statement_end"].strftime("%Y-%m"),
            "relationship_id": relationship_id,
            "statement_account_number": header["statement_account_number"],
            "clabe": header["clabe"],
            "currency": header["currency"],
        }

        for row in snapshots:
            row.update(common)
            row["account_id"] = f"{relationship_id}:{row['product_type']}"

        for row in movements:
            row.update(common)
            row["account_id"] = f"{relationship_id}:{row['product_type']}"

        for idx, row in enumerate(positions, start=1):
            row.update(common)
            row["position_id"] = (
                f"{relationship_id}:FIXED_TERM:"
                f"{row['start_date']}:{row['maturity_date']}:{idx}"
            )

        cash_closing = sum(
            float(row.get("closing_balance_mxn") or 0.0)
            for row in snapshots
            if row["product_type"] in {"DEBIT", "SIGHT"}
        )

        derived_closing = safe_round(cash_closing + active_fixed)

        reported_distribution_total = safe_round(
            sum(
                value or 0.0
                for value in (
                    reported_debit_card,
                    reported_sight_card,
                    reported_fixed_card,
                )
            )
        )

        summary = {
            **common,
            "reported_debit_card_mxn": reported_debit_card,
            "reported_sight_card_mxn": reported_sight_card,
            "reported_fixed_term_card_mxn": reported_fixed_card,
            "reported_distribution_total_mxn": reported_distribution_total,
            "derived_active_fixed_term_mxn": active_fixed,
            "derived_closing_portfolio_mxn": derived_closing,
            "fixed_term_positions_count": len(positions),
            "expired_positions_listed_count": sum(
                p["status_at_cutoff"] == "MATURED_BY_CUTOFF"
                for p in positions
            ),
        }

        return summary, snapshots, positions, movements
    finally:
        doc.close()


def main() -> None:
    config = load_config()
    raw_dir = project_path(
        config,
        "path_crediclub_edo_cta",
        "crediclub/edo_cta",
    )
    processed_dir = project_path(
        config,
        "path_crediclub_processed",
        "crediclub/processed",
    )
    processed_dir.mkdir(parents=True, exist_ok=True)

    pdfs = list_pdfs(raw_dir)
    if not pdfs:
        raise SystemExit(f"No PDF files found under: {raw_dir}")

    print(f"Crediclub parser version: {CREDICLUB_PARSER_VERSION}")

    summaries = []
    snapshots = []
    positions = []
    movements = []
    errors = []

    for path in pdfs:
        try:
            summary, s_rows, p_rows, m_rows = parse_statement(path, config)
            summaries.append(summary)
            snapshots.extend(s_rows)
            positions.extend(p_rows)
            movements.extend(m_rows)
            print(
                f"Parsed {path.name}: "
                f"{summary['layout_version']} "
                f"{summary['year_month']}"
            )
        except Exception as exc:
            errors.append(
                {
                    "source_file": path.name,
                    "error": str(exc),
                }
            )
            print(f"ERROR {path.name}: {exc}")

    write_table(
        pd.DataFrame(summaries),
        processed_dir / "statement_summary",
    )
    write_table(
        pd.DataFrame(snapshots),
        processed_dir / "account_snapshots",
    )
    write_table(
        pd.DataFrame(positions),
        processed_dir / "fixed_term_positions",
    )
    write_table(
        pd.DataFrame(movements),
        processed_dir / "movements",
    )

    if errors:
        write_table(
            pd.DataFrame(errors),
            processed_dir / "parse_errors",
        )
        raise SystemExit(
            f"Crediclub extraction completed with {len(errors)} error(s)."
        )

    print("Crediclub extraction completed successfully.")


if __name__ == "__main__":
    main()
