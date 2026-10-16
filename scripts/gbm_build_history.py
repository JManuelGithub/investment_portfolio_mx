from __future__ import annotations

import argparse
import calendar
from pathlib import Path
from uuid import uuid4

import pandas as pd

from gbm_utils import (
    config_float,
    config_int,
    load_config,
    resolve_project_path,
    write_table,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build per-contract and consolidated monthly GBM histories for Power BI."
        )
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--processed", default=None)
    return parser.parse_args()


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _ensure_unique_contract_month(summary: pd.DataFrame) -> None:
    duplicates = summary[
        summary.duplicated(subset=["contract_id", "year_month"], keep=False)
    ]
    if not duplicates.empty:
        raise ValueError(
            "statement_summary.csv contains duplicate contract/month rows. "
            "Run extract_gbm_statements.py and resolve duplicate_statements.csv first."
        )


def _preserve_dim_contract(summary: pd.DataFrame, processed_dir: Path) -> pd.DataFrame:
    discovered = (
        summary.groupby("contract_id", as_index=False)
        .agg(
            first_statement_date=("statement_date", "min"),
            last_statement_date=("statement_date", "max"),
            statement_count=("year_month", "nunique"),
            filename_contract_tokens=(
                "contract_file_id",
                lambda s: ";".join(sorted({str(x) for x in s.dropna()})),
            ),
        )
    )
    discovered.insert(1, "contract_name", "")
    discovered.insert(2, "broker", "GBM")
    discovered.insert(3, "include_in_portfolio", True)

    existing_path = processed_dir / "dim_contract.csv"
    if existing_path.exists():
        existing = pd.read_csv(existing_path)
        preserve_cols = [
            col
            for col in ["contract_id", "contract_name", "include_in_portfolio"]
            if col in existing.columns
        ]
        if "contract_id" in preserve_cols:
            existing_keep = existing[preserve_cols].drop_duplicates("contract_id", keep="last")
            discovered = discovered.merge(
                existing_keep,
                on="contract_id",
                how="left",
                suffixes=("", "_existing"),
            )
            if "contract_name_existing" in discovered:
                discovered["contract_name"] = discovered["contract_name_existing"].fillna(
                    discovered["contract_name"]
                )
                discovered = discovered.drop(columns=["contract_name_existing"])
            if "include_in_portfolio_existing" in discovered:
                discovered["include_in_portfolio"] = discovered[
                    "include_in_portfolio_existing"
                ].where(
                    discovered["include_in_portfolio_existing"].notna(),
                    discovered["include_in_portfolio"],
                )
                discovered = discovered.drop(columns=["include_in_portfolio_existing"])

    discovered["include_in_portfolio"] = (
        discovered["include_in_portfolio"]
        .astype(str)
        .str.lower()
        .isin({"true", "1", "yes"})
    )
    write_table(discovered, processed_dir / "dim_contract")
    return discovered


def _load_manual_transfer_overrides(
    config: dict[str, str],
    tolerance: float,
    flows: pd.DataFrame,
) -> list[tuple[str, str]]:
    configured = config.get("path_gbm_internal_transfer_overrides")
    if not configured:
        return []

    path = resolve_project_path(configured)
    if not path.exists():
        return []

    overrides = pd.read_csv(path)
    required = {"out_flow_id", "in_flow_id"}
    if not required.issubset(overrides.columns):
        raise ValueError(
            f"{path} must contain columns: out_flow_id, in_flow_id"
        )

    by_id = flows.set_index("flow_id", drop=False)
    pairs: list[tuple[str, str]] = []
    for row in overrides.itertuples(index=False):
        out_id = str(row.out_flow_id)
        in_id = str(row.in_flow_id)
        if out_id not in by_id.index or in_id not in by_id.index:
            raise ValueError(f"Manual transfer override references an unknown flow: {out_id}, {in_id}")

        out_row = by_id.loc[out_id]
        in_row = by_id.loc[in_id]
        if out_row["contract_id"] == in_row["contract_id"]:
            raise ValueError("Internal transfer override must use two different contracts.")
        if float(out_row["external_flow_mxn"]) >= 0 or float(in_row["external_flow_mxn"]) <= 0:
            raise ValueError("Override must pair a withdrawal (out) with a contribution (in).")
        if abs(abs(float(out_row["external_flow_mxn"])) - float(in_row["external_flow_mxn"])) > tolerance:
            raise ValueError("Override amounts differ by more than the configured tolerance.")
        pairs.append((out_id, in_id))

    return pairs


