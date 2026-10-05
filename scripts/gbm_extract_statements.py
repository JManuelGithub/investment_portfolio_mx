from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from gbm_utils import (
    discover_statements,
    extract_between_headers,
    flatten_pages,
    following_numbers,
    is_number,
    load_config,
    normalize_text,
    parse_ddmmyyyy,
    parse_number,
    parse_statement_filename,
    pdf_pages_as_lines,
    resolve_project_path,
    safe_date,
    write_table,
)


TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-_/& ]{0,24}\*?$", re.IGNORECASE)
MOVEMENT_DATE_RE = re.compile(r"^(?P<oper>\d{2})/(?P<liq>\d{2})$")


def _clean_ticker(value: str) -> str:
    return value.replace("*", "").strip()


def _summary_row_numbers(
    page1_lines: list[str],
    label: str,
    count: int = 3,
    *,
    contains: bool = False,
    ignore_legacy_footnote: bool = False,
) -> list[float]:
    """
    Extract a row from page 1.

    Legacy GBM PDFs (before April 2022) render the footnote marker in
    "VALOR DEL PORTAFOLIO¹" as a separate numeric line containing just "1".
    Without this guard that footnote is incorrectly parsed as a portfolio value.
    """
    from gbm_utils import find_line, find_line_contains

    finder = find_line_contains if contains else find_line
    idx = finder(page1_lines, label)

    values: list[float] = []
    for line in page1_lines[idx + 1:]:
        if is_number(line):
            # In legacy statements the superscript footnote becomes a standalone 1.
            if (
                ignore_legacy_footnote
                and not values
                and line.strip() in {"1", "¹"}
            ):
                continue
            values.append(parse_number(line))
            if len(values) == count:
                return values
        elif values:
            break

    raise ValueError(
        f"Expected {count} numeric values after '{label}', found {len(values)}."
    )


def extract_summary(page1_lines: list[str], meta) -> dict:
    """
    Extract the stable page-1 portfolio summary for both known GBM layouts:
      * legacy_pre_2022_04
      * modern_2022_04_plus
    """
    variable_income = _summary_row_numbers(page1_lines, "RENTA VARIABLE", 3)
    cash = _summary_row_numbers(page1_lines, "EFECTIVO", 3)
    debt = _summary_row_numbers(page1_lines, "DEUDA", 3)
    portfolio = _summary_row_numbers(
        page1_lines,
        "VALOR DEL PORTAFOLIO",
        3,
        contains=True,
        ignore_legacy_footnote=True,
    )

    def row(label: str) -> list[float] | None:
        try:
            return _summary_row_numbers(page1_lines, label, 3)
        except ValueError:
            return None

    def one(label: str) -> float | None:
        try:
            return following_numbers(page1_lines, label, 1)[0]
        except ValueError:
            return None

    # Optional components differ slightly between legacy and modern statements.
    fund_of_funds = row("FONDO DE FONDOS")
    other_investments = row("OTRAS INVERSIONES")
    margin_credits = row("CREDITOS DE MARGEN")
    guarantees = row("GARANTÍAS")
    short_values = row("VALORES EN CORTO / PRÉSTAMO DE VALORES")

    return {
        "statement_date": meta.period_end.isoformat(),
        "year": meta.year,
        "month": meta.month,
        "year_month": meta.year_month,
        "layout_version": meta.layout_version,
        "contract_file_id": meta.contract,
        "source_file": meta.path.name,
        "debt_previous_mxn": debt[0],
        "debt_current_mxn": debt[1],
        "variable_income_previous_mxn": variable_income[0],
        "variable_income_current_mxn": variable_income[1],
        "cash_previous_mxn": cash[0],
        "cash_current_mxn": cash[1],
        "fund_of_funds_previous_mxn": fund_of_funds[0] if fund_of_funds else None,
        "fund_of_funds_current_mxn": fund_of_funds[1] if fund_of_funds else None,
        "other_investments_previous_mxn": other_investments[0] if other_investments else None,
        "other_investments_current_mxn": other_investments[1] if other_investments else None,
        "margin_credits_previous_mxn": margin_credits[0] if margin_credits else None,
        "margin_credits_current_mxn": margin_credits[1] if margin_credits else None,
        "guarantees_previous_mxn": guarantees[0] if guarantees else None,
        "guarantees_current_mxn": guarantees[1] if guarantees else None,
        "short_values_previous_mxn": short_values[0] if short_values else None,
        "short_values_current_mxn": short_values[1] if short_values else None,
        "portfolio_value_previous_mxn": portfolio[0],
        "portfolio_value_current_mxn": portfolio[1],
        "cash_in_mxn": one("ENTRADAS DE EFECTIVO"),
        "cash_out_mxn": one("SALIDAS DE EFECTIVO"),
        "securities_deposit_mxn": one("DEPOSITO DE VALORES"),
        "securities_withdrawal_mxn": one("RETIRO DE VALORES"),
        "patrimonial_rights_mxn": one("DERECHOS PATRIMONIALES"),
        "commissions_mxn": one("COMISIONES"),
        "net_movements_mxn": one("SALDO NETO DE MOVIMIENTOS"),
    }


