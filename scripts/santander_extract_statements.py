from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from santander_utils import (
    account_key,
    discover_statements,
    extract_account_number,
    extract_pdf_pages,
    extract_statement_period,
    load_config,
    money_values,
    normalize_text,
    parse_money,
    parse_spanish_date,
    resolve_project_path,
    write_table,
)


SANTANDER_PARSER_VERSION = "2026-11-08.5"


RECORD_START_RE = re.compile(
    # Accept AG0 as well as AGO because OCR frequently confuses O/0.
    r"^(?P<date>\d{2}-[A-ZÁÉÍÓÚ0]{3}-\d{4})\s+"
    r"(?P<folio>[A-Z0-9]+)(?:\s+(?P<description>.*))?$",
    re.IGNORECASE,
)

MONEY_TOKEN_RE = re.compile(r"(?<!\d)-?\d{1,3}(?:,\d{3})*\.\d{2}(?!\d)")

DATE_ONLY_RE = re.compile(
    r"^\d{2}-[A-ZÁÉÍÓÚ0]{3}-\d{4}$",
    re.IGNORECASE,
)

FOLIO_DESCRIPTION_RE = re.compile(
    r"^(?P<folio>[A-Z0-9]{1,12})\s+(?P<description>.+)$",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Santander Mexico image-based monthly statements."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def _section(text: str, start: str, end_markers: list[str]) -> str:
    normalized = normalize_text(text)
    start_norm = normalize_text(start)
    start_idx = normalized.find(start_norm)
    if start_idx < 0:
        return ""

    end_idx = len(normalized)
    for marker in end_markers:
        idx = normalized.find(normalize_text(marker), start_idx + len(start_norm))
        if idx >= 0:
            end_idx = min(end_idx, idx)

    # normalized text has the same broad content but not exact character offsets.
    # Use line-based extraction instead to preserve raw OCR text.
    lines = text.splitlines()
    started = False
    result = []
    end_norms = [normalize_text(x) for x in end_markers]

    for line in lines:
        line_norm = normalize_text(line)
        if not started:
            if start_norm in line_norm:
                started = True
                result.append(line)
            continue

        if any(marker in line_norm for marker in end_norms):
            break
        result.append(line)

    return "\n".join(result)


def _find_after_label(text: str, label: str) -> float | None:
    normalized_lines = [(line, normalize_text(line)) for line in text.splitlines()]
    wanted = normalize_text(label)

    for i, (raw, norm) in enumerate(normalized_lines):
        if wanted in norm:
            same = money_values(raw)
            if same:
                return same[-1]
            for j in range(i + 1, min(i + 5, len(normalized_lines))):
                values = money_values(normalized_lines[j][0])
                if values:
                    return values[0]
    return None


def _parse_total_balances(page1: str) -> tuple[float, float]:
    normalized = normalize_text(page1)
    match = re.search(
        r"TOTAL\s+(\d{1,3}(?:,\d{3})*\.\d{2})\s+100\.00%\s+"
        r"(\d{1,3}(?:,\d{3})*\.\d{2})\s+100\.00%",
        normalized,
    )
    if match:
        return parse_money(match.group(1)), parse_money(match.group(2))

    # Fallback: use the last four monetary values before "CUENTA DE CHEQUES".
    before = page1.split("Cuenta de cheques")[0]
    vals = money_values(before)
    if len(vals) >= 2:
        return vals[-2], vals[-1]
    raise ValueError("Could not parse Santander total previous/current balances.")


def _parse_account_summary(
    text: str,
    *,
    product_type: str,
    product_label: str,
    ordinal: int = 1,
) -> dict | None:
    lines = text.splitlines()
    wanted = normalize_text(product_label)

    # A product name can appear first in the summary table and later in the
    # detailed product section. Prefer the occurrence whose following lines
    # contain both "Saldo inicial" and "saldo final".
    candidate_starts = [
        i for i, line in enumerate(lines)
        if wanted in normalize_text(line)
    ]

    start = None
    for candidate in candidate_starts:
        probe = "\n".join(lines[candidate : min(len(lines), candidate + 55)])
        probe_norm = normalize_text(probe)
        if "SALDO INICIAL" in probe_norm and "SALDO FINAL" in probe_norm:
            start = candidate
            break

    if start is None and candidate_starts:
        start = candidate_starts[-1]
    if start is None:
        return None

    block_lines = lines[start : min(len(lines), start + 55)]
    block = "\n".join(block_lines)
    account_number = extract_account_number(block)

    values = {}
    for label, key in [
        ("Saldo inicial", "opening_balance_mxn"),
        ("+ Depositos", "deposits_reported_mxn"),
        ("+Depositos", "deposits_reported_mxn"),
        ("- Retiros", "withdrawals_reported_mxn"),
        ("~ Retiros", "withdrawals_reported_mxn"),
        ("= saldo final", "closing_balance_mxn"),
        ("= Saldo final", "closing_balance_mxn"),
        ("Saldo promedio", "average_balance_mxn"),
    ]:
        if key not in values or values[key] is None:
            values[key] = _find_after_label(block, label)

    if values.get("opening_balance_mxn") is None or values.get("closing_balance_mxn") is None:
        return None

    return {
        "product_type": product_type,
        "product_name": product_label,
        "account_number": account_number,
        "account_id": account_key(product_type, account_number, ordinal),
        **values,
    }


def _parse_interest_summary(page1: str) -> dict[str, float | None]:
    # The product table includes net interest for Dinero Creciente.
    normalized = normalize_text(page1)
    match = re.search(
        r"DINERO CRECIENTE SANTANDER(?:\s+\d{2}-\d{8}-\d)?\s+"
        r"(\d{1,3}(?:,\d{3})*\.\d{2})\s+"
        r"(\d{1,3}(?:,\d{3})*\.\d{2})\s+"
        r"(\d{1,3}(?:,\d{3})*\.\d{2})",
        normalized,
    )
    if not match:
        return {
            "interest_gross_reported_mxn": None,
            "isr_reported_mxn": None,
            "interest_net_reported_mxn": None,
        }

    return {
        "interest_gross_reported_mxn": parse_money(match.group(1)),
        "isr_reported_mxn": parse_money(match.group(2)),
        "interest_net_reported_mxn": parse_money(match.group(3)),
    }



def _movement_block(page_texts: list[str], product_type: str) -> tuple[str, float | None]:
    """
    Locate a complete Santander movement table across page boundaries.

    Statement length is variable. In the observed layouts:
      - CHECKING can start on page 2 and continue for several pages.
      - DINERO_CREC starts after the checking table and may also span pages.
    Therefore no fixed page number is used.
    """
    combined = "\n".join(page_texts[1:])
    lines = combined.splitlines()
    norms = [normalize_text(line) for line in lines]

    if product_type == "CHECKING":
        # The product name can appear in a summary table and again at the start
        # of the detailed movement table. Use the occurrence followed by the
        # prior-period balance header.
        candidates = [
            i for i, norm in enumerate(norms)
            if "CHEQUES-PAGO FIJO" in norm
        ]

        start_idx = None
        for candidate in candidates:
            probe = " ".join(norms[candidate : min(candidate + 18, len(norms))])
            if "SALDO FINAL DEL PERIODO ANTERIOR" in probe:
                start_idx = candidate
                break

        if start_idx is None:
            return "", None

        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if "DETALLES DE MOVIMIENTOS DINERO CRECIENTE" in norms[i]:
                end_idx = i
                break

    elif product_type == "DINERO_CREC":
        detail_idx = None
        for i, norm in enumerate(norms):
            if "DETALLES DE MOVIMIENTOS DINERO CRECIENTE" in norm:
                detail_idx = i
                break

        start_idx = None
        search_from = detail_idx if detail_idx is not None else 0

        for i in range(search_from, len(lines)):
            if "DINERO CRECIENTE SANTANDER" not in norms[i]:
                continue

            probe = " ".join(norms[i : min(i + 18, len(norms))])
            if (
                "SALDO FINAL DEL PERIODO ANTERIOR" in probe
                or detail_idx is not None
            ):
                start_idx = i
                break

        if start_idx is None:
            return "", None

        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            norm = norms[i]
            if (
                "DETALLE DE MOVIMIENTO MIS METAS" in norm
                or "DETALLES DE MOVIMIENTO MIS METAS" in norm
            ):
                end_idx = i
                break

            # Fallback for minor OCR variations in the section heading.
            if i > start_idx + 10 and norm.startswith("MIS METAS "):
                end_idx = i
                break
    else:
        return "", None

    opening = None
    for i in range(start_idx, min(start_idx + 25, end_idx)):
        if "SALDO FINAL DEL PERIODO ANTERIOR" not in norms[i]:
            continue

        vals = money_values(lines[i])
        if vals:
            opening = vals[-1]
        else:
            for j in range(i + 1, min(i + 5, end_idx)):
                vals = money_values(lines[j])
                if vals:
                    opening = vals[0]
                    break
        break

    return "\n".join(lines[start_idx:end_idx]), opening


def _parse_records(
    block: str,
    *,
    account_id: str,
    product_type: str,
    opening_balance: float,
) -> list[dict]:
    """
    Parse Santander movement rows from OCR text.

    Important OCR cases handled here:
      - Date and folio/description may be emitted on separate lines.
      - Amount and balance may be emitted in either order.
      - Thousands commas may be OCR'ed as dots.
      - Small interest/ISR values may lose their decimal point.
      - Damaged balances are reconstructed from prior validated balance + amount.
    """

    raw_lines = [line.strip() for line in block.splitlines() if line.strip()]

    # Santander/Tesseract sometimes outputs:
    #   01-JUL-2026
    #   2385542 LIQ INVERSION...
    # Join those into a canonical record-start line before parsing.
    lines: list[str] = []
    i = 0
    while i < len(raw_lines):
        current_norm = normalize_text(raw_lines[i])

        if DATE_ONLY_RE.match(current_norm) and i + 1 < len(raw_lines):
            next_norm = normalize_text(raw_lines[i + 1])
            if FOLIO_DESCRIPTION_RE.match(next_norm):
                lines.append(f"{raw_lines[i]} {raw_lines[i + 1]}")
                i += 2
                continue

        lines.append(raw_lines[i])
        i += 1

    starts = [
        i for i, line in enumerate(lines)
        if RECORD_START_RE.match(normalize_text(line))
    ]

    def numeric_only(line: str, *, interest_like: bool = False) -> float | None:
        clean = line.replace("$", "").strip()

        # Strip OCR/table punctuation around an otherwise numeric token.
        clean = re.sub(r"^[^0-9-]+", "", clean)
        clean = re.sub(r"[^0-9]+$", "", clean)

        if not clean:
            return None

        # Standard MXN rendering.
        if re.fullmatch(r"-?\d+(?:,\d{3})*\.\d{2}", clean):
            return float(clean.replace(",", ""))

        # OCR sometimes replaces thousands commas with dots:
        # 10.100.00 -> 10,100.00
        if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+\.\d{2}", clean):
            sign = -1.0 if clean.startswith("-") else 1.0
            unsigned = clean.lstrip("-")
            parts = unsigned.split(".")
            integer = "".join(parts[:-1])
            decimals = parts[-1]
            return sign * float(f"{integer}.{decimals}")

        # In interest/ISR rows Tesseract can drop the decimal point:
        # 481 -> 4.81. Restrict this repair to those row types.
        if interest_like and re.fullmatch(r"\d{1,4}", clean):
            return float(clean) / 100.0

        return None

    def amount_sign(description_normalized: str) -> int:
        positive_markers = (
            "ABONO ",
            "ABO POR INTERESES",
            "TU CASHBACK",
            "LIQ A CHE INVERSION CRECIENTE",
            "DEP APERT INVERSION CRECIENTE",
        )
        negative_markers = (
            "PAGO ",
            "CARGO ",
            "DOMICILIACION ",
            "LIQ INVERSION CRECIENTE",
            "RETENCION ",
        )

        if any(marker in description_normalized for marker in positive_markers):
            return 1
        if any(marker in description_normalized for marker in negative_markers):
            return -1
        return 0

    parsed = []

    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        record_lines = lines[start:end]

        first = normalize_text(record_lines[0])
        match = RECORD_START_RE.match(first)
        if not match:
            continue

        first_description = (match.group("description") or "").strip()
        description_lines = [first_description] if first_description else []
        first_description_norm = normalize_text(first_description)

        interest_like = (
            "ABO POR INTERESES" in first_description_norm
            or "RETENCION" in first_description_norm
        )

        numeric_lines: list[float] = []

        for line in record_lines[1:]:
            value = numeric_only(line, interest_like=interest_like)
            if value is not None and len(numeric_lines) < 2:
                numeric_lines.append(value)
                continue

            norm = normalize_text(line)

            if norm in {
                "DEPOSITO",
                "RETIRO",
                "SALDO",
                "FECHA",
                "FOLIO",
                "DESCRIPCION",
                "DESCRIPCION OPERACION",
                "|",
            }:
                continue

            if (
                "SANTANDER SELECT" in norm
                or "BANCO SANTANDER MEXICO" in norm
                or norm.startswith("PAGINA ")
                or "CODIGO DE CLIENTE" in norm
                or norm.startswith("PERIODO DEL ")
                or norm == "ESTADO DE CUENTA"
            ):
                continue

            description_lines.append(line)

        description = " ".join(description_lines).strip()
        normalized_description = normalize_text(description)

        parsed.append(
            {
                "transaction_date": parse_spanish_date(match.group("date")),
                "folio": match.group("folio"),
                "description": description,
                "description_normalized": normalized_description,
                "numbers": numeric_lines,
                "sign": amount_sign(normalized_description),
            }
        )

    def valid_options(previous_balance: float, item: dict) -> list[dict]:
        nums = item["numbers"]
        preferred_sign = item["sign"]

        if not nums:
            return []

        options: list[dict] = []

        if len(nums) >= 2:
            orientations = [
                (nums[0], nums[1], "ocr"),
                (nums[1], nums[0], "ocr_swapped"),
            ]

            for amount_abs, balance, source in orientations:
                signs = [preferred_sign] if preferred_sign else [1, -1]

                for sign in signs:
                    signed = round(sign * abs(float(amount_abs)), 2)
                    predicted = round(previous_balance + signed, 2)

                    if abs(predicted - float(balance)) <= 1.00:
                        options.append(
                            {
                                "signed_amount": signed,
                                "balance": round(float(balance), 2),
                                "balance_source": source,
                                "sign_match": int(
                                    bool(preferred_sign and sign == preferred_sign)
                                ),
                            }
                        )

        # If the OCR balance is damaged but the transaction amount and its
        # direction are known, reconstruct the resulting balance.
        if not options and preferred_sign:
            amount_abs = abs(float(nums[0]))
            signed = round(preferred_sign * amount_abs, 2)
            options.append(
                {
                    "signed_amount": signed,
                    "balance": round(previous_balance + signed, 2),
                    "balance_source": "reconstructed",
                    "sign_match": 1,
                }
            )

        unique = []
        seen = set()

        for option in options:
            key = (option["signed_amount"], option["balance"])
            if key not in seen:
                seen.add(key)
                unique.append(option)

        return unique

    rows = []
    previous_balance = opening_balance

    for idx, item in enumerate(parsed):
        options = valid_options(previous_balance, item)
        if not options:
            continue

        scored = []

        for option in options:
            score = (
                3
                if option["balance_source"] in {"ocr", "ocr_swapped"}
                else 1
            )
            score += option.get("sign_match", 0)

            # Look ahead one row. A candidate that lets the next row reconcile
            # to an OCR balance is stronger than one that forces reconstruction.
            if idx + 1 < len(parsed):
                next_options = valid_options(option["balance"], parsed[idx + 1])
                if any(
                    candidate["balance_source"] in {"ocr", "ocr_swapped"}
                    for candidate in next_options
                ):
                    score += 2

            scored.append(
                (
                    score,
                    1 if option["balance_source"] == "ocr" else 0,
                    option,
                )
            )

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        chosen = scored[0][2]

        previous_balance_before = previous_balance
        previous_balance = chosen["balance"]

        normalized_description = item["description_normalized"]

        counterparty_bank = None
        bank_match = re.search(
            r"(?:RECIBIDO DE|ENVIADO A)\s+([A-Z0-9 ]+?)"
            r"(?:\s+DE LA CUENTA|\s+ALA CUENTA|\s+AL CLIENTE|$)",
            normalized_description,
        )
        if bank_match:
            counterparty_bank = bank_match.group(1).strip()

        signed_amount = chosen["signed_amount"]

        rows.append(
            {
                "transaction_date": item["transaction_date"].isoformat(),
                "account_id": account_id,
                "product_type": product_type,
                "folio": item["folio"],
                "description": item["description"],
                "description_normalized": normalized_description,
                "previous_balance_mxn": round(previous_balance_before, 2),
                "balance_mxn": round(previous_balance, 2),
                "balance_source": chosen["balance_source"],
                "ocr_numeric_values": "|".join(
                    f"{x:.2f}" for x in item["numbers"]
                ),
                "signed_amount_mxn": signed_amount,
                "deposit_mxn": signed_amount if signed_amount > 0 else 0.0,
                "withdrawal_mxn": -signed_amount if signed_amount < 0 else 0.0,
                "counterparty_bank": counterparty_bank,
            }
        )

    return rows


def _classify_transaction(row: dict) -> tuple[str, str]:
    text = row["description_normalized"]
    amount = row["signed_amount_mxn"]

    if "ABO POR INTERESES" in text:
        return "interest", "earnings"

    # OCR can render ISR in several forms: "ISR", "IS R", "1S R", "|S R".
    # RETENCION itself is a sufficiently strong Santander movement marker.
    if "RETENCION" in text:
        return "tax_withholding", "tax"

    if "CASHBACK" in text:
        return "cashback", "earnings"
    if "PAGO DE NOMINA" in text:
        return "payroll", "external_candidate"
    if "PAGO TARJETA CREDITO" in text:
        return "credit_card_payment", "external_candidate"
    if "CARGO CAPITAL DE CREDITO" in text:
        return "loan_principal_payment", "external_candidate"
    if "CARGO POR INTERESES DE CREDITO" in text:
        return "loan_interest_payment", "external_candidate"
    if "DOMICILIACION PAGO SERVICIO" in text:
        return "service_payment", "external_candidate"

    if "LIQ A CHE INVERSION CRECIENTE" in text:
        return "santander_internal_transfer", "internal_candidate"
    if "CARGO APERTURA INV CRECIENTE" in text:
        return "santander_internal_transfer", "internal_candidate"
    if "LIQ INVERSION CRECIENTE" in text:
        return "santander_internal_transfer", "internal_candidate"
    if "DEP APERT INVERSION CRECIENTE" in text:
        return "santander_internal_transfer", "internal_candidate"

    if "ABONO TRANSFERENCIA" in text:
        return "transfer_in", "external_candidate"
    if (
        "PAGO TRANSFERENCIA" in text
        or "CARGO TRANSFERENCIA" in text
        or "PAGO TRANSF RAPIDA" in text
    ):
        return "transfer_out", "external_candidate"

    if amount > 0:
        return "other_inflow", "external_candidate"
    if amount < 0:
        return "other_outflow", "external_candidate"
    return "other", "not_applicable"

def _pair_internal_santander(movements: pd.DataFrame) -> pd.DataFrame:
    if movements.empty:
        return movements

    df = movements.copy()
    df["flow_scope"] = df["flow_scope_initial"]
    df["internal_pair_id"] = pd.NA

    candidates = df[df["flow_scope_initial"].eq("internal_candidate")].copy()
    used: set[int] = set()
    pair_no = 0

    for idx, row in candidates.iterrows():
        if idx in used:
            continue

        opposite = candidates[
            (candidates.index != idx)
            & (~candidates.index.isin(used))
            & (candidates["folio"].astype(str) == str(row["folio"]))
            & (candidates["transaction_date"] == row["transaction_date"])
            & ((candidates["signed_amount_mxn"] + row["signed_amount_mxn"]).abs() <= 0.02)
            & (candidates["account_id"] != row["account_id"])
        ]

        if len(opposite) == 1:
            other_idx = opposite.index[0]
            pair_no += 1
            pair_id = f"SANT-INT-{pair_no:06d}"
            df.loc[[idx, other_idx], "flow_scope"] = "internal_santander"
            df.loc[[idx, other_idx], "internal_pair_id"] = pair_id
            used.update({idx, other_idx})

    # Earnings never represent external contributed capital.
    df.loc[df["flow_scope_initial"].eq("earnings"), "flow_scope"] = "earnings"

    df["external_flow_candidate_mxn"] = 0.0
    external = df["flow_scope"].eq("external_candidate")
    df.loc[external, "external_flow_candidate_mxn"] = df.loc[external, "signed_amount_mxn"]

    return df


def parse_statement(path: Path, config: dict[str, str], output_dir: Path):
    cache_dir = output_dir / "ocr_cache"
    pages = extract_pdf_pages(path, config=config, cache_dir=cache_dir, max_pages=None)

    if len(pages) < 4:
        raise ValueError(f"Expected at least 4 pages in Santander statement: {path.name}")

    period_start, period_end = extract_statement_period(pages[0])
    total_previous, total_current = _parse_total_balances(pages[0])
    interest_summary = _parse_interest_summary(pages[0])

    accounts = []

    checking = _parse_account_summary(
        pages[0],
        product_type="CHECKING",
        product_label="CHEQUES-PAGO FIJO",
    )
    if checking:
        accounts.append(checking)

    dinero = _parse_account_summary(
        pages[1],
        product_type="DINERO_CREC",
        product_label="DINERO CRECIENTE SANTANDER",
    )
    if dinero:
        accounts.append(dinero)

    # Parse Mis Metas from page 2 summary; the zero-movement detail is page 5.
    page2_lines = pages[1].splitlines()
    mis_metas_headers = [
        line for line in page2_lines
        if normalize_text(line).startswith("MIS METAS SANTANDER")
    ]
    for ordinal, header in enumerate(mis_metas_headers, start=1):
        account_number = extract_account_number(header)
        accounts.append(
            {
                "product_type": "MIS_METAS",
                "product_name": "MIS METAS SANTANDER",
                "account_number": account_number,
                "account_id": account_key("MIS_METAS", account_number, ordinal),
                "opening_balance_mxn": 0.0,
                "deposits_reported_mxn": 0.0,
                "withdrawals_reported_mxn": 0.0,
                "closing_balance_mxn": 0.0,
                "average_balance_mxn": 0.0,
            }
        )

    movements = []

    account_by_type = {x["product_type"]: x for x in accounts if x["product_type"] != "MIS_METAS"}

    for product_type in ("CHECKING", "DINERO_CREC"):
        if product_type not in account_by_type:
            continue

        block, opening_from_movements = _movement_block(pages, product_type)
        if not block:
            continue

        account = account_by_type[product_type]
        opening = (
            opening_from_movements
            if opening_from_movements is not None
            else account["opening_balance_mxn"]
        )

        records = _parse_records(
            block,
            account_id=account["account_id"],
            product_type=product_type,
            opening_balance=opening,
        )
        for row in records:
            category, scope = _classify_transaction(row)
            row["category"] = category
            row["flow_scope_initial"] = scope
            row["statement_month"] = f"{period_end.year:04d}-{period_end.month:02d}"
            row["source_file"] = path.name
        movements.extend(records)

    movements_df = pd.DataFrame(movements)
    if not movements_df.empty:
        movements_df = _pair_internal_santander(movements_df)

    summary = {
        "statement_date": period_end.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "year": period_end.year,
        "month": period_end.month,
        "year_month": f"{period_end.year:04d}-{period_end.month:02d}",
        "total_previous_mxn": total_previous,
        "total_current_mxn": total_current,
        "source_file": path.name,
        **interest_summary,
    }

    account_rows = []
    for account in accounts:
        row = dict(account)
        row.update(
            {
                "statement_date": period_end.isoformat(),
                "year_month": f"{period_end.year:04d}-{period_end.month:02d}",
                "source_file": path.name,
            }
        )
        account_rows.append(row)

    return summary, account_rows, movements_df


def main() -> None:
    print(f"Santander parser version: {SANTANDER_PARSER_VERSION}")
    args = parse_args()
    config = load_config(args.config)

    input_dir = resolve_project_path(
        args.input
        or config.get("path_santander_edo_cta", "santander/edo_cta")
    )
    output_dir = resolve_project_path(
        args.output
        or config.get("path_santander_processed", "santander/processed")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    accounts = []
    movements = []
    errors = []

    for pdf in discover_statements(input_dir):
        try:
            summary, account_rows, movement_df = parse_statement(pdf, config, output_dir)
            summaries.append(summary)
            accounts.extend(account_rows)
            if not movement_df.empty:
                movements.extend(movement_df.to_dict("records"))
            print(
                f"[OK] {pdf.name}: "
                f"{len(account_rows)} account rows, "
                f"{len(movement_df)} movements"
            )
        except Exception as exc:
            errors.append(
                {
                    "source_file": pdf.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            print(f"[ERROR] {pdf.name}: {exc}")

    summary_df = pd.DataFrame(summaries)
    accounts_df = pd.DataFrame(accounts)
    movements_df = pd.DataFrame(movements)
    errors_df = pd.DataFrame(errors)

    if not summary_df.empty:
        summary_df = summary_df.sort_values(["statement_date", "source_file"])
        write_table(summary_df, output_dir / "statement_summary")

    if not accounts_df.empty:
        accounts_df = accounts_df.sort_values(
            ["statement_date", "product_type", "account_id"]
        )
        write_table(accounts_df, output_dir / "account_snapshots")

    if not movements_df.empty:
        movements_df = movements_df.sort_values(
            ["transaction_date", "account_id", "folio"]
        )
        write_table(movements_df, output_dir / "movements")

    if not errors_df.empty:
        write_table(errors_df, output_dir / "parse_errors")

    print(f"\nOutput written to: {output_dir}")


if __name__ == "__main__":
    main()
