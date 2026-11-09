from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from santander_utils import load_config, resolve_project_path, write_table


TOLERANCE_MXN = 1.00


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Santander statement extraction and reconciliations."
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

    rows = []

    for statement in summary.itertuples(index=False):
        ym = statement.year_month
        account_month = accounts[accounts["year_month"] == ym].copy()
        movement_month = movements[movements["statement_month"] == ym].copy()

        previous_sum = pd.to_numeric(
            account_month["opening_balance_mxn"], errors="coerce"
        ).fillna(0).sum()
        current_sum = pd.to_numeric(
            account_month["closing_balance_mxn"], errors="coerce"
        ).fillna(0).sum()

        previous_diff = round(previous_sum - float(statement.total_previous_mxn), 2)
        current_diff = round(current_sum - float(statement.total_current_mxn), 2)

        account_recon_issues = []
        for account in account_month.itertuples(index=False):
            lhs = (
                float(account.opening_balance_mxn)
                + float(account.deposits_reported_mxn or 0)
                - float(account.withdrawals_reported_mxn or 0)
            )
            diff = round(lhs - float(account.closing_balance_mxn), 2)
            if abs(diff) > TOLERANCE_MXN:
                account_recon_issues.append(f"{account.product_type}:{diff:.2f}")

        movement_issues = []
        for account in account_month.itertuples(index=False):
            tx = movement_month[movement_month["account_id"] == account.account_id]
            if tx.empty and abs(float(account.closing_balance_mxn)) <= TOLERANCE_MXN:
                continue
            if tx.empty:
                movement_issues.append(f"{account.product_type}:no_movements")
                continue

            deposits = pd.to_numeric(tx["deposit_mxn"], errors="coerce").fillna(0).sum()
            withdrawals = pd.to_numeric(tx["withdrawal_mxn"], errors="coerce").fillna(0).sum()

            dep_diff = round(deposits - float(account.deposits_reported_mxn or 0), 2)
            wd_diff = round(withdrawals - float(account.withdrawals_reported_mxn or 0), 2)

            if abs(dep_diff) > TOLERANCE_MXN:
                movement_issues.append(f"{account.product_type}:deposit_diff={dep_diff:.2f}")
            if abs(wd_diff) > TOLERANCE_MXN:
                movement_issues.append(f"{account.product_type}:withdrawal_diff={wd_diff:.2f}")

        interest_tx = movement_month[
            movement_month["category"].eq("interest")
        ]
        tax_tx = movement_month[
            movement_month["category"].eq("tax_withholding")
        ]

        interest_gross_parsed = pd.to_numeric(
            interest_tx["deposit_mxn"], errors="coerce"
        ).fillna(0).sum()

        isr_parsed = pd.to_numeric(
            tax_tx["withdrawal_mxn"], errors="coerce"
        ).fillna(0).sum()

        interest_net_parsed = interest_gross_parsed - isr_parsed

        interest_gross_reported = getattr(
            statement, "interest_gross_reported_mxn", None
        )
        isr_reported = getattr(statement, "isr_reported_mxn", None)
        interest_net_reported = getattr(
            statement, "interest_net_reported_mxn", None
        )

        interest_gross_diff = (
            None
            if pd.isna(interest_gross_reported)
            else round(
                interest_gross_parsed - float(interest_gross_reported),
                2,
            )
        )

        isr_diff = (
            None
            if pd.isna(isr_reported)
            else round(isr_parsed - float(isr_reported), 2)
        )

        interest_net_diff = (
            None
            if pd.isna(interest_net_reported)
            else round(
                interest_net_parsed - float(interest_net_reported),
                2,
            )
        )

        reconstructed_rows = int(
            movement_month.get("balance_source", pd.Series(dtype=str))
            .astype(str)
            .eq("reconstructed")
            .sum()
        )

        issues = []
        if abs(previous_diff) > TOLERANCE_MXN:
            issues.append("previous_total_mismatch")
        if abs(current_diff) > TOLERANCE_MXN:
            issues.append("current_total_mismatch")
        if account_recon_issues:
            issues.append("account_summary_mismatch")
        if movement_issues:
            issues.append("movement_totals_mismatch")
        if (
            interest_gross_diff is not None
            and abs(interest_gross_diff) > TOLERANCE_MXN
        ):
            issues.append("interest_gross_mismatch")

        if isr_diff is not None and abs(isr_diff) > TOLERANCE_MXN:
            issues.append("isr_mismatch")

        if (
            interest_net_diff is not None
            and abs(interest_net_diff) > TOLERANCE_MXN
        ):
            issues.append("interest_net_mismatch")

        rows.append(
            {
                "year_month": ym,
                "source_file": statement.source_file,
                "reported_previous_total_mxn": statement.total_previous_mxn,
                "parsed_previous_total_mxn": previous_sum,
                "previous_diff_mxn": previous_diff,
                "reported_current_total_mxn": statement.total_current_mxn,
                "parsed_current_total_mxn": current_sum,
                "current_diff_mxn": current_diff,
                "interest_gross_reported_mxn": interest_gross_reported,
                "interest_gross_parsed_mxn": round(interest_gross_parsed, 2),
                "interest_gross_diff_mxn": interest_gross_diff,
                "isr_reported_mxn": isr_reported,
                "isr_parsed_mxn": round(isr_parsed, 2),
                "isr_diff_mxn": isr_diff,
                "interest_net_reported_mxn": interest_net_reported,
                "interest_net_parsed_mxn": round(interest_net_parsed, 2),
                "interest_net_diff_mxn": interest_net_diff,
                "reconstructed_balance_rows": reconstructed_rows,
                "account_reconciliation_details": ";".join(account_recon_issues),
                "movement_reconciliation_details": ";".join(movement_issues),
                "validation_status": "OK" if not issues else ";".join(issues),
            }
        )

    report = pd.DataFrame(rows)
    write_table(report, processed_dir / "validation_report")

    problems = report[report["validation_status"] != "OK"]
    print(f"Validated {len(report)} Santander statement month(s).")

    if problems.empty:
        print("All parsed Santander statements passed validation.")
    else:
        print(f"WARNING: {len(problems)} statement month(s) need review.")
        print(
            problems[
                [
                    "year_month",
                    "source_file",
                    "validation_status",
                    "previous_diff_mxn",
                    "current_diff_mxn",
                    "movement_reconciliation_details",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
