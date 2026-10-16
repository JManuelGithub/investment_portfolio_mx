from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from gbm_utils import (
    discover_statements,
    extract_between_headers,
    extract_contract_id,
    extract_statement_period,
    flatten_pages,
    following_numbers,
    is_number,
    load_config,
    normalize_contract_id,
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

def _parse_numeric_cells(line: str) -> list[float] | None:
    """
    Parse one or more numeric table cells that PyMuPDF may merge into a
    single text line.

    Example:
        "0  56,200.000000" -> [0.0, 56200.0]

    Returns None when the line contains any non-numeric token, so table
    headings/tickers are never treated as numeric cells.
    """
    tokens = re.split(r"\s+", line.strip())
    if not tokens:
        return None
    if not all(is_number(token) for token in tokens):
        return None
    return [parse_number(token) for token in tokens]



def _summary_row_numbers(
    page1_lines: list[str],
    label: str,
    count: int = 3,
    *,
    contains: bool = False,
    ignore_legacy_footnote: bool = False,
) -> list[float]:
    from gbm_utils import find_line, find_line_contains

    finder = find_line_contains if contains else find_line
    idx = finder(page1_lines, label)

    values: list[float] = []
    for line in page1_lines[idx + 1 :]:
        if is_number(line):
            if ignore_legacy_footnote and not values and line.strip() in {"1", "¹"}:
                continue
            values.append(parse_number(line))
            if len(values) == count:
                return values
        elif values:
            break

    raise ValueError(
        f"Expected {count} numeric values after '{label}', found {len(values)}."
    )


def _statement_identity(path: Path, page1_lines: list[str]) -> dict:
    meta = parse_statement_filename(path)
    pdf_contract_id = extract_contract_id(page1_lines)
    file_contract_id = normalize_contract_id(meta.contract_file_id)
    contract_id = pdf_contract_id or file_contract_id

    if not contract_id:
        raise ValueError(f"Could not determine a contract ID for {path.name}")

    printed_period = extract_statement_period(page1_lines)
    if printed_period:
        pdf_period_start, pdf_period_end = printed_period
        period_matches_filename = (
            pdf_period_start.year == meta.year
            and pdf_period_start.month == meta.month
        )
    else:
        pdf_period_start = None
        pdf_period_end = None
        period_matches_filename = None

    contract_matches_filename = (
        None
        if pdf_contract_id is None or file_contract_id is None
        else pdf_contract_id == file_contract_id
    )

    return {
        "meta": meta,
        "contract_id": contract_id,
        "pdf_contract_id": pdf_contract_id,
        "contract_file_id": file_contract_id,
        "contract_matches_filename": contract_matches_filename,
        "pdf_period_start": pdf_period_start,
        "pdf_period_end": pdf_period_end,
        "period_matches_filename": period_matches_filename,
    }


def extract_summary(page1_lines: list[str], identity: dict) -> dict:
    meta = identity["meta"]

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

    fund_of_funds = row("FONDO DE FONDOS")
    other_investments = row("OTRAS INVERSIONES")
    margin_credits = row("CREDITOS DE MARGEN")
    guarantees = row("GARANTÍAS")
    short_values = row("VALORES EN CORTO / PRÉSTAMO DE VALORES")

    pdf_period_start = identity["pdf_period_start"]
    pdf_period_end = identity["pdf_period_end"]

    return {
        "statement_date": meta.period_end.isoformat(),
        "year": meta.year,
        "month": meta.month,
        "year_month": meta.year_month,
        "layout_version": meta.layout_version,
        "contract_id": identity["contract_id"],
        "pdf_contract_id": identity["pdf_contract_id"],
        "contract_file_id": identity["contract_file_id"],
        "contract_matches_filename": identity["contract_matches_filename"],
        "pdf_period_start": pdf_period_start.isoformat() if pdf_period_start else None,
        "pdf_period_end": pdf_period_end.isoformat() if pdf_period_end else None,
        "period_matches_filename": identity["period_matches_filename"],
        "source_file": meta.path.name,
        "source_path": str(meta.path.resolve()),
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


def _extract_exact_section(
    all_lines: list[str],
    start_header: str,
    end_headers: list[str],
) -> list[str]:
    """
    Extract a section using exact normalized header matches.

    This is important for GBM because "ACCIONES" and "ACCIONES DEL SIC"
    are different tables; a contains-based search would confuse them.
    """
    start_normalized = normalize_text(start_header)
    start_idx = None

    for idx, line in enumerate(all_lines):
        if normalize_text(line) == start_normalized:
            start_idx = idx + 1
            break

    if start_idx is None:
        return []

    end_normalized = {normalize_text(header) for header in end_headers}
    end_idx = len(all_lines)

    for idx in range(start_idx, len(all_lines)):
        if normalize_text(all_lines[idx]) in end_normalized:
            end_idx = idx
            break

    return all_lines[start_idx:end_idx]


def _parse_holdings_section(
    section: list[str],
    identity: dict,
    *,
    holding_section: str,
    market_scope: str,
) -> list[dict]:
    """Parse one GBM equity table (domestic ACCIONES or ACCIONES DEL SIC)."""
    meta = identity["meta"]
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
            parsed_cells = _parse_numeric_cells(current)
            if parsed_cells is None:
                break

            remaining = 10 - len(numeric_values)
            numeric_values.extend(parsed_cells[:remaining])
            j += 1

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
                    "contract_id": identity["contract_id"],
                    "contract_file_id": identity["contract_file_id"],
                    "holding_section": holding_section,
                    "market_scope": market_scope,
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


def extract_holdings(all_lines: list[str], identity: dict) -> list[dict]:
    """
    Extract all equity holdings from a GBM statement.

    GBM can split renta variable into at least two separate tables:
      - ACCIONES: Mexican/local-market securities (for example GRUMA B)
      - ACCIONES DEL SIC: securities listed through the SIC

    Both tables are combined into the same holdings output so their market
    values reconcile to RENTA VARIABLE in the statement summary.
    """
    rows: list[dict] = []

    domestic_section = _extract_exact_section(
        all_lines,
        "ACCIONES",
        [
            "Total: ACCIONES",
            "ACCIONES DEL SIC",
            "TOTAL: RENTA VARIABLE",
            "EFECTIVO",
        ],
    )
    if domestic_section:
        rows.extend(
            _parse_holdings_section(
                domestic_section,
                identity,
                holding_section="ACCIONES",
                market_scope="DOMESTIC",
            )
        )

    sic_section = _extract_exact_section(
        all_lines,
        "ACCIONES DEL SIC",
        [
            "Total: ACCIONES DEL SIC",
            "TOTAL: RENTA VARIABLE",
            "EFECTIVO",
        ],
    )
    if sic_section:
        rows.extend(
            _parse_holdings_section(
                sic_section,
                identity,
                holding_section="ACCIONES DEL SIC",
                market_scope="SIC",
            )
        )

    return rows


def extract_monthly_returns(all_lines: list[str], identity: dict) -> list[dict]:
    meta = identity["meta"]
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
            elif numbers:
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
                    "contract_id": identity["contract_id"],
                    "contract_file_id": identity["contract_file_id"],
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


def extract_movements(all_lines: list[str], identity: dict) -> list[dict]:
    meta = identity["meta"]
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
        i for i, line in enumerate(section) if MOVEMENT_DATE_RE.match(line.strip())
    ]

    rows: list[dict] = []
    for pos, start in enumerate(start_indices):
        end = start_indices[pos + 1] if pos + 1 < len(start_indices) else len(section)
        record = section[start:end]

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

        operation_date = safe_date(meta.year, meta.month, operation_day)
        flow_id = f"{identity['contract_id']}|{operation_date.isoformat()}|{folio}|{meta.path.name}"

        rows.append(
            {
                "statement_date": meta.period_end.isoformat(),
                "year_month": meta.year_month,
                "layout_version": meta.layout_version,
                "operation_date": operation_date.isoformat(),
                "operation_day": operation_day,
                "liquidation_day": liquidation_day,
                "folio": folio,
                "description": description,
                "issuer": issuer,
                "category": category,
                "net_mxn_reported": net,
                "balance_mxn_reported": balance,
                "external_flow_mxn": external_flow,
                "flow_id": flow_id,
                "raw_record_text": " | ".join(record),
                "contract_id": identity["contract_id"],
                "contract_file_id": identity["contract_file_id"],
                "source_file": meta.path.name,
            }
        )

    return rows


def parse_statement(path: Path) -> tuple[dict, list[dict], list[dict], list[dict]]:
    pages = pdf_pages_as_lines(path)
    if not pages:
        raise ValueError(f"No text pages found in {path.name}")

    identity = _statement_identity(path, pages[0])
    all_lines = flatten_pages(pages)

    summary = extract_summary(pages[0], identity)
    holdings = extract_holdings(all_lines, identity)
    returns = extract_monthly_returns(all_lines, identity)
    movements = extract_movements(all_lines, identity)

    return summary, holdings, returns, movements


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse legacy and modern GBM PDF statements with multi-contract support."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def _validate_contract_mode(summary: dict, mode: str) -> None:
    mismatch = summary.get("contract_matches_filename") is False
    if not mismatch:
        return

    message = (
        f"Contract mismatch in {summary['source_file']}: filename token "
        f"'{summary['contract_file_id']}' != PDF contract '{summary['contract_id']}'."
    )
    if mode == "error":
        raise ValueError(message)
    if mode == "warn":
        print(f"[WARN] {message}")


def _find_duplicate_contract_months(summary_df: pd.DataFrame) -> pd.DataFrame:
    if summary_df.empty:
        return pd.DataFrame()
    return summary_df[
        summary_df.duplicated(subset=["contract_id", "year_month"], keep=False)
    ].sort_values(["contract_id", "year_month", "source_file"])


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    configured_input = config.get("path_gbm_edo_cta")
    if args.input:
        input_dir = resolve_project_path(args.input)
    elif configured_input:
        input_dir = resolve_project_path(configured_input)
    else:
        raise KeyError("Missing path_gbm_edo_cta in .config and no --input was provided.")

    output_dir = resolve_project_path(
        args.output or config.get("path_gbm_processed", "gbm/processed")
    )

    contract_validation = config.get("contract_validation", "warn").strip().lower()
    if contract_validation not in {"error", "warn", "off"}:
        raise ValueError("contract_validation must be one of: error, warn, off")

    pdfs = discover_statements(input_dir)
    if not pdfs:
        raise FileNotFoundError(
            f"No CB_YYYYM_Contrato.pdf files found under {input_dir}"
        )

    summaries: list[dict] = []
    holdings: list[dict] = []
    returns: list[dict] = []
    movements: list[dict] = []
    errors: list[dict] = []

    for pdf in pdfs:
        try:
            summary, statement_holdings, statement_returns, statement_movements = parse_statement(pdf)
            if contract_validation != "off":
                _validate_contract_mode(summary, contract_validation)

            summaries.append(summary)
            holdings.extend(statement_holdings)
            returns.extend(statement_returns)
            movements.extend(statement_movements)
            print(
                f"[OK] {pdf.name} -> contract {summary['contract_id']}: "
                f"{len(statement_holdings)} holdings, "
                f"{len(statement_returns)} return rows, "
                f"{len(statement_movements)} movements"
            )
        except Exception as exc:
            print(f"[ERROR] {pdf.name}: {exc}")
            errors.append(
                {
                    "source_file": pdf.name,
                    "source_path": str(pdf.resolve()),
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
        summary_df = summary_df.sort_values(["statement_date", "contract_id", "source_file"])
    if not holdings_df.empty:
        holdings_df = holdings_df.sort_values(["statement_date", "contract_id", "ticker"])
    if not returns_df.empty:
        returns_df = returns_df.sort_values(["period_end", "contract_id", "source_statement_date"])
    if not movements_df.empty:
        movements_df = movements_df.sort_values(["operation_date", "contract_id", "folio"])

    duplicates = _find_duplicate_contract_months(summary_df)
    if not duplicates.empty:
        write_table(duplicates, output_dir / "duplicate_statements")
        raise ValueError(
            "Duplicate canonical contract/month statements were found. "
            f"Review {output_dir / 'duplicate_statements.csv'} before continuing."
        )

    write_table(summary_df, output_dir / "statement_summary")
    write_table(holdings_df, output_dir / "holdings")
    write_table(returns_df, output_dir / "monthly_returns_raw")
    write_table(movements_df, output_dir / "movements")

    if not errors_df.empty:
        write_table(errors_df, output_dir / "parse_errors")

    print(f"\nOutput written to: {output_dir}")
    print("CSV is always created; Parquet is also created when pyarrow is installed.")

    if errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
