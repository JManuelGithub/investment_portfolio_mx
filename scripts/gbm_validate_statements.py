from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gbm_utils import load_config, resolve_project_path, write_table


TOLERANCE_MXN = 1.00


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate parsed GBM statements, multi-contract identity, coverage and transfer matching."
        )
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--processed", default=None)
    return parser.parse_args()


def _to_bool_or_none(value):
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _contract_coverage(summary: pd.DataFrame) -> pd.DataFrame:
    records: list[dict] = []
    for contract_id, group in summary.groupby("contract_id"):
        months = sorted(pd.Period(x, freq="M") for x in group["year_month"].dropna().astype(str).unique())
        if not months:
            continue
        expected = pd.period_range(months[0], months[-1], freq="M")
        missing = [str(period) for period in expected if period not in months]
        records.append(
            {
                "contract_id": contract_id,
                "first_month": str(months[0]),
                "last_month": str(months[-1]),
                "statement_months": len(months),
                "expected_months_between_first_last": len(expected),
                "missing_month_count": len(missing),
                "missing_months": ";".join(missing),
            }
        )
    return pd.DataFrame(records)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    processed_dir = resolve_project_path(
        args.processed or config.get("path_gbm_processed", "gbm/processed")
    )

    summary_path = processed_dir / "statement_summary.csv"
    holdings_path = processed_dir / "holdings.csv"
    returns_path = processed_dir / "monthly_returns_raw.csv"
    ambiguous_path = processed_dir / "internal_transfer_ambiguous.csv"

    if not summary_path.exists():
        raise FileNotFoundError(
            f"{summary_path} does not exist. Run extract_gbm_statements.py first."
        )

    summary = pd.read_csv(summary_path)
    holdings = pd.read_csv(holdings_path) if holdings_path.exists() else pd.DataFrame()
    returns = pd.read_csv(returns_path) if returns_path.exists() else pd.DataFrame()
    ambiguous = pd.read_csv(ambiguous_path) if ambiguous_path.exists() else pd.DataFrame()

    duplicate_mask = summary.duplicated(subset=["contract_id", "year_month"], keep=False)
    duplicates = summary.loc[duplicate_mask].copy()
    if not duplicates.empty:
        write_table(duplicates, processed_dir / "duplicate_statements")

    base_cols = [
        "statement_date",
        "year_month",
        "contract_id",
        "pdf_contract_id",
        "contract_file_id",
        "contract_matches_filename",
        "period_matches_filename",
        "layout_version",
        "source_file",
        "variable_income_current_mxn",
        "cash_current_mxn",
        "portfolio_value_current_mxn",
    ]
    report = summary[[c for c in base_cols if c in summary.columns]].copy()

    if not holdings.empty:
        holding_totals = (
            holdings.groupby(["year_month", "contract_id"], as_index=False)
            .agg(
                holdings_count=("ticker", "count"),
                holdings_market_value_mxn=("market_value_mxn", "sum"),
            )
        )
        report = report.merge(holding_totals, on=["year_month", "contract_id"], how="left")
    else:
        report["holdings_count"] = 0
        report["holdings_market_value_mxn"] = pd.NA

    report["holdings_count"] = report["holdings_count"].fillna(0).astype(int)
    report["holdings_vs_variable_income_diff_mxn"] = (
        pd.to_numeric(report["holdings_market_value_mxn"], errors="coerce")
        - pd.to_numeric(report["variable_income_current_mxn"], errors="coerce")
    )
    report["holdings_match_variable_income"] = (
        report["holdings_vs_variable_income_diff_mxn"].abs().le(TOLERANCE_MXN)
    )

    if not returns.empty:
        current_returns = (
            returns.sort_values("source_statement_date")
            .drop_duplicates(subset=["return_year_month", "contract_id"], keep="last")
            [["return_year_month", "contract_id", "net_return_pct"]]
            .rename(columns={"return_year_month": "year_month"})
        )
        report = report.merge(current_returns, on=["year_month", "contract_id"], how="left")
        report["current_month_return_found"] = report["net_return_pct"].notna()
    else:
        report["net_return_pct"] = pd.NA
        report["current_month_return_found"] = False

    duplicate_keys = set(
        zip(duplicates.get("contract_id", []), duplicates.get("year_month", []))
    )

    def status(row) -> str:
        issues: list[str] = []

        key = (row.get("contract_id"), row.get("year_month"))
        if key in duplicate_keys:
            issues.append("duplicate_contract_month")

        contract_match = _to_bool_or_none(row.get("contract_matches_filename"))
        if contract_match is False:
            issues.append("filename_contract_mismatch")

        period_match = _to_bool_or_none(row.get("period_matches_filename"))
        if period_match is False:
            issues.append("filename_period_mismatch")

        variable_income = float(row.get("variable_income_current_mxn") or 0.0)
        if row["holdings_count"] == 0 and abs(variable_income) > TOLERANCE_MXN:
            issues.append("holdings_not_parsed")
        elif row["holdings_count"] > 0 and not bool(row["holdings_match_variable_income"]):
            issues.append("holdings_total_mismatch")

        if not bool(row["current_month_return_found"]):
            issues.append("current_month_return_not_found")

        return "OK" if not issues else ";".join(issues)

    report["validation_status"] = report.apply(status, axis=1)
    write_table(report, processed_dir / "validation_report")

    coverage = _contract_coverage(summary)
    write_table(coverage, processed_dir / "contract_coverage_report")

    problems = report[report["validation_status"] != "OK"]
    missing_coverage = coverage[coverage["missing_month_count"] > 0] if not coverage.empty else pd.DataFrame()

    print(f"Validated {len(report)} statement month(s) across {summary['contract_id'].nunique()} contract(s).")
    if problems.empty:
        print("All parsed statement rows passed the structural validation rules.")
    else:
        print(f"WARNING: {len(problems)} statement month(s) need review.")
        print(
            problems[
                [
                    "contract_id",
                    "year_month",
                    "source_file",
                    "validation_status",
                    "holdings_vs_variable_income_diff_mxn",
                ]
            ].to_string(index=False)
        )

    if not missing_coverage.empty:
        print("\nCoverage warning: some contracts have missing statement months between their first and last files.")
        print(
            missing_coverage[
                ["contract_id", "missing_month_count", "missing_months"]
            ].to_string(index=False)
        )

    if not ambiguous.empty:
        print(
            f"\nTransfer warning: {len(ambiguous)} ambiguous internal-transfer candidate row(s) remain external."
        )

    if not duplicates.empty:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
