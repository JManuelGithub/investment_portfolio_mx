from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from cetes_utils import load_config, resolve_project_path, write_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build monthly CETESDirecto history for later portfolio consolidation."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--processed", default=None)
    return parser.parse_args()


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _preserve_dim_account(summary: pd.DataFrame, processed: Path) -> pd.DataFrame:
    dim = (
        summary.groupby("account_id", as_index=False)
        .agg(
            first_statement_date=("statement_date", "min"),
            last_statement_date=("statement_date", "max"),
            statement_count=("year_month", "nunique"),
        )
    )
    dim.insert(1, "account_name", "")
    dim.insert(2, "institution", "CETESDirecto")
    dim.insert(3, "account_type", "investment")
    dim.insert(4, "include_in_portfolio", True)

    existing_path = processed / "dim_cetes_account.csv"
    if existing_path.exists():
        existing = pd.read_csv(existing_path)
        keep = [
            c for c in [
                "account_id", "account_name", "account_type", "include_in_portfolio"
            ]
            if c in existing.columns
        ]
        if "account_id" in keep:
            old = existing[keep].drop_duplicates("account_id", keep="last")
            dim = dim.merge(old, on="account_id", how="left", suffixes=("", "_old"))
            for col in ["account_name", "account_type", "include_in_portfolio"]:
                old_col = f"{col}_old"
                if old_col in dim:
                    dim[col] = dim[old_col].where(dim[old_col].notna(), dim[col])
                    dim = dim.drop(columns=[old_col])

    dim["include_in_portfolio"] = (
        dim["include_in_portfolio"].astype(str).str.lower().isin({"true", "1", "yes"})
    )
    write_table(dim, processed / "dim_cetes_account")
    return dim


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    processed = resolve_project_path(
        args.processed or config.get("path_cetes_processed", "cetes/processed")
    )

    summary = _read(processed / "statement_summary.csv")
    movements = _read(processed / "movements.csv")
    returns = _read(processed / "monthly_returns_raw.csv")

    if summary.empty:
        raise FileNotFoundError("Run cetes_extract_statements.py first.")

    duplicates = summary[summary.duplicated(["account_id", "year_month"], keep=False)]
    if not duplicates.empty:
        raise ValueError(
            "Duplicate CETESDirecto account/month rows found. "
            "Run cetes_validate_statements.py and resolve them first."
        )

    dim = _preserve_dim_account(summary, processed)

    if not returns.empty:
        returns = (
            returns.sort_values("source_statement_date")
            .drop_duplicates(["account_id", "return_year_month"], keep="last")
            [["account_id", "return_year_month", "net_return_pct"]]
            .rename(columns={"return_year_month": "year_month", "net_return_pct": "chart_net_return_pct"})
        )
        history = summary.merge(returns, on=["account_id", "year_month"], how="left")
    else:
        history = summary.copy()
        history["chart_net_return_pct"] = pd.NA

    if not movements.empty:
        flow = movements[movements["category"].isin(["contribution", "withdrawal"])].copy()
        if not flow.empty:
            flow["contribution_mxn"] = flow["external_flow_mxn"].clip(lower=0)
            flow["withdrawal_mxn"] = -flow["external_flow_mxn"].clip(upper=0)
            flows = (
                flow.groupby(["account_id", "year_month"], as_index=False)
                .agg(
                    contributions_mxn=("contribution_mxn", "sum"),
                    withdrawals_mxn=("withdrawal_mxn", "sum"),
                    net_external_flow_mxn=("external_flow_mxn", "sum"),
                )
            )
            history = history.merge(flows, on=["account_id", "year_month"], how="left")

    for col in ["contributions_mxn", "withdrawals_mxn", "net_external_flow_mxn"]:
        if col not in history:
            history[col] = 0.0
        history[col] = history[col].fillna(0.0)

    history["statement_date"] = pd.to_datetime(history["statement_date"])
    history = history.sort_values(["account_id", "statement_date"])
    history["cumulative_contributions_mxn"] = history.groupby("account_id")["contributions_mxn"].cumsum()
    history["cumulative_withdrawals_mxn"] = history.groupby("account_id")["withdrawals_mxn"].cumsum()
    history["cumulative_net_contributed_mxn"] = (
        history["cumulative_contributions_mxn"] - history["cumulative_withdrawals_mxn"]
    )
    history["statement_date"] = history["statement_date"].dt.date.astype(str)

    write_table(history, processed / "cetes_portfolio_monthly")

    included = set(
        dim.loc[dim["include_in_portfolio"], "account_id"].astype(str)
    )
    consolidated = history[history["account_id"].astype(str).isin(included)].copy()

    if consolidated.empty:
        consolidated_monthly = pd.DataFrame()
    else:
        consolidated_monthly = (
            consolidated.groupby("year_month", as_index=False)
            .agg(
                statement_date=("statement_date", "max"),
                portfolio_previous_mxn=("portfolio_previous_mxn", "sum"),
                cash_previous_mxn=("cash_previous_mxn", "sum"),
                total_previous_mxn=("total_previous_mxn", "sum"),
                portfolio_current_mxn=("portfolio_current_mxn", "sum"),
                cash_current_mxn=("cash_current_mxn", "sum"),
                total_current_mxn=("total_current_mxn", "sum"),
                contributions_mxn=("contributions_mxn", "sum"),
                withdrawals_mxn=("withdrawals_mxn", "sum"),
                net_external_flow_mxn=("net_external_flow_mxn", "sum"),
            )
            .sort_values("year_month")
        )

    write_table(consolidated_monthly, processed / "cetes_consolidated_monthly")

    print(f"Created: {processed / 'cetes_portfolio_monthly.csv'}")
    print(f"Created: {processed / 'cetes_consolidated_monthly.csv'}")
    print("These are ready to be combined later with GBM and Santander.")


if __name__ == "__main__":
    main()
