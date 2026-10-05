from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gbm_utils import load_config, resolve_project_path, write_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a monthly GBM table for Power BI from the parsed statements."
        )
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


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    processed_dir = resolve_project_path(
        args.processed
        or config.get("path_gbm_processed", "gbm/processed")
    )

    summary = _read_csv(processed_dir / "statement_summary.csv")
    returns = _read_csv(processed_dir / "monthly_returns_raw.csv")
    movements = _read_csv(processed_dir / "movements.csv")

    if summary.empty:
        raise FileNotFoundError(
            f"No parsed statement_summary.csv found in {processed_dir}. "
            "Run extract_gbm_statements.py first."
        )

    # One row per contract/month. If the same PDF was parsed twice, keep the
    # latest occurrence deterministically.
    summary = (
        summary.sort_values(["statement_date", "source_file"])
        .drop_duplicates(
            subset=["year_month", "contract_file_id"],
            keep="last",
        )
    )

    # GBM repeats prior monthly returns in each new statement.
    # Keep the value found in the newest source statement for each period.
    if not returns.empty:
        returns = (
            returns.sort_values("source_statement_date")
            .drop_duplicates(
                subset=["return_year_month", "contract_file_id"],
                keep="last",
            )
        )

        returns_keep = returns[
            [
                "return_year_month",
                "contract_file_id",
                "gross_return_mxn",
                "gross_return_pct",
                "net_return_mxn",
                "net_return_pct",
            ]
        ].rename(columns={"return_year_month": "year_month"})

        history = summary.merge(
            returns_keep,
            on=["year_month", "contract_file_id"],
            how="left",
        )
    else:
        history = summary.copy()

    # External flows are the deposits/withdrawals that change how much money
    # the investor put into the portfolio. Trades, dividends and reportos are
    # intentionally excluded.
    if not movements.empty and "external_flow_mxn" in movements.columns:
        flow_rows = movements[movements["category"].isin(["contribution", "withdrawal"])].copy()

        if not flow_rows.empty:
            flow_rows["contribution_mxn"] = flow_rows["external_flow_mxn"].clip(lower=0)
            flow_rows["withdrawal_mxn"] = (
                -flow_rows["external_flow_mxn"].clip(upper=0)
            )

            flows = (
                flow_rows.groupby(
                    ["year_month", "contract_file_id"],
                    as_index=False,
                )
                .agg(
                    contributions_mxn=("contribution_mxn", "sum"),
                    withdrawals_mxn=("withdrawal_mxn", "sum"),
                    net_external_flow_mxn=("external_flow_mxn", "sum"),
                )
            )

            history = history.merge(
                flows,
                on=["year_month", "contract_file_id"],
                how="left",
            )

    for column in (
        "contributions_mxn",
        "withdrawals_mxn",
        "net_external_flow_mxn",
    ):
        if column not in history.columns:
            history[column] = 0.0
        history[column] = history[column].fillna(0.0)

    history["statement_date"] = pd.to_datetime(history["statement_date"])
    history = history.sort_values(
        ["statement_date", "contract_file_id"]
    )

    # Helpful cumulative cash-flow fields for the later inflation comparison.
    history["cumulative_contributions_mxn"] = history.groupby(
        "contract_file_id"
    )["contributions_mxn"].cumsum()

    history["cumulative_withdrawals_mxn"] = history.groupby(
        "contract_file_id"
    )["withdrawals_mxn"].cumsum()

    history["cumulative_net_contributed_mxn"] = (
        history["cumulative_contributions_mxn"]
        - history["cumulative_withdrawals_mxn"]
    )

    # Keep dates in ISO format for Power BI.
    history["statement_date"] = history["statement_date"].dt.date.astype(str)

    output = processed_dir / "gbm_portfolio_monthly"
    write_table(history, output)

    print(f"Created: {output.with_suffix('.csv')}")
    print(
        "Use this monthly table as the base for the Power BI model. "
        "The next step is to merge INPC by year_month and calculate "
        "inflation-adjusted contributions."
    )


if __name__ == "__main__":
    main()
