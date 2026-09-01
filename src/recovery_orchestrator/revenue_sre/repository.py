"""Explicit tenant-scoped persistence for the Revenue SRE foundation."""

# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.revenue_sre.auth import (
    hash_password,
    hash_session_token,
    verify_password,
)
from recovery_orchestrator.revenue_sre.catalog import DEMO_MERCHANTS, DemoMerchant


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    merchant_id: str | None
    merchant_name: str | None
    role: str
    login_id: str


class RevenueSRERepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def seed_tenants(self) -> None:
        """Create stable demo tenants without resetting existing demo evidence."""

        created_at = _now().isoformat()
        with self.database.transaction(immediate=True) as connection:
            for merchant in DEMO_MERCHANTS:
                connection.execute(
                    """
                    INSERT INTO merchants(merchant_id, name, sector, timezone, baseline_success_rate, created_at)
                    VALUES (?, ?, ?, 'Asia/Kolkata', ?, ?)
                    ON CONFLICT(merchant_id) DO UPDATE SET
                        name = excluded.name, sector = excluded.sector,
                        baseline_success_rate = excluded.baseline_success_rate
                    """,
                    (
                        merchant.merchant_id,
                        merchant.name,
                        merchant.sector,
                        merchant.baseline_success_rate,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO merchant_profiles(
                        merchant_id, enabled_methods_json, platforms_json, sdk_version,
                        average_order_value_paise, monthly_attempts
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(merchant_id) DO UPDATE SET
                        enabled_methods_json = excluded.enabled_methods_json,
                        platforms_json = excluded.platforms_json, sdk_version = excluded.sdk_version
                    """,
                    (
                        merchant.merchant_id,
                        json.dumps(merchant.methods),
                        json.dumps(merchant.platforms),
                        merchant.sdk_version,
                        2_500_00 + int((1 - merchant.baseline_success_rate) * 1_000_000),
                        8_000 + int(merchant.baseline_success_rate * 10_000),
                    ),
                )
                self._upsert_user(connection, merchant, created_at)
            connection.execute(
                """
                INSERT INTO merchant_users(user_id, merchant_id, login_id, password_hash, role, active, created_at)
                VALUES ('USR-OPS-001', NULL, ?, ?, 'razorpay_ops', 1, ?)
                ON CONFLICT(login_id) DO UPDATE SET role = excluded.role, active = 1
                """,
                ("ops@demo.revenuesre.local", hash_password("RazorpayOps#Demo"), created_at),
            )

    @staticmethod
    def _upsert_user(connection, merchant: DemoMerchant, created_at: str) -> None:
        connection.execute(
            """
            INSERT INTO merchant_users(user_id, merchant_id, login_id, password_hash, role, active, created_at)
            VALUES (?, ?, ?, ?, 'merchant_owner', 1, ?)
            ON CONFLICT(login_id) DO UPDATE SET merchant_id = excluded.merchant_id, active = 1
            """,
            (
                f"USR-{merchant.merchant_id.removeprefix('MRC-')}",
                merchant.merchant_id,
                merchant.login_id,
                hash_password(merchant.password),
                created_at,
            ),
        )

    def authenticate(self, login_id: str, password: str, token: str) -> AuthenticatedUser | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT u.user_id, u.merchant_id, u.login_id, u.password_hash, u.role, u.active, m.name
                FROM merchant_users u LEFT JOIN merchants m ON m.merchant_id = u.merchant_id
                WHERE u.login_id = ?
                """,
                (login_id.lower().strip(),),
            ).fetchone()
        if row is None or not row["active"] or not verify_password(password, row["password_hash"]):
            return None
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions(session_id, user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    row["user_id"],
                    hash_session_token(token),
                    (now + timedelta(hours=8)).isoformat(),
                    now.isoformat(),
                ),
            )
        return AuthenticatedUser(
            user_id=row["user_id"],
            merchant_id=row["merchant_id"],
            merchant_name=row["name"],
            role=row["role"],
            login_id=row["login_id"],
        )

    def user_from_token(self, token: str) -> AuthenticatedUser | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT u.user_id, u.merchant_id, u.login_id, u.role, m.name
                FROM auth_sessions s
                JOIN merchant_users u ON u.user_id = s.user_id
                LEFT JOIN merchants m ON m.merchant_id = u.merchant_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.active = 1
                """,
                (hash_session_token(token), _now().isoformat()),
            ).fetchone()
        if row is None:
            return None
        return AuthenticatedUser(
            user_id=row["user_id"],
            merchant_id=row["merchant_id"],
            merchant_name=row["name"],
            role=row["role"],
            login_id=row["login_id"],
        )

    def revoke_token(self, token: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_now().isoformat(), hash_session_token(token)),
            )

    def merchant_overview(self, merchant_id: str) -> dict[str, object]:
        with self.database.connect() as connection:
            merchant = connection.execute(
                "SELECT merchant_id, name, sector, baseline_success_rate FROM merchants WHERE merchant_id = ?",
                (merchant_id,),
            ).fetchone()
            incidents = connection.execute(
                """
                SELECT incident_id, title, scope, status, severity, started_at, summary, visible_evidence_json
                FROM sre_incidents WHERE merchant_id = ? ORDER BY started_at DESC
                """,
                (merchant_id,),
            ).fetchall()
        if merchant is None:
            raise KeyError("Merchant not found")
        return {
            "merchant": dict(merchant),
            "incidents": [
                {**dict(row), "visible_evidence": json.loads(row["visible_evidence_json"])}
                for row in incidents
            ],
        }

    def operations_summary(self) -> dict[str, object]:
        with self.database.connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS count FROM merchants").fetchone()[
                "count"
            ]
            rows = connection.execute(
                """
                SELECT s.time_bucket, s.payment_method, s.provider, s.issuer, s.device, s.error_family,
                       COUNT(DISTINCT i.merchant_id) AS affected_merchants
                FROM sre_network_signatures s
                JOIN sre_incidents i ON i.incident_id = s.incident_id
                GROUP BY s.time_bucket, s.payment_method, s.provider, s.issuer, s.device, s.error_family
                HAVING COUNT(DISTINCT i.merchant_id) >= 3
                ORDER BY affected_merchants DESC, s.time_bucket
                """
            ).fetchall()
        return {"merchant_count": total, "anonymized_clusters": [dict(row) for row in rows]}
