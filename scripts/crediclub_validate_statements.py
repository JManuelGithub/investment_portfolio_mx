
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from crediclub_utils import (
    filename_period,
    load_config,
    project_path,
    safe_round,
    write_table,
)


TOLERANCE_MXN = 1.00


def _num(value) -> float:
    if pd.isna(value):
        return 0.0
    return float(value)


def main() -> None:
    config = load_config()
    processed_dir = project_path(
        config,
        "path_crediclub_processed",
        "crediclub/processed",
    )

    summary_path = processed_dir / "statement_summary.csv"
    snapshots_path = processed_dir / "account_snapshots.csv"
    positions_path = processed_dir / "fixed_term_positions.csv"
    movements_path = processed_dir / "movements.csv"

    for path in (summary_path, snapshots_path, positions_path, movements_path):
        if not path.exists():
            raise SystemExit(
                f"Missing {path}. Run crediclub_extract_statements.py first."
            )

    summary = pd.read_csv(summary_path)
    snapshots = pd.read_csv(snapshots_path)
    positions = pd.read_csv(positions_path)
    movements = pd.read_csv(movements_path)

    report = []

    for statement in summary.itertuples(index=False):
        month = statement.year_month
        issues = []
        warnings = []
        details = []

        month_snapshots = snapshots[
            snapshots["year_month"].eq(month)
            & snapshots["source_file"].eq(statement.source_file)
        ]
        month_positions = positions[
            positions["year_month"].eq(month)
            & positions["source_file"].eq(statement.source_file)
        ]
        month_movements = movements[
            movements["year_month"].eq(month)
            & movements["source_file"].eq(statement.source_file)
        ]

        # Filename period check when filename follows YYYY_MM_...
        fp = filename_period(Path(statement.source_file))
        if fp:
            expected = f"{fp[0]:04d}-{fp[1]:02d}"
            if expected != month:
                issues.append("filename_period_mismatch")

        # Cash account arithmetic.
        for account in month_snapshots.itertuples(index=False):
            if account.product_type not in {"DEBIT", "SIGHT"}:
                continue

            required = [
                account.opening_balance_mxn,
                account.deposits_mxn,
                account.withdrawals_mxn,
                account.closing_balance_mxn,
            ]
            if any(pd.isna(v) for v in required):
                continue

            commissions = _num(
                getattr(account, "commissions_mxn", 0.0)
            )
            iva = _num(
                getattr(account, "iva_commissions_mxn", 0.0)
            )

            expected_close = (
                float(account.opening_balance_mxn)
                + float(account.deposits_mxn)
                - float(account.withdrawals_mxn)
                - commissions
                - iva
            )
            diff = round(
                expected_close - float(account.closing_balance_mxn),
                2,
            )

            if abs(diff) > TOLERANCE_MXN:
                issues.append(
                    f"{account.product_type.lower()}_balance_reconciliation"
                )

            tx = month_movements[
                month_movements["product_type"].eq(account.product_type)
            ]

            if not tx.empty:
                deposits_parsed = pd.to_numeric(
                    tx["deposit_mxn"],
                    errors="coerce",
                ).fillna(0).sum()
                withdrawals_parsed = pd.to_numeric(
                    tx["withdrawal_mxn"],
                    errors="coerce",
                ).fillna(0).sum()

                dep_diff = round(
                    deposits_parsed - float(account.deposits_mxn),
                    2,
                )
                wdr_diff = round(
                    withdrawals_parsed - float(account.withdrawals_mxn),
                    2,
                )

                # For modern SIGHT, summary deposits/withdrawals are derived
                # from movements, but the check is still useful if data is
                # modified downstream.
                if abs(dep_diff) > TOLERANCE_MXN:
                    issues.append(
                        f"{account.product_type.lower()}_deposit_mismatch"
                    )
                if abs(wdr_diff) > TOLERANCE_MXN:
                    issues.append(
                        f"{account.product_type.lower()}_withdrawal_mismatch"
                    )

            gross = getattr(account, "interest_gross_mxn", None)
            isr = getattr(account, "isr_mxn", None)
            net = getattr(account, "interest_net_mxn", None)

            if not any(pd.isna(v) for v in (gross, isr, net)):
                interest_diff = round(
                    float(gross) - float(isr) - float(net),
                    2,
                )
                if abs(interest_diff) > TOLERANCE_MXN:
                    issues.append(
                        f"{account.product_type.lower()}_interest_reconciliation"
                    )

            details.append(
                {
                    "product_type": account.product_type,
                    "balance_diff_mxn": diff,
                }
            )

        # Fixed-term arithmetic.
        fixed_position_issues = []
        for pos in month_positions.itertuples(index=False):
            principal = _num(pos.principal_mxn)
            gross = _num(pos.gross_interest_at_maturity_mxn)
            isr = _num(pos.isr_at_maturity_mxn)
            net = _num(pos.net_interest_at_maturity_mxn)
            capital = _num(pos.capital_at_maturity_mxn)

            net_diff = round(gross - isr - net, 2)
            capital_diff = round(principal + net - capital, 2)

            if abs(net_diff) > TOLERANCE_MXN:
                issues.append("fixed_term_net_interest_mismatch")
            if abs(capital_diff) > TOLERANCE_MXN:
                issues.append("fixed_term_capital_maturity_mismatch")

            if pos.status_at_cutoff == "MATURED_BY_CUTOFF":
                warnings.append("expired_fixed_term_listed_in_statement")

            fixed_position_issues.append(
                {
                    "position_id": pos.position_id,
                    "net_interest_diff_mxn": net_diff,
                    "capital_maturity_diff_mxn": capital_diff,
                    "status_at_cutoff": pos.status_at_cutoff,
                }
            )

        # The fixed-term card should match principal of rows listed by the
        # statement, even when the statement still lists an already matured row.
        reported_fixed = getattr(
            statement,
            "reported_fixed_term_card_mxn",
            None,
        )
        if not pd.isna(reported_fixed):
            listed_principal = pd.to_numeric(
                month_positions["principal_mxn"],
                errors="coerce",
            ).fillna(0).sum()
            fixed_card_diff = round(
                listed_principal - float(reported_fixed),
                2,
            )
            if abs(fixed_card_diff) > TOLERANCE_MXN:
                issues.append("fixed_term_card_mismatch")
        else:
            fixed_card_diff = None

        # Internal pairs must net to zero.
        paired = month_movements[
            month_movements["flow_scope"].eq("internal_crediclub")
            & month_movements["internal_pair_id"].notna()
            & ~month_movements["internal_pair_id"].eq("FIXED_TERM_MATURITY")
        ]

        if not paired.empty:
            for pair_id, group in paired.groupby("internal_pair_id"):
                net_pair = pd.to_numeric(
                    group["signed_amount_mxn"],
                    errors="coerce",
                ).fillna(0).sum()
                if abs(net_pair) > TOLERANCE_MXN:
                    issues.append("internal_transfer_pair_mismatch")

        issues = sorted(set(issues))
        warnings = sorted(set(warnings))

        status = "ERROR" if issues else (
            "OK_WITH_WARNINGS" if warnings else "OK"
        )

        report.append(
            {
                "source_file": statement.source_file,
                "year_month": month,
                "layout_version": statement.layout_version,
                "status": status,
                "issues": "|".join(issues),
                "warnings": "|".join(warnings),
                "account_reconciliation_details": json.dumps(
                    details,
                    ensure_ascii=False,
                ),
                "fixed_term_reconciliation_details": json.dumps(
                    fixed_position_issues,
                    ensure_ascii=False,
                ),
                "fixed_term_card_diff_mxn": fixed_card_diff,
                "derived_closing_portfolio_mxn": getattr(
                    statement,
                    "derived_closing_portfolio_mxn",
                ),
            }
        )

    report_df = pd.DataFrame(report)
    write_table(report_df, processed_dir / "validation_report")

    errors = report_df[report_df["status"].eq("ERROR")]
    if not errors.empty:
        print(errors.to_string(index=False))
        raise SystemExit(
            f"{len(errors)} Crediclub statement(s) failed validation."
        )

    warning_count = report_df["status"].eq("OK_WITH_WARNINGS").sum()
    if warning_count:
        print(
            f"All Crediclub statements passed hard validation; "
            f"{warning_count} statement(s) have informational warnings."
        )
    else:
        print("All parsed Crediclub statements passed validation.")


if __name__ == "__main__":
    main()
