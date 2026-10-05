from __future__ import annotations

import argparse

import pandas as pd

from gbm_utils import load_config, resolve_project_path, write_table


TOLERANCE_MXN = 1.00


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the GBM parser outputs and flag statement-format changes."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to .config. Defaults to <project>/.config.",
    )
    parser.add_argument(
        "--processed",
        default=None,
        help="Parsed GBM folder. Default: gbm/processed.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    processed_dir = resolve_project_path(
        args.processed
        or config.get("path_gbm_processed", "gbm/processed")
    )

    summary_path = processed_dir / "statement_summary.csv"
    holdings_path = processed_dir / "holdings.csv"
    returns_path = processed_dir / "monthly_returns_raw.csv"

    if not summary_path.exists():
        raise FileNotFoundError(
            f"{summary_path} does not exist. Run extract_gbm_statements.py first."
        )

    summary = pd.read_csv(summary_path)
    holdings = (
        pd.read_csv(holdings_path)
        if holdings_path.exists()
        else pd.DataFrame()
    )
    returns = (
        pd.read_csv(returns_path)
        if returns_path.exists()
        else pd.DataFrame()
    )

    report = summary[
        [
            "statement_date",
            "year_month",
            "contract_file_id",
            "source_file",
            "variable_income_current_mxn",
            "cash_current_mxn",
            "portfolio_value_current_mxn",
        ]
    ].copy()

    if not holdings.empty:
        holding_totals = (
            holdings.groupby(
                ["year_month", "contract_file_id"],
                as_index=False,
            )
            .agg(
                holdings_count=("ticker", "count"),
                holdings_market_value_mxn=("market_value_mxn", "sum"),
            )
        )
        report = report.merge(
            holding_totals,
            on=["year_month", "contract_file_id"],
            how="left",
        )
    else:
        report["holdings_count"] = 0
        report["holdings_market_value_mxn"] = pd.NA

    report["holdings_count"] = report["holdings_count"].fillna(0).astype(int)
    report["holdings_vs_variable_income_diff_mxn"] = (
        report["holdings_market_value_mxn"]
        - report["variable_income_current_mxn"]
    )

    # Do not assume portfolio = equity + cash because future statements may
    # include debt, funds, guarantees, derivatives or other asset classes.
    # Instead, only validate the table we explicitly parsed: renta variable.
    report["holdings_match_variable_income"] = (
        report["holdings_vs_variable_income_diff_mxn"]
        .abs()
        .le(TOLERANCE_MXN)
    )

    if not returns.empty:
        current_returns = (
            returns.sort_values("source_statement_date")
            .drop_duplicates(
                subset=["return_year_month", "contract_file_id"],
                keep="last",
            )
            [["return_year_month", "contract_file_id", "net_return_pct"]]
            .rename(columns={"return_year_month": "year_month"})
        )
        report = report.merge(
            current_returns,
            on=["year_month", "contract_file_id"],
            how="left",
        )
        report["current_month_return_found"] = report["net_return_pct"].notna()
    else:
        report["net_return_pct"] = pd.NA
        report["current_month_return_found"] = False

    def status(row) -> str:
        issues: list[str] = []
        if row["holdings_count"] == 0 and row["variable_income_current_mxn"] != 0:
            issues.append("holdings_not_parsed")
        elif (
            row["holdings_count"] > 0
            and not bool(row["holdings_match_variable_income"])
        ):
            issues.append("holdings_total_mismatch")

        if not bool(row["current_month_return_found"]):
            issues.append("current_month_return_not_found")

        return "OK" if not issues else ";".join(issues)

    report["validation_status"] = report.apply(status, axis=1)

    write_table(report, processed_dir / "validation_report")

    problems = report[report["validation_status"] != "OK"]

    print(f"Validated {len(report)} statement month(s).")
    if problems.empty:
        print("All parsed statements passed the current validation rules.")
    else:
        print(
            f"WARNING: {len(problems)} statement month(s) need review. "
            "This can indicate a GBM layout change or a parser edge case."
        )
        print(
            problems[
                [
                    "source_file",
                    "validation_status",
                    "holdings_vs_variable_income_diff_mxn",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
