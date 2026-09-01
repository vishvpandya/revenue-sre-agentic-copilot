"""Approval-gated synthetic customer recovery for a UPI provider outage."""

# ruff: noqa: E501

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.revenue_sre.live_whatsapp import (
    customer_recovery_delivery_status,
    send_customer_recovery_whatsapp,
)
from recovery_orchestrator.settings import Settings

MAX_TEST_RECIPIENTS = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _batch_id(finding_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre:customer-recovery:{finding_id}"))


def _addresses(settings: Settings) -> list[str]:
    configured = settings.twilio_customer_test_recipients or ""
    items = [value.strip() for value in configured.split(",") if value.strip()]
    return items[:MAX_TEST_RECIPIENTS]


def _public_link(settings: Settings, token: str) -> str:
    base = (settings.twilio_webhook_base_url or "").rstrip("/")
    if not base:
        raise ValueError(
            "Start ngrok and set TWILIO_WEBHOOK_BASE_URL before creating customer test links."
        )
    return f"{base}/customer-recovery/test-payment/{token}"


def _gemini_message(settings: Settings, *, prompt: str) -> str:
    if settings.gemini_api_key is None:
        raise RuntimeError("Set GEMINI_API_KEY before generating customer-recovery messages.")
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    response = client.models.generate_content(model=settings.gemini_model, contents=prompt)
    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise RuntimeError("Gemini returned an empty customer-recovery message.")
    return text.strip()


def _initial_message(settings: Settings, link: str) -> str:
    return _gemini_message(
        settings,
        prompt=(
            "Write a concise WhatsApp message (under 70 words) for a customer. A UPI payment "
            "attempt could not complete because of a temporary provider issue. Offer this safe, "
            f"synthetic test-payment link: {link}. Explain that the customer can use an alternate "
            "payment method. Do not claim the payment succeeded and do not mention internal agents."
        ),
    )


def _follow_up_message(settings: Settings, link: str, *, opened: bool) -> str:
    context = (
        "The customer opened the test link but has not marked payment complete."
        if opened
        else "The customer has not opened or completed the test link yet."
    )
    return _gemini_message(
        settings,
        prompt=(
            "Write a concise WhatsApp follow-up (under 55 words). "
            f"{context} Ask whether "
            f"they still need help and include the same test link: {link}. Do not pressure them or "
            "claim a payment failed."
        ),
    )


def prepare_batch(
    database: Database, settings: Settings, merchant_id: str, finding_id: str
) -> dict[str, object]:
    """Prepare Gemini drafts for configured test customers; do not send anything."""

    recipients = _addresses(settings)
    if not recipients:
        raise ValueError(
            "Set TWILIO_CUSTOMER_TEST_RECIPIENTS with up to three Sandbox-joined test numbers."
        )
    with database.connect() as connection:
        finding = connection.execute(
            "SELECT * FROM sre_anomaly_findings WHERE finding_id = ? AND merchant_id = ?",
            (finding_id, merchant_id),
        ).fetchone()
        investigation = connection.execute(
            "SELECT * FROM sre_investigations WHERE finding_id = ? ORDER BY created_at DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
    if finding is None or investigation is None:
        raise ValueError("Run the specialist investigation before preparing customer recovery.")
    if finding["payment_method"] != "upi" or investigation["scope"] != "network":
        raise ValueError(
            "Customer recovery links are available only for a confirmed network-level UPI issue."
        )

    batch_id = _batch_id(finding_id)
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM customer_recovery_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO customer_recovery_batches(batch_id, merchant_id, finding_id, investigation_id, status, created_at)
                VALUES (?, ?, ?, ?, 'draft', ?)
                """,
                (batch_id, merchant_id, finding_id, investigation["investigation_id"], _now()),
            )
            for index, address in enumerate(recipients, start=1):
                token = secrets.token_urlsafe(24)
                connection.execute(
                    """
                    INSERT INTO customer_recovery_recipients(
                        recipient_id, batch_id, customer_label, whatsapp_address, link_token, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'draft', ?)
                    """,
                    (
                        str(uuid5(NAMESPACE_URL, f"{batch_id}:{address}")),
                        batch_id,
                        f"Test customer {index}",
                        address,
                        token,
                        _now(),
                    ),
                )
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM customer_recovery_recipients WHERE batch_id = ? ORDER BY customer_label",
            (batch_id,),
        ).fetchall()
    for row in rows:
        if row["message_body"]:
            continue
        link = _public_link(settings, row["link_token"])
        message = _initial_message(settings, link)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE customer_recovery_recipients SET message_body = ? WHERE recipient_id = ?",
                (message, row["recipient_id"]),
            )
    return batch_for_merchant(database, merchant_id, batch_id)


def approve_and_send_batch(
    database: Database, settings: Settings, merchant_id: str, batch_id: str
) -> dict[str, object]:
    """The one merchant approval that permits this small, configured test batch to send."""

    batch = batch_for_merchant(database, merchant_id, batch_id)
    if batch["status"] in {"sent", "completed"}:
        return {**batch, "replayed": True}
    if any(not item["message_body"] for item in batch["recipients"]):
        raise ValueError("Gemini drafts are not ready yet.")
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE customer_recovery_batches SET status = 'approved', approved_at = ? WHERE batch_id = ?",
            (_now(), batch_id),
        )
    failures: list[str] = []
    for recipient in batch["recipients"]:
        result = send_customer_recovery_whatsapp(
            settings, recipient["whatsapp_address"], recipient["message_body"]
        )
        if result["provider_message_id"]:
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE customer_recovery_recipients
                    SET status = 'sent', provider_message_id = ?, follow_up_due_at = ?
                    WHERE recipient_id = ?
                    """,
                    (
                        result["provider_message_id"],
                        (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                        recipient["recipient_id"],
                    ),
                )
        else:
            failures.append(str(result["safe_error"] or "Twilio did not accept the message."))
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE customer_recovery_batches SET status = 'sent', sent_at = ? WHERE batch_id = ?",
            (_now(), batch_id),
        )
    return {**batch_for_merchant(database, merchant_id, batch_id), "send_errors": failures}


def record_link_open(database: Database, token: str) -> dict[str, object] | None:
    with database.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM customer_recovery_recipients WHERE link_token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "sent":
            connection.execute(
                """
                UPDATE customer_recovery_recipients
                SET status = 'opened', link_opened_at = ?
                WHERE recipient_id = ?
                """,
                (
                    _now(),
                    row["recipient_id"],
                ),
            )
        return dict(row)


def record_payment_complete(database: Database, token: str) -> bool:
    with database.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM customer_recovery_recipients WHERE link_token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            """
            UPDATE customer_recovery_recipients
            SET status = 'completed', payment_completed_at = ? WHERE recipient_id = ?
            """,
            (_now(), row["recipient_id"]),
        )
    return True


