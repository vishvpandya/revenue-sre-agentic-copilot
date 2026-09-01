"""Controlled live payment-event simulation for the judge demonstration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from recovery_orchestrator.db.connection import Database


def live_payment_summary(database: Database, merchant_id: str) -> dict[str, Any]:
    """Return only events injected during the running demo's last hour."""

    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid
            FROM sre_payment_events
            WHERE merchant_id = ? AND occurred_at >= ?
            """,
            (merchant_id, since),
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_id, occurred_at, payment_method, status, error_family
            FROM sre_payment_events
            WHERE merchant_id = ? AND occurred_at >= ?
            ORDER BY occurred_at DESC LIMIT 8
            """,
            (merchant_id, since),
        ).fetchall()
    attempts, paid = int(row["attempts"]), int(row["paid"] or 0)
    success_rate = paid / attempts if attempts else None
    if attempts >= 5 and success_rate is not None and success_rate < 0.70:
        message = (
            "Live Monitor has flagged a sharp decline in the events you just added. "
            "Open Payment health to investigate the wider pattern."
        )
    elif attempts:
        message = "Live Monitor is receiving new payment outcomes from this merchant in real time."
    else:
        message = "No live demo events have been added in the last hour."
    return {
        "window_minutes": 60,
        "attempts": attempts,
        "paid": paid,
        "success_rate": success_rate,
        "message": message,
        "events": [dict(item) for item in events],
    }


def record_live_payment(
    database: Database, merchant_id: str, payment_method: str, status: str
) -> dict[str, Any]:
    """Append one clearly-labelled synthetic event; it never contacts a real payment system."""

    now = datetime.now(UTC)
    with database.transaction(immediate=True) as connection:
        profile = connection.execute(
            "SELECT sdk_version FROM merchant_profiles WHERE merchant_id = ?", (merchant_id,)
        ).fetchone()
        if profile is None:
            raise ValueError("Merchant profile was not found")
        provider = "upi_collect" if payment_method == "upi" else "card_gateway"
        error_family = "live_demo_payment_failure" if status == "failed" else None
        event = {
            "event_id": str(uuid4()),
            "merchant_id": merchant_id,
            "occurred_at": now.isoformat(),
            "payment_method": payment_method,
            "provider": provider,
            "issuer": "demo_issuer",
            "device": "web",
            "sdk_version": str(profile["sdk_version"]),
            "amount_paise": 199_900,
            "status": status,
            "error_family": error_family,
            "authorization_latency_ms": 1_250 if status == "failed" else 410,
        }
        connection.execute(
            """
            INSERT INTO sre_payment_events(
                event_id, merchant_id, occurred_at, payment_method, provider, issuer, device,
                sdk_version, amount_paise, status, error_family, authorization_latency_ms
            ) VALUES (:event_id, :merchant_id, :occurred_at, :payment_method, :provider, :issuer,
                      :device, :sdk_version, :amount_paise, :status, :error_family,
                      :authorization_latency_ms)
            """,
            event,
        )
    return {"event": event, "live_summary": live_payment_summary(database, merchant_id)}
