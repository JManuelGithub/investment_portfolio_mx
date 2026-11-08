from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from cetes_utils import load_config, resolve_project_path, write_table


TOLERANCE_MXN = 1.00


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate CETESDirecto parsed statements."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--processed", default=None)
    return parser.parse_args()


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _coverage(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for account_id, group in summary.groupby("account_id"):
        months = sorted(pd.Period(x, freq="M") for x in group["year_month"].astype(str).unique())
        if not months:
            continue
        expected = pd.period_range(months[0], months[-1], freq="M")
        missing = [str(x) for x in expected if x not in months]
        rows.append({
            "account_id": account_id,
            "first_month": str(months[0]),
            "last_month": str(months[-1]),
            "statement_months": len(months),
            "expected_months": len(expected),
            "missing_month_count": len(missing),
            "missing_months": ";".join(missing),
        })
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    processed = resolve_project_path(
        args.processed or config.get("path_cetes_processed", "cetes/processed")
    )

    summary = _read(processed / "statement_summary.csv")
    final_pos = _read(processed / "positions_final.csv")
    initial_pos = _read(processed / "positions_initial.csv")
    movements = _read(processed / "movements.csv")

    if summary.empty:
        raise FileNotFoundError("statement_summary.csv is empty or missing")

    duplicate_mask = summary.duplicated(["account_id", "year_month"], keep=False)
    duplicates = summary.loc[duplicate_mask].copy()
    if not duplicates.empty:
        write_table(duplicates, processed / "duplicate_statements")

    final_totals = (
        final_pos.groupby(["account_id", "year_month"], as_index=False)
        .agg(
            final_position_count=("issuer", "count"),
            final_positions_mxn=("valuation_mxn", "sum"),
        )
        if not final_pos.empty
        else pd.DataFrame()
    )
    initial_totals = (
        initial_pos.groupby(["account_id", "year_month"], as_index=False)
        .agg(
            initial_position_count=("issuer", "count"),
            initial_positions_mxn=("valuation_mxn", "sum"),
        )
        if not initial_pos.empty
        else pd.DataFrame()
    )

    report = summary.copy()
    if not final_totals.empty:
        report = report.merge(final_totals, on=["account_id", "year_month"], how="left")
    else:
        report["final_position_count"] = 0
        report["final_positions_mxn"] = pd.NA

    if not initial_totals.empty:
        report = report.merge(initial_totals, on=["account_id", "year_month"], how="left")
    else:
        report["initial_position_count"] = 0
        report["initial_positions_mxn"] = pd.NA

    for col in ["final_position_count", "initial_position_count"]:
        report[col] = report[col].fillna(0).astype(int)

    report["final_positions_diff_mxn"] = (
        pd.to_numeric(report["final_positions_mxn"], errors="coerce")
        - pd.to_numeric(report["portfolio_current_mxn"], errors="coerce")
    )
    report["initial_positions_diff_mxn"] = (
        pd.to_numeric(report["initial_positions_mxn"], errors="coerce")
        - pd.to_numeric(report["portfolio_previous_mxn"], errors="coerce")
    )
    report["total_current_diff_mxn"] = (
        pd.to_numeric(report["portfolio_current_mxn"], errors="coerce")
        + pd.to_numeric(report["cash_current_mxn"], errors="coerce")
        - pd.to_numeric(report["total_current_mxn"], errors="coerce")
    )
    report["total_previous_diff_mxn"] = (
        pd.to_numeric(report["portfolio_previous_mxn"], errors="coerce")
        + pd.to_numeric(report["cash_previous_mxn"], errors="coerce")
        - pd.to_numeric(report["total_previous_mxn"], errors="coerce")
    )

    if not movements.empty:
        flow = movements[movements["category"].isin(["contribution", "withdrawal"])].copy()
        if not flow.empty:
            flow["contribution_mxn"] = flow["external_flow_mxn"].clip(lower=0)
            flow["withdrawal_mxn"] = -flow["external_flow_mxn"].clip(upper=0)
            flow_totals = (
                flow.groupby(["account_id", "year_month"], as_index=False)
                .agg(
                    movement_cash_in_mxn=("contribution_mxn", "sum"),
                    movement_cash_out_mxn=("withdrawal_mxn", "sum"),
                )
            )
            report = report.merge(flow_totals, on=["account_id", "year_month"], how="left")

    for col in ["movement_cash_in_mxn", "movement_cash_out_mxn"]:
        if col not in report:
            report[col] = 0.0
        report[col] = report[col].fillna(0.0)

    report["cash_in_diff_mxn"] = (
        report["movement_cash_in_mxn"]
        - pd.to_numeric(report["cash_in_mxn"], errors="coerce").fillna(0.0)
    )
    report["cash_out_diff_mxn"] = (
        report["movement_cash_out_mxn"]
        - pd.to_numeric(report["cash_out_mxn"], errors="coerce").fillna(0.0)
    )

    duplicate_keys = set(zip(duplicates.get("account_id", []), duplicates.get("year_month", [])))

    def status(row) -> str:
        issues = []
        if (row["account_id"], row["year_month"]) in duplicate_keys:
            issues.append("duplicate_account_month")
        if row.get("filename_period_matches") is False or str(row.get("filename_period_matches")).lower() == "false":
            issues.append("filename_period_mismatch")
        if row["final_position_count"] == 0 and abs(float(row["portfolio_current_mxn"])) > TOLERANCE_MXN:
            issues.append("final_positions_not_parsed")
        elif abs(float(row["final_positions_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("final_positions_total_mismatch")
        if row["initial_position_count"] == 0 and abs(float(row["portfolio_previous_mxn"])) > TOLERANCE_MXN:
            issues.append("initial_positions_not_parsed")
        elif abs(float(row["initial_positions_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("initial_positions_total_mismatch")
        if abs(float(row["total_current_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("current_total_mismatch")
        if abs(float(row["total_previous_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("previous_total_mismatch")
        if abs(float(row["cash_in_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("cash_in_mismatch")
        if abs(float(row["cash_out_diff_mxn"])) > TOLERANCE_MXN:
            issues.append("cash_out_mismatch")
        if pd.isna(row.get("net_return_pct")):
            issues.append("net_return_missing")
        return "OK" if not issues else ";".join(issues)

    report["validation_status"] = report.apply(status, axis=1)
    write_table(report, processed / "validation_report")

    coverage = _coverage(summary)
    write_table(coverage, processed / "account_coverage_report")

    problems = report[report["validation_status"] != "OK"]
    print(
        f"Validated {len(report)} statement month(s) across "
        f"{summary['account_id'].nunique()} CETESDirecto account(s)."
    )
    if problems.empty:
        print("All parsed statement rows passed validation.")
    else:
        print(f"WARNING: {len(problems)} statement month(s) need review.")
        print(
            problems[
                [
                    "account_id", "year_month", "source_file", "validation_status",
                    "final_positions_diff_mxn", "initial_positions_diff_mxn",
                    "cash_in_diff_mxn", "cash_out_diff_mxn",
                ]
            ].to_string(index=False)
        )

    missing = coverage[coverage["missing_month_count"] > 0] if not coverage.empty else pd.DataFrame()
    if not missing.empty:
        print("\nCoverage warning:")
        print(missing[["account_id", "missing_month_count", "missing_months"]].to_string(index=False))

    if not duplicates.empty:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
