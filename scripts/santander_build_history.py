from __future__ import annotations

import argparse

import pandas as pd

from santander_utils import load_config, resolve_project_path, write_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Santander monthly account and consolidated histories."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--processed", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    processed_dir = resolve_project_path(
        args.processed
        or config.get("path_santander_processed", "santander/processed")
    )

    summary = pd.read_csv(processed_dir / "statement_summary.csv")
    accounts = pd.read_csv(processed_dir / "account_snapshots.csv")
    movements = pd.read_csv(processed_dir / "movements.csv")

    # Account-level history.
    history = accounts.copy()

    flow_rows = movements.copy()
    flow_rows["external_contribution_candidate_mxn"] = 0.0
    flow_rows["external_withdrawal_candidate_mxn"] = 0.0
    external = flow_rows["flow_scope"].eq("external_candidate")

    flow_rows.loc[
        external & flow_rows["signed_amount_mxn"].gt(0),
        "external_contribution_candidate_mxn",
    ] = flow_rows.loc[
        external & flow_rows["signed_amount_mxn"].gt(0),
        "signed_amount_mxn",
    ]

    flow_rows.loc[
        external & flow_rows["signed_amount_mxn"].lt(0),
        "external_withdrawal_candidate_mxn",
    ] = -flow_rows.loc[
        external & flow_rows["signed_amount_mxn"].lt(0),
        "signed_amount_mxn",
    ]

    grouped = (
        flow_rows.groupby(["statement_month", "account_id"], as_index=False)
        .agg(
            external_contribution_candidate_mxn=("external_contribution_candidate_mxn", "sum"),
            external_withdrawal_candidate_mxn=("external_withdrawal_candidate_mxn", "sum"),
            interest_mxn=("deposit_mxn", lambda s: 0.0),
        )
        .rename(columns={"statement_month": "year_month"})
    )

    # Compute earnings separately because not every positive deposit is interest.
    earnings = (
        flow_rows[flow_rows["flow_scope"].eq("earnings")]
        .groupby(["statement_month", "account_id"], as_index=False)
        .agg(earnings_mxn=("signed_amount_mxn", "sum"))
        .rename(columns={"statement_month": "year_month"})
    )

    taxes = (
        flow_rows[flow_rows["flow_scope"].eq("tax")]
        .groupby(["statement_month", "account_id"], as_index=False)
        .agg(tax_withheld_mxn=("withdrawal_mxn", "sum"))
        .rename(columns={"statement_month": "year_month"})
    )

    history = history.merge(
        grouped.drop(columns=["interest_mxn"]),
        on=["year_month", "account_id"],
        how="left",
    )
    history = history.merge(
        earnings,
        on=["year_month", "account_id"],
        how="left",
    )
    history = history.merge(
        taxes,
        on=["year_month", "account_id"],
        how="left",
    )

    for column in [
        "external_contribution_candidate_mxn",
        "external_withdrawal_candidate_mxn",
        "earnings_mxn",
        "tax_withheld_mxn",
    ]:
        history[column] = pd.to_numeric(history[column], errors="coerce").fillna(0.0)

    history["net_earnings_after_tax_mxn"] = (
        history["earnings_mxn"] - history["tax_withheld_mxn"]
    )

    write_table(history, processed_dir / "santander_account_monthly")

    # Santander consolidated balance by month.
    consolidated = (
        history.groupby("year_month", as_index=False)
        .agg(
            total_previous_mxn=("opening_balance_mxn", "sum"),
            total_current_mxn=("closing_balance_mxn", "sum"),
            external_contribution_candidate_mxn=("external_contribution_candidate_mxn", "sum"),
            external_withdrawal_candidate_mxn=("external_withdrawal_candidate_mxn", "sum"),
            earnings_mxn=("earnings_mxn", "sum"),
            tax_withheld_mxn=("tax_withheld_mxn", "sum"),
            net_earnings_after_tax_mxn=("net_earnings_after_tax_mxn", "sum"),
        )
    )

    consolidated["net_external_flow_candidate_mxn"] = (
        consolidated["external_contribution_candidate_mxn"]
        - consolidated["external_withdrawal_candidate_mxn"]
    )

    consolidated = consolidated.merge(
        summary[
            [
                "year_month",
                "statement_date",
                "total_previous_mxn",
                "total_current_mxn",
                "interest_net_reported_mxn",
            ]
        ].rename(
            columns={
                "total_previous_mxn": "statement_previous_mxn",
                "total_current_mxn": "statement_current_mxn",
            }
        ),
        on="year_month",
        how="left",
    )

    write_table(consolidated, processed_dir / "santander_consolidated_monthly")

    # Account dimension.
    dim = (
        accounts[
            ["account_id", "account_number", "product_type", "product_name"]
        ]
        .drop_duplicates("account_id")
        .copy()
    )
    dim["institution"] = "Santander"
    dim["include_in_portfolio"] = True
    dim["account_type"] = dim["product_type"].map(
        {
            "CHECKING": "cash",
            "DINERO_CREC": "savings",
            "MIS_METAS": "savings",
        }
    ).fillna("cash")

    write_table(dim, processed_dir / "dim_santander_account")

    # Cross-institution candidates to be reconciled later against GBM/CETES.
    cross_candidates = movements[
        movements["flow_scope"].eq("external_candidate")
        & movements["category"].isin(["transfer_in", "transfer_out"])
    ].copy()

    if not cross_candidates.empty:
        write_table(
            cross_candidates,
            processed_dir / "cross_institution_transfer_candidates",
        )

    print(f"Created Santander monthly history in: {processed_dir}")


if __name__ == "__main__":
    main()
