from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from cetes_utils import (
    DATE_DMY2,
    amount_after_label,
    discover_statements,
    load_config,
    money_tokens,
    normalize_text,
    numeric_tokens,
    pdf_pages_as_lines,
    resolve_project_path,
    statement_metadata,
    write_table,
)


INSTRUMENTS = {
    "BONDDIA", "ENERFIN", "CETES", "BONDES", "BONOS", "UDIBONOS",
    "BPAG28", "BPAG91", "BPA182",
}

MONTH_LABEL_RE = re.compile(
    r"^(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)-(\d{2})$",
    re.IGNORECASE,
)

FOLIO_DESC_RE = re.compile(r"^(?P<folio>\S+)\s+(?P<description>.+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse CETESDirecto monthly PDF statements."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def _parse_position_table(
    page_lines: list[str],
    meta,
    position_type: str,
) -> list[dict]:
    """
    Parse 'Posición final del período' or 'Posición inicial del período'.

    Rows vary by instrument: BONDDIA may have a weighted rate but no days;
    CETES may have days but no rate; BONOS can have both.
    """
    try:
        total_idx = next(
            i for i, line in enumerate(page_lines)
            if normalize_text(line).startswith("POSICION TOTAL")
        )
    except StopIteration:
        return []

    # The first issuer row occurs after the last table header 'Emisora'
    # before Posición Total.
    issuer_starts = [
        i for i, line in enumerate(page_lines[:total_idx])
        if normalize_text(line) in INSTRUMENTS
    ]
    if not issuer_starts:
        return []

    rows: list[dict] = []
    for pos, start in enumerate(issuer_starts):
        end = issuer_starts[pos + 1] if pos + 1 < len(issuer_starts) else total_idx
        issuer = normalize_text(page_lines[start])
        cells = page_lines[start + 1:end]

        # Ignore any accidental header text inside the slice.
        cells = [
            c for c in cells
            if normalize_text(c) not in {
                "EMISORA", "SERIE", "TITULOS", "VALUACION",
                "% DE PARTICIPACION", "PRECIO PONDERADO",
                "PLAZO (DIAS POR VENCER)", "TASA PONDERADA",
            }
        ]

        if len(cells) < 6:
            continue

        series = cells[0].strip()
        try:
            titles = int(round(numeric_tokens(cells[1])[0]))
            market_price = numeric_tokens(cells[2])[0]
            valuation = numeric_tokens(cells[3])[0]
            participation_pct = numeric_tokens(cells[4])[0]
            weighted_price = numeric_tokens(cells[5])[0]
        except (IndexError, ValueError):
            continue

        days_to_maturity = None
        weighted_rate_pct = None
        for token in cells[6:]:
            nums = numeric_tokens(token)
            if not nums:
                continue
            if "%" in token:
                weighted_rate_pct = nums[0]
            else:
                # Days is the only remaining non-percent numeric field.
                if days_to_maturity is None:
                    days_to_maturity = int(round(nums[0]))

        rows.append(
            {
                "statement_date": meta.period_end.isoformat(),
                "year_month": meta.year_month,
                "account_id": meta.account_id,
                "position_type": position_type,
                "issuer": issuer,
                "series": series,
                "titles": titles,
                "market_price_mxn": market_price,
                "valuation_mxn": valuation,
                "participation_pct": participation_pct,
                "weighted_price_mxn": weighted_price,
                "days_to_maturity": days_to_maturity,
                "weighted_rate_pct": weighted_rate_pct,
                "source_file": meta.path.name,
            }
        )

    return rows


def extract_summary(page1: list[str], page2: list[str], meta) -> dict:
    return {
        "statement_date": meta.period_end.isoformat(),
        "period_start": meta.period_start.isoformat(),
        "period_end": meta.period_end.isoformat(),
        "year": meta.year,
        "month": meta.month,
        "year_month": meta.year_month,
        "account_id": meta.account_id,
        "filename_year": meta.filename_year,
        "filename_month": meta.filename_month,
        "filename_period_matches": meta.filename_period_matches,
        "source_file": meta.path.name,
        "source_path": str(meta.path.resolve()),
        "currency": "MXN",
        "portfolio_current_mxn": amount_after_label(page1, "Total de cartera:"),
        "cash_current_mxn": amount_after_label(page1, "Total de efectivo:"),
        "total_current_mxn": amount_after_label(page1, "Total final:"),
        "isr_period_mxn": amount_after_label(page1, "ISR Retenidodel período:"),
        "isr_ytd_mxn": amount_after_label(page1, "ISR Retenido acumulado del ejercicio fiscal:"),
        "cash_in_mxn": amount_after_label(page1, "Ingresos del período:"),
        "cash_out_mxn": amount_after_label(page1, "Egresos del período:"),
        "interest_period_mxn": amount_after_label(page1, "Intereses del período:"),
        "interest_ytd_mxn": amount_after_label(page1, "Intereses acumulados del ejercicio fiscal:"),
        "early_sale_result_ytd_mxn": amount_after_label(
            page1, "Resultado Acumulado por Venta Anticipada GUBER:", contains=True
        ),
        "early_sale_result_period_mxn": amount_after_label(
            page1, "Resultado por Venta Anticipada GUBER Período:", contains=True
        ),
        "bonddia_return_mxn": amount_after_label(page1, "Rendimiento BONDDIA:"),
        "enerfin_return_mxn": amount_after_label(page1, "Rendimiento ENERFIN:"),
        "portfolio_previous_mxn": amount_after_label(page2, "Total de cartera:"),
        "cash_previous_mxn": amount_after_label(page2, "Total de efectivo:"),
        "total_previous_mxn": amount_after_label(page2, "Total inicial:"),
    }


def _classify_movement(description: str) -> str:
    text = normalize_text(description)
    if "INGEFVO" in text:
        return "contribution"
    if "EGREFVO" in text:
        return "withdrawal"
    if re.search(r"\bISR\b", text):
        return "tax"
    if "PAGINTCU" in text:
        return "interest"
    if "VTASI" in text:
        return "fund_sale"
    if "COMPSI" in text:
        return "fund_purchase"
    if "VENTA" in text:
        return "sale"
    if "COMPRA" in text:
        return "purchase"
    if "AMORTIZACION" in text:
        return "redemption"
    return "other"


def extract_movements(page2: list[str], meta) -> list[dict]:
    """
    Each movement begins with two consecutive dd/mm/yy lines:
    registration date and settlement date.
    The final three numeric values in each record are Cargo, Abono and Saldo.
    """
    starts: list[int] = []
    for i in range(len(page2) - 1):
        if DATE_DMY2.match(page2[i].strip()) and DATE_DMY2.match(page2[i + 1].strip()):
            starts.append(i)

    rows: list[dict] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(page2)
        # Stop at Saldo final / personal-data footer.
        for j in range(start + 2, end):
            if normalize_text(page2[j]) in {"SALDO FINAL", "NOMBRE:"}:
                end = j
                break

        chunk = page2[start:end]
        if len(chunk) < 5:
            continue

        registration_date = _parse_date(chunk[0])
        settlement_date = _parse_date(chunk[1])

        first = chunk[2]
        match = FOLIO_DESC_RE.match(first)
        if match:
            folio = match.group("folio")
            description = match.group("description").strip()
            cursor = 3
        else:
            folio = first.strip()
            description = chunk[3].strip() if len(chunk) > 3 else ""
            cursor = 4

        issuer = chunk[cursor].strip() if cursor < len(chunk) else ""
        series = chunk[cursor + 1].strip() if cursor + 1 < len(chunk) else ""

        numeric_values: list[float] = []
        for line in chunk[cursor + 2:]:
            numeric_values.extend(numeric_tokens(line))

        if len(numeric_values) < 3:
            continue

        cargo, abono, balance = numeric_values[-3:]
        net_cash = abono - cargo
        category = _classify_movement(description)

        external_flow = 0.0
        if category in {"contribution", "withdrawal"}:
            external_flow = net_cash

        rows.append(
            {
                "statement_date": meta.period_end.isoformat(),
                "year_month": meta.year_month,
                "account_id": meta.account_id,
                "registration_date": registration_date.isoformat(),
                "settlement_date": settlement_date.isoformat(),
                "folio": folio,
                "description": description,
                "issuer": issuer,
                "series": series,
                "category": category,
                "cargo_mxn": cargo,
                "abono_mxn": abono,
                "net_cash_mxn": net_cash,
                "balance_mxn": balance,
                "external_flow_mxn": external_flow,
                "raw_record_text": " | ".join(chunk),
                "source_file": meta.path.name,
            }
        )

    return rows


def _parse_date(value: str):
    match = DATE_DMY2.match(value.strip())
    if not match:
        raise ValueError(value)
    return pd.to_datetime(value, format="%d/%m/%y").date()


def extract_returns(page3: list[str], meta) -> tuple[dict, list[dict]]:
    current_gross = None
    current_net = None
    for line in page3:
        match = re.search(r"Rendimiento del período:\s*(-?\d+(?:\.\d+)?)%", line, re.IGNORECASE)
        if match:
            current_gross = float(match.group(1))
        match = re.search(r"Rendimiento Neto del período:\s*(-?\d+(?:\.\d+)?)%", line, re.IGNORECASE)
        if match:
            current_net = float(match.group(1))

    current = {
        "statement_date": meta.period_end.isoformat(),
        "year_month": meta.year_month,
        "account_id": meta.account_id,
        "gross_return_pct": current_gross,
        "net_return_pct": current_net,
        "source_file": meta.path.name,
    }

    month_labels: list[str] = []
    for line in page3:
        if MONTH_LABEL_RE.match(line.strip()):
            month_labels.append(line.strip().lower())

    # Only percentages before the explicit current-period return lines belong
    # to the 12-month historical chart.
    chart_lines: list[str] = []
    for line in page3:
        if normalize_text(line).startswith("RENDIMIENTO DEL PERIODO:"):
            break
        chart_lines.append(line)

    percentages: list[float] = []
    for line in chart_lines:
        if re.fullmatch(r"-?\d+(?:\.\d+)?%", line.strip()):
            percentages.append(float(line.strip().replace("%", "")))

    history: list[dict] = []
    if month_labels and len(month_labels) == len(percentages):
        month_map = {
            "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
            "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
        }
        for label, value in zip(month_labels, percentages):
            abbr, yy = label.split("-")
            year = 2000 + int(yy)
            month = month_map[abbr]
            history.append(
                {
                    "return_year_month": f"{year:04d}-{month:02d}",
                    "account_id": meta.account_id,
                    "net_return_pct": value,
                    "source_statement_date": meta.period_end.isoformat(),
                    "source_file": meta.path.name,
                }
            )

    return current, history


def parse_statement(path: Path):
    pages = pdf_pages_as_lines(path)
    if len(pages) < 3:
        raise ValueError(f"Expected at least 3 pages in {path.name}")

    meta = statement_metadata(path, pages[0])
    if not meta.account_id:
        raise ValueError(f"Could not extract CETESDirecto account ID from {path.name}")

    summary = extract_summary(pages[0], pages[1], meta)
    final_positions = _parse_position_table(pages[0], meta, "final")
    initial_positions = _parse_position_table(pages[1], meta, "initial")
    movements = extract_movements(pages[1], meta)
    current_return, return_history = extract_returns(pages[2], meta)

    summary["gross_return_pct"] = current_return["gross_return_pct"]
    summary["net_return_pct"] = current_return["net_return_pct"]

    return summary, final_positions, initial_positions, movements, return_history


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    input_dir = resolve_project_path(
        args.input or config.get("path_cetes_edo_cta", "cetes/edo_cta")
    )
    output_dir = resolve_project_path(
        args.output or config.get("path_cetes_processed", "cetes/processed")
    )

    pdfs = discover_statements(input_dir)
    if not pdfs:
        raise FileNotFoundError(f"No PDF statements found under {input_dir}")

    summaries = []
    final_positions = []
    initial_positions = []
    movements = []
    returns = []
    errors = []

    for pdf in pdfs:
        try:
            summary, final_rows, initial_rows, mov_rows, return_rows = parse_statement(pdf)
            summaries.append(summary)
            final_positions.extend(final_rows)
            initial_positions.extend(initial_rows)
            movements.extend(mov_rows)
            returns.extend(return_rows)
            print(
                f"[OK] {pdf.name}: "
                f"{len(final_rows)} final positions, "
                f"{len(initial_rows)} initial positions, "
                f"{len(mov_rows)} movements"
            )
        except Exception as exc:
            print(f"[ERROR] {pdf.name}: {type(exc).__name__}: {exc}")
            errors.append({
                "source_file": pdf.name,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

    summary_df = pd.DataFrame(summaries)
    final_df = pd.DataFrame(final_positions)
    initial_df = pd.DataFrame(initial_positions)
    movements_df = pd.DataFrame(movements)
    returns_df = pd.DataFrame(returns)
    errors_df = pd.DataFrame(errors)

    if not summary_df.empty:
        summary_df = summary_df.sort_values(["statement_date", "account_id"])
    if not final_df.empty:
        final_df = final_df.sort_values(["statement_date", "account_id", "issuer", "series"])
    if not initial_df.empty:
        initial_df = initial_df.sort_values(["statement_date", "account_id", "issuer", "series"])
    if not movements_df.empty:
        movements_df = movements_df.sort_values(["registration_date", "account_id", "folio"])
    if not returns_df.empty:
        returns_df = returns_df.sort_values(["return_year_month", "source_statement_date"])

    write_table(summary_df, output_dir / "statement_summary")
    write_table(final_df, output_dir / "positions_final")
    write_table(initial_df, output_dir / "positions_initial")
    write_table(movements_df, output_dir / "movements")
    write_table(returns_df, output_dir / "monthly_returns_raw")
    if not errors_df.empty:
        write_table(errors_df, output_dir / "parse_errors")

    print(f"\nOutput written to: {output_dir}")


if __name__ == "__main__":
    main()