def _looks_like_ticker(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 28:
        return False

    normalized = normalize_text(text)
    banned = (
        "TOTAL",
        "EFECTIVO",
        "EMISORA",
        "NO. TITULOS",
        "COSTO",
        "PRECIO",
        "VALOR",
        "PLUSVALIA",
        "CART.",
    )
    if any(word in normalized for word in banned):
        return False
    return bool(TICKER_RE.fullmatch(text))


def extract_holdings(all_lines: list[str], meta) -> list[dict]:
    """
    Parse the 'ACCIONES DEL SIC' holdings table without assuming a page number.

    The April-2022+ format emits each table cell as a separate text line.
    A normal row contains 10 numeric values after the issuer/ticker.
    """
    try:
        section = extract_between_headers(
            all_lines,
            "ACCIONES DEL SIC",
            ["TOTAL: ACCIONES DEL SIC", "TOTAL: RENTA VARIABLE", "EFECTIVO"],
        )
    except ValueError:
        return []

    rows: list[dict] = []
    i = 0
    while i < len(section):
        line = section[i]
        if not _looks_like_ticker(line):
            i += 1
            continue

        numeric_values: list[float] = []
        j = i + 1
        while j < len(section) and len(numeric_values) < 10:
            current = section[j]
            if is_number(current):
                numeric_values.append(parse_number(current))
                j += 1
            else:
                break

        if len(numeric_values) == 10:
            (
                qty_previous,
                qty_current,
                qty_on_loan,
                avg_cost,
                total_cost,
                market_price,
                previous_market_price,
                market_value,
                unrealized_gain,
                portfolio_pct,
            ) = numeric_values

            rows.append(
                {
                    "statement_date": meta.period_end.isoformat(),
                    "year_month": meta.year_month,
                    "layout_version": meta.layout_version,
                    "contract_file_id": meta.contract,
                    "ticker": _clean_ticker(line),
                    "quantity_previous": qty_previous,
                    "quantity_current": qty_current,
                    "quantity_on_loan": qty_on_loan,
                    "average_cost_mxn": avg_cost,
                    "total_cost_mxn": total_cost,
                    "market_price_mxn": market_price,
                    "previous_market_price_mxn": previous_market_price,
                    "market_value_mxn": market_value,
                    "unrealized_gain_mxn": unrealized_gain,
                    "portfolio_pct": portfolio_pct,
                    "source_file": meta.path.name,
                }
            )
            i = j
        else:
            i += 1

    return rows


def extract_monthly_returns(all_lines: list[str], meta) -> list[dict]:
    """
    Parse 'RENDIMIENTO DEL PORTAFOLIO' wherever it appears in the PDF.
    GBM repeats several prior months, so build_gbm_history.py de-duplicates them.
    """
    try:
        section = extract_between_headers(
            all_lines,
            "RENDIMIENTO DEL PORTAFOLIO",
            [
                "COMPOSICIÓN FISCAL INFORMATIVA",
                "COMPOSICION FISCAL INFORMATIVA",
                "CONSTANCIA INFORMATIVA",
            ],
        )
    except ValueError:
        return []

    rows: list[dict] = []
    i = 0
    while i < len(section):
        line = section[i]
        match = re.match(
            r"^Del\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})$",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            i += 1
            continue

        numbers: list[float] = []
        j = i + 1
        while j < len(section) and len(numbers) < 4:
            if is_number(section[j]):
                numbers.append(parse_number(section[j]))
            else:
                if numbers:
                    break
            j += 1

        if len(numbers) == 4:
            start_date = parse_ddmmyyyy(match.group(1))
            end_date = parse_ddmmyyyy(match.group(2))
            rows.append(
                {
                    "period_start": start_date.isoformat(),
                    "period_end": end_date.isoformat(),
                    "return_year_month": f"{end_date.year:04d}-{end_date.month:02d}",
                    "gross_return_mxn": numbers[0],
                    "gross_return_pct": numbers[1],
                    "net_return_mxn": numbers[2],
                    "net_return_pct": numbers[3],
                    "source_statement_date": meta.period_end.isoformat(),
                    "layout_version": meta.layout_version,
                    "source_file": meta.path.name,
                    "contract_file_id": meta.contract,
                }
            )
            i = j
        else:
            i += 1

    return rows


def _classify_movement(description: str) -> str:
    text = normalize_text(description)

    if "RETIRO" in text and "EFECTIVO" in text:
        return "withdrawal"
    if (
        ("DEPOSITO" in text or "ENTRADA" in text or "ABONO" in text)
        and "EFECTIVO" in text
        and "DIVIDENDO" not in text
    ):
        return "contribution"
    if "DIVIDENDO" in text and "ISR" not in text:
        return "dividend"
    if "ISR" in text or "IMPUESTO" in text:
        return "tax"
    if "COMISION" in text:
        return "commission"
    if "REPORTO" in text:
        return "reporto"
    if "COMPRA" in text:
        return "purchase"
    if "VENTA" in text:
        return "sale"
    if "EFECTIVO INICIAL" in text:
        return "opening_balance"
    return "other"


def extract_movements(all_lines: list[str], meta) -> list[dict]:
    """
    Extract movement records across pages.

    Because descriptions can wrap and the table can span pages, each record is
    captured from one FECHA OPER/LIQ token to the next. The parser intentionally
    keeps raw_record_text for auditability.

    external_flow_mxn is populated only for contributions/withdrawals, which is
    the key field needed to separate investment performance from new savings.
    """
    try:
        section = extract_between_headers(
            all_lines,
            "DESGLOSE DE MOVIMIENTOS",
            [
                "RENDIMIENTO DEL PORTAFOLIO",
                "COMPOSICIÓN FISCAL INFORMATIVA",
                "COMPOSICION FISCAL INFORMATIVA",
            ],
        )
    except ValueError:
        return []

    start_indices = [
        i for i, line in enumerate(section)
        if MOVEMENT_DATE_RE.match(line.strip())
    ]

    rows: list[dict] = []
    for pos, start in enumerate(start_indices):
        end = start_indices[pos + 1] if pos + 1 < len(start_indices) else len(section)
        record = section[start:end]

        # Remove page footers/headers that can appear between the last record
        # on one page and the first record on the next page.
        for stop_idx, token in enumerate(record):
            if normalize_text(token).startswith("HOJA:"):
                record = record[:stop_idx]
                break

        if len(record) < 2:
            continue

        date_match = MOVEMENT_DATE_RE.match(record[0].strip())
        if not date_match:
            continue

        operation_day = int(date_match.group("oper"))
        liquidation_day = int(date_match.group("liq"))

        # Legacy PDFs often combine FOLIO + DESCRIPTION in a single extracted
        # text line (e.g. "190604298 Compra en Reporto"). Newer PDFs normally
        # extract them as separate lines. Support both forms.
        folio_token = record[1].strip()
        combined = re.match(r"^(?P<folio>\d+)\s+(?P<description>.+)$", folio_token)
        if combined:
            folio = combined.group("folio")
            body = [combined.group("description")] + record[2:]
        else:
            folio = folio_token
            body = record[2:]

        first_numeric = None
        for idx, token in enumerate(body):
            if is_number(token):
                first_numeric = idx
                break

        if first_numeric is None:
            text_tokens = body
            numeric_tokens: list[str] = []
        else:
            text_tokens = body[:first_numeric]
            numeric_tokens = [token for token in body[first_numeric:] if is_number(token)]

        issuer = ""
        description_tokens = text_tokens
        if text_tokens:
            last = text_tokens[-1].strip()
            if len(last) <= 24 and (
                "*" in last
                or re.fullmatch(r"[A-Z0-9][A-Z0-9 .\-_/&]*", last, re.IGNORECASE)
            ):
                issuer = _clean_ticker(last)
                description_tokens = text_tokens[:-1]

        description = " ".join(description_tokens).strip()
        category = _classify_movement(description)

        numeric_values = [parse_number(x) for x in numeric_tokens]
        net = numeric_values[-2] if len(numeric_values) >= 2 else None
        balance = numeric_values[-1] if numeric_values else None

        external_flow = 0.0
        if net is not None:
            if category == "contribution":
                external_flow = abs(net)
            elif category == "withdrawal":
                external_flow = -abs(net)

        rows.append(
            {
                "statement_date": meta.period_end.isoformat(),
                "year_month": meta.year_month,
                "layout_version": meta.layout_version,
                "operation_date": safe_date(
                    meta.year, meta.month, operation_day
                ).isoformat(),
                "operation_day": operation_day,
                "liquidation_day": liquidation_day,
                "folio": folio,
                "description": description,
                "issuer": issuer,
                "category": category,
                "net_mxn_reported": net,
                "balance_mxn_reported": balance,
                "external_flow_mxn": external_flow,
                "raw_record_text": " | ".join(record),
                "contract_file_id": meta.contract,
                "source_file": meta.path.name,
            }
        )

    return rows


def parse_statement(path: Path) -> tuple[dict, list[dict], list[dict], list[dict]]:
    meta = parse_statement_filename(path)
    pages = pdf_pages_as_lines(path)

    if not pages:
        raise ValueError(f"No text pages found in {path.name}")

    all_lines = flatten_pages(pages)

    summary = extract_summary(pages[0], meta)
    holdings = extract_holdings(all_lines, meta)
    returns = extract_monthly_returns(all_lines, meta)
    movements = extract_movements(all_lines, meta)

    return summary, holdings, returns, movements


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse legacy and modern GBM PDF statements."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to .config. Defaults to <project>/.config.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Override the GBM statement folder from .config.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output folder. Default: gbm/processed.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    configured_input = config.get("path_gbm_edo_cta")
    if args.input:
        input_dir = resolve_project_path(args.input)
    elif configured_input:
        input_dir = resolve_project_path(configured_input)
    else:
        raise KeyError(
            "Missing path_gbm_edo_cta in .config and no --input was provided."
        )

    output_dir = resolve_project_path(
        args.output
        or config.get("path_gbm_processed", "gbm/processed")
    )

    pdfs = discover_statements(input_dir)
    if not pdfs:
        raise FileNotFoundError(
            f"No supported CB_YYYYM_Contrato.pdf files found under {input_dir}"
        )

    summaries: list[dict] = []
    holdings: list[dict] = []
    returns: list[dict] = []
    movements: list[dict] = []
    errors: list[dict] = []

    for pdf in pdfs:
        try:
            summary, statement_holdings, statement_returns, statement_movements = (
                parse_statement(pdf)
            )
            summaries.append(summary)
            holdings.extend(statement_holdings)
            returns.extend(statement_returns)
            movements.extend(statement_movements)
            print(
                f"[OK] {pdf.name}: "
                f"{len(statement_holdings)} holdings, "
                f"{len(statement_returns)} return rows, "
                f"{len(statement_movements)} movements"
            )
        except Exception as exc:
            print(f"[ERROR] {pdf.name}: {exc}")
            errors.append(
                {
                    "source_file": pdf.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    summary_df = pd.DataFrame(summaries)
    holdings_df = pd.DataFrame(holdings)
    returns_df = pd.DataFrame(returns)
    movements_df = pd.DataFrame(movements)
    errors_df = pd.DataFrame(errors)

    if not summary_df.empty:
        summary_df = summary_df.sort_values(
            ["statement_date", "contract_file_id"]
        )
    if not holdings_df.empty:
        holdings_df = holdings_df.sort_values(
            ["statement_date", "ticker"]
        )
    if not returns_df.empty:
        returns_df = returns_df.sort_values(
            ["period_end", "source_statement_date"]
        )
    if not movements_df.empty:
        movements_df = movements_df.sort_values(
            ["operation_date", "folio"]
        )

    write_table(summary_df, output_dir / "statement_summary")
    write_table(holdings_df, output_dir / "holdings")
    write_table(returns_df, output_dir / "monthly_returns_raw")
    write_table(movements_df, output_dir / "movements")

    if not errors_df.empty:
        write_table(errors_df, output_dir / "parse_errors")

    print(f"\nOutput written to: {output_dir}")
    print("CSV is always created; Parquet is also created when pyarrow is installed.")


if __name__ == "__main__":
    main()