def _classify_internal_transfers(
    movements: pd.DataFrame,
    config: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if movements.empty:
        return movements.copy(), pd.DataFrame(), pd.DataFrame()

    tolerance = config_float(config, "internal_transfer_tolerance_mxn", 1.00)
    window_days = config_int(config, "internal_transfer_window_days", 3)

    df = movements.copy()
    df["operation_date"] = pd.to_datetime(df["operation_date"])
    df["external_flow_mxn"] = pd.to_numeric(df["external_flow_mxn"], errors="coerce").fillna(0.0)
    df["flow_scope"] = "not_applicable"
    flow_mask = df["category"].isin(["contribution", "withdrawal"]) & df["external_flow_mxn"].ne(0)
    df.loc[flow_mask, "flow_scope"] = "external"
    df["transfer_pair_id"] = pd.NA
    df["external_flow_consolidated_mxn"] = df["external_flow_mxn"]

    flows = df.loc[flow_mask].copy()
    matched_ids: set[str] = set()
    matches: list[dict] = []

    def register_pair(out_id: str, in_id: str, method: str) -> None:
        nonlocal df
        out_row = flows.loc[flows["flow_id"] == out_id].iloc[0]
        in_row = flows.loc[flows["flow_id"] == in_id].iloc[0]
        pair_id = f"IT-{uuid4().hex[:12]}"
        matched_ids.update({out_id, in_id})

        mask = df["flow_id"].isin([out_id, in_id])
        df.loc[mask, "flow_scope"] = "internal_transfer"
        df.loc[mask, "transfer_pair_id"] = pair_id
        df.loc[mask, "external_flow_consolidated_mxn"] = 0.0

        matches.append(
            {
                "transfer_pair_id": pair_id,
                "match_method": method,
                "out_flow_id": out_id,
                "out_contract_id": out_row["contract_id"],
                "out_date": out_row["operation_date"].date().isoformat(),
                "in_flow_id": in_id,
                "in_contract_id": in_row["contract_id"],
                "in_date": in_row["operation_date"].date().isoformat(),
                "amount_mxn": abs(float(out_row["external_flow_mxn"])),
                "date_difference_days": abs(
                    (in_row["operation_date"] - out_row["operation_date"]).days
                ),
            }
        )

    for out_id, in_id in _load_manual_transfer_overrides(config, tolerance, flows):
        register_pair(out_id, in_id, "manual_override")

    remaining = flows[~flows["flow_id"].isin(matched_ids)].copy()
    withdrawals = remaining[remaining["external_flow_mxn"] < 0]
    contributions = remaining[remaining["external_flow_mxn"] > 0]

    candidates: list[tuple[str, str]] = []
    for out_row in withdrawals.itertuples(index=False):
        amount = abs(float(out_row.external_flow_mxn))
        possible = contributions[
            (contributions["contract_id"] != out_row.contract_id)
            & ((contributions["external_flow_mxn"] - amount).abs() <= tolerance)
            & (
                (contributions["operation_date"] - out_row.operation_date)
                .dt.days.abs()
                .le(window_days)
            )
        ]
        for in_row in possible.itertuples(index=False):
            candidates.append((str(out_row.flow_id), str(in_row.flow_id)))

    out_counts: dict[str, int] = {}
    in_counts: dict[str, int] = {}
    for out_id, in_id in candidates:
        out_counts[out_id] = out_counts.get(out_id, 0) + 1
        in_counts[in_id] = in_counts.get(in_id, 0) + 1

    for out_id, in_id in candidates:
        if out_counts[out_id] == 1 and in_counts[in_id] == 1:
            if out_id not in matched_ids and in_id not in matched_ids:
                register_pair(out_id, in_id, "auto_unique_amount_date")

    unresolved_pairs = [
        (out_id, in_id)
        for out_id, in_id in candidates
        if out_id not in matched_ids and in_id not in matched_ids
    ]
    ambiguous_records: list[dict] = []
    for out_id, in_id in unresolved_pairs:
        out_row = flows.loc[flows["flow_id"] == out_id].iloc[0]
        in_row = flows.loc[flows["flow_id"] == in_id].iloc[0]
        ambiguous_records.append(
            {
                "out_flow_id": out_id,
                "out_contract_id": out_row["contract_id"],
                "out_date": out_row["operation_date"].date().isoformat(),
                "in_flow_id": in_id,
                "in_contract_id": in_row["contract_id"],
                "in_date": in_row["operation_date"].date().isoformat(),
                "amount_mxn": abs(float(out_row["external_flow_mxn"])),
                "reason": "multiple_possible_matches",
            }
        )

    df["operation_date"] = df["operation_date"].dt.date.astype(str)
    return df, pd.DataFrame(matches), pd.DataFrame(ambiguous_records)


def _build_contract_history(
    summary: pd.DataFrame,
    returns: pd.DataFrame,
    movements: pd.DataFrame,
) -> pd.DataFrame:
    if not returns.empty:
        returns = (
            returns.sort_values("source_statement_date")
            .drop_duplicates(subset=["return_year_month", "contract_id"], keep="last")
        )
        returns_keep = returns[
            [
                "return_year_month",
                "contract_id",
                "gross_return_mxn",
                "gross_return_pct",
                "net_return_mxn",
                "net_return_pct",
            ]
        ].rename(columns={"return_year_month": "year_month"})
        history = summary.merge(returns_keep, on=["year_month", "contract_id"], how="left")
    else:
        history = summary.copy()

    flow_rows = movements[movements["category"].isin(["contribution", "withdrawal"])].copy()
    if not flow_rows.empty:
        flow_rows["external_contribution_mxn"] = flow_rows["external_flow_consolidated_mxn"].clip(lower=0)
        flow_rows["external_withdrawal_mxn"] = -flow_rows["external_flow_consolidated_mxn"].clip(upper=0)
        flow_rows["raw_contribution_mxn"] = flow_rows["external_flow_mxn"].clip(lower=0)
        flow_rows["raw_withdrawal_mxn"] = -flow_rows["external_flow_mxn"].clip(upper=0)
        flow_rows["internal_transfer_in_mxn"] = 0.0
        flow_rows["internal_transfer_out_mxn"] = 0.0
        internal = flow_rows["flow_scope"].eq("internal_transfer")
        flow_rows.loc[internal & flow_rows["external_flow_mxn"].gt(0), "internal_transfer_in_mxn"] = flow_rows.loc[
            internal & flow_rows["external_flow_mxn"].gt(0), "external_flow_mxn"
        ]
        flow_rows.loc[internal & flow_rows["external_flow_mxn"].lt(0), "internal_transfer_out_mxn"] = -flow_rows.loc[
            internal & flow_rows["external_flow_mxn"].lt(0), "external_flow_mxn"
        ]

        flows = (
            flow_rows.groupby(["year_month", "contract_id"], as_index=False)
            .agg(
                raw_contributions_mxn=("raw_contribution_mxn", "sum"),
                raw_withdrawals_mxn=("raw_withdrawal_mxn", "sum"),
                contributions_mxn=("external_contribution_mxn", "sum"),
                withdrawals_mxn=("external_withdrawal_mxn", "sum"),
                internal_transfer_in_mxn=("internal_transfer_in_mxn", "sum"),
                internal_transfer_out_mxn=("internal_transfer_out_mxn", "sum"),
                net_external_flow_mxn=("external_flow_consolidated_mxn", "sum"),
            )
        )
        history = history.merge(flows, on=["year_month", "contract_id"], how="left")

    flow_columns = [
        "raw_contributions_mxn",
        "raw_withdrawals_mxn",
        "contributions_mxn",
        "withdrawals_mxn",
        "internal_transfer_in_mxn",
        "internal_transfer_out_mxn",
        "net_external_flow_mxn",
    ]
    for column in flow_columns:
        if column not in history.columns:
            history[column] = 0.0
        history[column] = pd.to_numeric(history[column], errors="coerce").fillna(0.0)

    history["statement_date"] = pd.to_datetime(history["statement_date"])
    history = history.sort_values(["contract_id", "statement_date"])
    history["cumulative_contributions_mxn"] = history.groupby("contract_id")["contributions_mxn"].cumsum()
    history["cumulative_withdrawals_mxn"] = history.groupby("contract_id")["withdrawals_mxn"].cumsum()
    history["cumulative_net_contributed_mxn"] = (
        history["cumulative_contributions_mxn"] - history["cumulative_withdrawals_mxn"]
    )
    history["statement_date"] = history["statement_date"].dt.date.astype(str)
    return history


def _modified_dietz_for_month(
    row: pd.Series,
    month_flows: pd.DataFrame,
) -> float | None:
    begin = float(row["portfolio_value_previous_mxn"])
    end = float(row["portfolio_value_current_mxn"])

    if month_flows.empty:
        denominator = begin
        numerator = end - begin
    else:
        month_start = pd.Timestamp(row["year_month"] + "-01")
        last_day = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = pd.Timestamp(month_start.year, month_start.month, last_day)
        total_days = max((month_end - month_start).days, 1)

        weighted_flows = 0.0
        total_flows = 0.0
        for flow in month_flows.itertuples(index=False):
            amount = float(flow.external_flow_consolidated_mxn)
            flow_date = pd.Timestamp(flow.operation_date)
            weight = max(min((month_end - flow_date).days / total_days, 1.0), 0.0)
            weighted_flows += weight * amount
            total_flows += amount

        denominator = begin + weighted_flows
        numerator = end - begin - total_flows

    if abs(denominator) < 1e-9:
        return None
    return numerator / denominator


def _build_consolidated_history(
    summary: pd.DataFrame,
    movements: pd.DataFrame,
    dim_contract: pd.DataFrame,
) -> pd.DataFrame:
    included_contracts = set(
        dim_contract.loc[dim_contract["include_in_portfolio"], "contract_id"].astype(str)
    )
    included_summary = summary[summary["contract_id"].astype(str).isin(included_contracts)].copy()

    numeric_to_sum = [
        "portfolio_value_previous_mxn",
        "portfolio_value_current_mxn",
        "debt_previous_mxn",
        "debt_current_mxn",
        "variable_income_previous_mxn",
        "variable_income_current_mxn",
        "cash_previous_mxn",
        "cash_current_mxn",
    ]
    for col in numeric_to_sum:
        included_summary[col] = pd.to_numeric(included_summary[col], errors="coerce").fillna(0.0)

    consolidated = (
        included_summary.groupby("year_month", as_index=False)
        .agg(
            statement_date=("statement_date", "max"),
            contract_count=("contract_id", "nunique"),
            contracts_present=(
                "contract_id",
                lambda s: ";".join(sorted({str(x) for x in s})),
            ),
            **{col: (col, "sum") for col in numeric_to_sum},
        )
        .sort_values("year_month")
    )

    flow_rows = movements[
        movements["category"].isin(["contribution", "withdrawal"])
        & movements["contract_id"].astype(str).isin(included_contracts)
    ].copy()
    if not flow_rows.empty:
        flow_rows["external_contribution_mxn"] = flow_rows["external_flow_consolidated_mxn"].clip(lower=0)
        flow_rows["external_withdrawal_mxn"] = -flow_rows["external_flow_consolidated_mxn"].clip(upper=0)
        flow_rows["internal_transfer_in_mxn"] = 0.0
        flow_rows["internal_transfer_out_mxn"] = 0.0
        internal = flow_rows["flow_scope"].eq("internal_transfer")
        flow_rows.loc[internal & flow_rows["external_flow_mxn"].gt(0), "internal_transfer_in_mxn"] = flow_rows.loc[
            internal & flow_rows["external_flow_mxn"].gt(0), "external_flow_mxn"
        ]
        flow_rows.loc[internal & flow_rows["external_flow_mxn"].lt(0), "internal_transfer_out_mxn"] = -flow_rows.loc[
            internal & flow_rows["external_flow_mxn"].lt(0), "external_flow_mxn"
        ]

        monthly_flows = (
            flow_rows.groupby("year_month", as_index=False)
            .agg(
                contributions_mxn=("external_contribution_mxn", "sum"),
                withdrawals_mxn=("external_withdrawal_mxn", "sum"),
                net_external_flow_mxn=("external_flow_consolidated_mxn", "sum"),
                internal_transfer_in_mxn=("internal_transfer_in_mxn", "sum"),
                internal_transfer_out_mxn=("internal_transfer_out_mxn", "sum"),
            )
        )
        consolidated = consolidated.merge(monthly_flows, on="year_month", how="left")

    for col in [
        "contributions_mxn",
        "withdrawals_mxn",
        "net_external_flow_mxn",
        "internal_transfer_in_mxn",
        "internal_transfer_out_mxn",
    ]:
        if col not in consolidated.columns:
            consolidated[col] = 0.0
        consolidated[col] = pd.to_numeric(consolidated[col], errors="coerce").fillna(0.0)

    returns: list[float | None] = []
    for _, row in consolidated.iterrows():
        month_flows = flow_rows[
            flow_rows["year_month"].eq(row["year_month"])
            & flow_rows["flow_scope"].ne("internal_transfer")
        ] if not flow_rows.empty else pd.DataFrame()
        returns.append(_modified_dietz_for_month(row, month_flows))

    consolidated["modified_dietz_return"] = returns
    consolidated["modified_dietz_return_pct"] = consolidated["modified_dietz_return"] * 100.0

    cumulative_factor = 1.0
    cumulative_values: list[float | None] = []
    for value in consolidated["modified_dietz_return"]:
        if pd.isna(value):
            cumulative_values.append(None)
            continue
        cumulative_factor *= 1.0 + float(value)
        cumulative_values.append((cumulative_factor - 1.0) * 100.0)
    consolidated["cumulative_twr_modified_dietz_pct"] = cumulative_values

    consolidated["cumulative_contributions_mxn"] = consolidated["contributions_mxn"].cumsum()
    consolidated["cumulative_withdrawals_mxn"] = consolidated["withdrawals_mxn"].cumsum()
    consolidated["cumulative_net_contributed_mxn"] = (
        consolidated["cumulative_contributions_mxn"]
        - consolidated["cumulative_withdrawals_mxn"]
    )
    return consolidated


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    processed_dir = resolve_project_path(
        args.processed or config.get("path_gbm_processed", "gbm/processed")
    )

    summary = _read_csv(processed_dir / "statement_summary.csv")
    returns = _read_csv(processed_dir / "monthly_returns_raw.csv")
    movements = _read_csv(processed_dir / "movements.csv")

    if summary.empty:
        raise FileNotFoundError(
            f"No parsed statement_summary.csv found in {processed_dir}. "
            "Run extract_gbm_statements.py first."
        )

    _ensure_unique_contract_month(summary)
    dim_contract = _preserve_dim_contract(summary, processed_dir)

    classified_movements, transfer_matches, transfer_ambiguous = _classify_internal_transfers(
        movements, config
    )
    write_table(classified_movements, processed_dir / "movements_classified")
    if not transfer_matches.empty:
        write_table(transfer_matches, processed_dir / "internal_transfer_matches")
    if not transfer_ambiguous.empty:
        write_table(transfer_ambiguous, processed_dir / "internal_transfer_ambiguous")

    contract_history = _build_contract_history(summary, returns, classified_movements)
    write_table(contract_history, processed_dir / "gbm_portfolio_monthly")

    consolidated = _build_consolidated_history(summary, classified_movements, dim_contract)
    write_table(consolidated, processed_dir / "portfolio_consolidated_monthly")

    print(f"Created: {processed_dir / 'gbm_portfolio_monthly.csv'}")
    print(f"Created: {processed_dir / 'portfolio_consolidated_monthly.csv'}")
    print(f"Created: {processed_dir / 'dim_contract.csv'}")
    if not transfer_matches.empty:
        print(f"Matched {len(transfer_matches)} internal transfer pair(s).")
    if not transfer_ambiguous.empty:
        print(
            f"WARNING: {len(transfer_ambiguous)} ambiguous internal-transfer candidate(s). "
            "They remain external until reviewed or added to the override file."
        )


if __name__ == "__main__":
    main()
