
from __future__ import annotations

from pathlib import Path

import pandas as pd

from crediclub_utils import (
    load_config,
    project_path,
    safe_round,
    write_table,
)


def main() -> None:
    config = load_config()
    processed_dir = project_path(
        config,
        "path_crediclub_processed",
        "crediclub/processed",
    )

    summary = pd.read_csv(processed_dir / "statement_summary.csv")
    snapshots = pd.read_csv(processed_dir / "account_snapshots.csv")
    positions = pd.read_csv(processed_dir / "fixed_term_positions.csv")
    movements = pd.read_csv(processed_dir / "movements.csv")
    validation = pd.read_csv(processed_dir / "validation_report.csv")

    failed = validation[validation["status"].eq("ERROR")]
    if not failed.empty:
        raise SystemExit(
            "Crediclub validation contains ERROR rows. "
            "Resolve them before building history."
        )

    # Account-level monthly history.
    account_monthly = snapshots.copy()

    for col in (
        "opening_balance_mxn",
        "deposits_mxn",
        "withdrawals_mxn",
        "closing_balance_mxn",
        "interest_gross_mxn",
        "isr_mxn",
        "interest_net_mxn",
    ):
        if col in account_monthly:
            account_monthly[col] = pd.to_numeric(
                account_monthly[col],
                errors="coerce",
            )

    write_table(
        account_monthly,
        processed_dir / "crediclub_account_monthly",
    )

    # Realized fixed-term earnings are recognized in the statement month in
    # which the maturity date falls. Prior statements carry principal only;
    # they do not provide a reliable accrued month-end market value.
    positions["maturity_date"] = pd.to_datetime(
        positions["maturity_date"],
        errors="coerce",
    )
    positions["maturity_month"] = positions["maturity_date"].dt.strftime("%Y-%m")

    matured = positions[
        positions["maturity_month"].eq(positions["year_month"])
        & positions["status_at_cutoff"].eq("MATURED_BY_CUTOFF")
    ].copy()

    fixed_earnings = (
        matured.groupby("year_month", as_index=False)
        .agg(
            fixed_term_gross_interest_realized_mxn=(
                "gross_interest_at_maturity_mxn",
                "sum",
            ),
            fixed_term_isr_realized_mxn=(
                "isr_at_maturity_mxn",
                "sum",
            ),
            fixed_term_net_interest_realized_mxn=(
                "net_interest_at_maturity_mxn",
                "sum",
            ),
        )
    )

    # Sight interest is already monthly.
    sight = account_monthly[
        account_monthly["product_type"].eq("SIGHT")
    ][
        [
            "year_month",
            "interest_gross_mxn",
            "isr_mxn",
            "interest_net_mxn",
        ]
    ].rename(
        columns={
            "interest_gross_mxn": "sight_gross_interest_mxn",
            "isr_mxn": "sight_isr_mxn",
            "interest_net_mxn": "sight_net_interest_mxn",
        }
    )

    flow = movements.copy()
    flow["signed_amount_mxn"] = pd.to_numeric(
        flow["signed_amount_mxn"],
        errors="coerce",
    ).fillna(0.0)

    external = flow[
        flow["flow_scope"].eq("external_candidate")
    ].copy()

    external["external_contribution_candidate_mxn"] = (
        external["signed_amount_mxn"].clip(lower=0)
    )
    external["external_withdrawal_candidate_mxn"] = (
        -external["signed_amount_mxn"].clip(upper=0)
    )

    external_monthly = (
        external.groupby("year_month", as_index=False)
        .agg(
            external_contribution_candidate_mxn=(
                "external_contribution_candidate_mxn",
                "sum",
            ),
            external_withdrawal_candidate_mxn=(
                "external_withdrawal_candidate_mxn",
                "sum",
            ),
        )
    )
    if not external_monthly.empty:
        external_monthly["net_external_flow_candidate_mxn"] = (
            external_monthly["external_contribution_candidate_mxn"]
            - external_monthly["external_withdrawal_candidate_mxn"]
        )

    consolidated = summary[
        [
            "year_month",
            "statement_end",
            "relationship_id",
            "statement_account_number",
            "layout_version",
            "derived_closing_portfolio_mxn",
            "reported_distribution_total_mxn",
            "derived_active_fixed_term_mxn",
        ]
    ].copy()

    consolidated = consolidated.merge(
        sight,
        on="year_month",
        how="left",
    )
    consolidated = consolidated.merge(
        fixed_earnings,
        on="year_month",
        how="left",
    )
    consolidated = consolidated.merge(
        external_monthly,
        on="year_month",
        how="left",
    )

    numeric_fill = [
        "sight_gross_interest_mxn",
        "sight_isr_mxn",
        "sight_net_interest_mxn",
        "fixed_term_gross_interest_realized_mxn",
        "fixed_term_isr_realized_mxn",
        "fixed_term_net_interest_realized_mxn",
        "external_contribution_candidate_mxn",
        "external_withdrawal_candidate_mxn",
        "net_external_flow_candidate_mxn",
    ]
    for col in numeric_fill:
        if col not in consolidated:
            consolidated[col] = 0.0
        consolidated[col] = pd.to_numeric(
            consolidated[col],
            errors="coerce",
        ).fillna(0.0)

    consolidated["gross_interest_realized_mxn"] = (
        consolidated["sight_gross_interest_mxn"]
        + consolidated["fixed_term_gross_interest_realized_mxn"]
    )
    consolidated["isr_realized_mxn"] = (
        consolidated["sight_isr_mxn"]
        + consolidated["fixed_term_isr_realized_mxn"]
    )
    consolidated["net_interest_realized_mxn"] = (
        consolidated["sight_net_interest_mxn"]
        + consolidated["fixed_term_net_interest_realized_mxn"]
    )

    write_table(
        consolidated,
        processed_dir / "crediclub_consolidated_monthly",
    )

    dim = (
        snapshots[
            [
                "relationship_id",
                "account_id",
                "product_type",
            ]
        ]
        .drop_duplicates()
        .sort_values(["relationship_id", "product_type"])
    )

    type_map = {
        "DEBIT": ("cash", True),
        "SIGHT": ("savings", True),
        "FIXED_TERM": ("fixed_income", True),
    }
    dim["institution"] = "CREDICLUB_SUPERTASAS"
    dim["account_type"] = dim["product_type"].map(
        lambda x: type_map[x][0]
    )
    dim["include_in_portfolio"] = dim["product_type"].map(
        lambda x: type_map[x][1]
    )

    write_table(
        dim,
        processed_dir / "dim_crediclub_account",
    )

    # Candidate external movements for the later cross-institution matcher.
    candidate_cols = [
        "transaction_date",
        "year_month",
        "relationship_id",
        "account_id",
        "product_type",
        "description",
        "category",
        "signed_amount_mxn",
        "deposit_mxn",
        "withdrawal_mxn",
        "source_file",
    ]
    cross_candidates = external[
        [c for c in candidate_cols if c in external.columns]
    ].copy()

    write_table(
        cross_candidates,
        processed_dir / "cross_institution_transfer_candidates",
    )

    internal = movements[
        movements["flow_scope"].eq("internal_crediclub")
    ].copy()
    write_table(
        internal,
        processed_dir / "internal_transfer_matches",
    )

    print("Crediclub monthly history built successfully.")


if __name__ == "__main__":
    main()