def run_follow_up_monitor(database: Database, settings: Settings, merchant_id: str) -> None:
    """After the approved five-minute window, Gemini sends one helpful follow-up."""

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT r.* FROM customer_recovery_recipients r
            JOIN customer_recovery_batches b ON b.batch_id = r.batch_id
            WHERE b.merchant_id = ? AND r.status IN ('sent', 'opened') AND r.follow_up_due_at <= ?
            """,
            (merchant_id, _now()),
        ).fetchall()
    for row in rows:
        link = _public_link(settings, row["link_token"])
        message = _follow_up_message(settings, link, opened=row["status"] == "opened")
        result = send_customer_recovery_whatsapp(settings, row["whatsapp_address"], message)
        if result["provider_message_id"]:
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE customer_recovery_recipients
                    SET status = 'follow_up_sent', follow_up_message_id = ? WHERE recipient_id = ?
                    """,
                    (result["provider_message_id"], row["recipient_id"]),
                )


def customer_delivery_statuses(
    database: Database, settings: Settings, merchant_id: str, batch_id: str
) -> list[dict[str, str | int | None]]:
    """Return Twilio delivery state for the small approved customer batch."""

    batch = batch_for_merchant(database, merchant_id, batch_id)
    statuses: list[dict[str, str | int | None]] = []
    for recipient in batch["recipients"]:
        message_id = recipient.get("provider_message_id")
        if not message_id:
            statuses.append(
                {
                    "recipient_id": str(recipient["recipient_id"]),
                    "customer_label": str(recipient["customer_label"]),
                    "status": "not_accepted",
                    "error_code": None,
                    "safe_error": "Twilio did not accept the original message.",
                }
            )
            continue
        statuses.append(
            {
                "recipient_id": str(recipient["recipient_id"]),
                "customer_label": str(recipient["customer_label"]),
                **customer_recovery_delivery_status(settings, str(message_id)),
            }
        )
    return statuses


def retry_undelivered_customer_message(
    database: Database,
    settings: Settings,
    merchant_id: str,
    batch_id: str,
    recipient_id: str,
) -> dict[str, str | int | None]:
    """Retry only a recipient Twilio has confirmed as failed or undelivered."""

    batch = batch_for_merchant(database, merchant_id, batch_id)
    recipient = next(
        (item for item in batch["recipients"] if item["recipient_id"] == recipient_id), None
    )
    if recipient is None:
        raise KeyError("Customer recipient not found")
    if recipient["status"] == "completed":
        raise ValueError("This test customer has already completed payment.")
    message_id = recipient.get("provider_message_id")
    if not message_id:
        raise ValueError("The original message was not accepted by Twilio.")
    delivery = customer_recovery_delivery_status(settings, str(message_id))
    if delivery["status"] not in {"failed", "undelivered"}:
        raise ValueError("Only a Twilio-confirmed failed message can be retried.")
    result = send_customer_recovery_whatsapp(
        settings, str(recipient["whatsapp_address"]), str(recipient["message_body"])
    )
    if not result["provider_message_id"]:
        return {
            "recipient_id": recipient_id,
            "status": "not_accepted",
            "error_code": None,
            "safe_error": result["safe_error"],
        }
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE customer_recovery_recipients
            SET provider_message_id = ?, status = 'sent', follow_up_due_at = ?
            WHERE recipient_id = ?
            """,
            (
                result["provider_message_id"],
                (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                recipient_id,
            ),
        )
    return {
        "recipient_id": recipient_id,
        "status": "accepted",
        "error_code": None,
        "safe_error": None,
    }


def batch_for_merchant(
    database: Database, merchant_id: str, batch_id: str | None = None
) -> dict[str, object]:
    with database.connect() as connection:
        clause, params = (
            ("AND b.batch_id = ?", (merchant_id, batch_id)) if batch_id else ("", (merchant_id,))
        )
        batch = connection.execute(
            f"SELECT b.* FROM customer_recovery_batches b WHERE b.merchant_id = ? {clause} ORDER BY b.created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if batch is None:
            raise KeyError("Customer recovery batch not found")
        recipients = connection.execute(
            "SELECT * FROM customer_recovery_recipients WHERE batch_id = ? ORDER BY customer_label",
            (batch["batch_id"],),
        ).fetchall()
    return {**dict(batch), "recipients": [dict(item) for item in recipients]}


def latest_batch(database: Database, merchant_id: str) -> dict[str, object] | None:
    try:
        return batch_for_merchant(database, merchant_id)
    except KeyError:
        return None
