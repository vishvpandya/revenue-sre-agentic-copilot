"""Print the demo logins and the currently detected issues for the latest run.

This is read-only.  Run it after creating synthetic data to see which merchant
accounts are useful for a particular demo path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from recovery_orchestrator.revenue_sre.catalog import DEMO_MERCHANTS


DATABASE_PATH = Path("data/recovery_orchestrator.sqlite3")


def main() -> None:
    if not DATABASE_PATH.exists():
        raise SystemExit(
            "No demo database yet. Start the backend once, then create synthetic data."
        )

    passwords = {merchant.merchant_id: merchant.password for merchant in DEMO_MERCHANTS}
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                m.merchant_id,
                m.name,
                u.login_id,
                f.payment_method,
                f.error_family,
                f.severity,
                f.baseline_success_rate,
                f.observed_success_rate
            FROM merchants AS m
            JOIN merchant_users AS u
              ON u.merchant_id = m.merchant_id AND u.role = 'merchant_owner'
            LEFT JOIN sre_anomaly_findings AS f
              ON f.merchant_id = m.merchant_id AND f.status = 'open'
            ORDER BY m.merchant_id, f.severity DESC, f.error_family
            """
        ).fetchall()
    finally:
        connection.close()

    print("\nCURRENT SYNTHETIC RUN - 20 DEMO MERCHANTS\n")
    current_merchant = None
    for row in rows:
        if row["merchant_id"] != current_merchant:
            current_merchant = row["merchant_id"]
            print(f"{row['name']} ({row['merchant_id']})")
            print(f"  Login:    {row['login_id']}")
            print(f"  Password: {passwords[row['merchant_id']]}")
        if row["error_family"] is None:
            print("  Issue:    No open payment issue")
        else:
            baseline = round(row["baseline_success_rate"] * 100)
            observed = round(row["observed_success_rate"] * 100)
            print(
                f"  Issue:    {row['payment_method'].upper()} | "
                f"{row['error_family']} | {row['severity']} | "
                f"{baseline}% -> {observed}%"
            )
        print()


if __name__ == "__main__":
    main()
