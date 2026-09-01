"""Stage 7 Resend adapter. It sends only to a controlled test recipient."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.settings import Settings


def _payload_fingerprint(*, recipient: str, subject: str, body: str) -> str:
    """Give Resend a stable key for one exact email payload, not merely one alert."""

    content = "\n".join((recipient, subject, body)).encode("utf-8")
    return sha256(content).hexdigest()[:24]


def _attempt_id(notification_id: str, fingerprint: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-live-email:{notification_id}:{fingerprint}"))


def deliver_controlled_email(
    database: Database,
    settings: Settings,
    notification_id: str,
    *,
    subject: str | None = None,
    body: str | None = None,
) -> dict[str, object]:
    """Send a real email only to RESEND_TEST_RECIPIENT, never a synthetic contact address."""

    with database.connect() as connection:
        notification = connection.execute(
            "SELECT * FROM notification_outbox WHERE notification_id = ?", (notification_id,)
        ).fetchone()
    if notification is None:
        raise KeyError("Notification not found")
    subject = (subject or notification["subject"]).strip()
    body = (body or notification["body"]).strip()
    if not subject or not body:
        raise ValueError("Email subject and body are required")
    recipient = settings.resend_test_recipient
    fingerprint = _payload_fingerprint(
        recipient=recipient or "not-configured", subject=subject, body=body
    )
    attempt_id = _attempt_id(notification_id, fingerprint)
    idempotency_key = f"resend:{notification_id}:{fingerprint}"

    # Once the provider accepted an email for this alert, never send another one
    # merely because the dashboard was refreshed or a user clicked again.
    with database.connect() as connection:
        accepted = connection.execute(
            """
            SELECT * FROM notification_live_attempts
            WHERE notification_id = ? AND provider = 'resend' AND status = 'live_sent'
            ORDER BY created_at DESC LIMIT 1
            """,
            (notification_id,),
        ).fetchone()
    if accepted:
        return {**dict(accepted), "replayed": True}

    if not settings.resend_api_key or not settings.resend_from_email or not recipient:
        return _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient_email=recipient or "not-configured",
            status="not_configured",
            provider_message_id=None,
            safe_error="Set RESEND_API_KEY, RESEND_FROM_EMAIL, and RESEND_TEST_RECIPIENT in .env.",
            idempotency_key=idempotency_key,
        )
    payload = {
        "from": settings.resend_from_email,
        "to": [recipient],
        "subject": f"[Revenue SRE test] {subject}",
        "text": body,
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            # Required by Resend for direct HTTP requests; missing it returns HTTP 403.
            "User-Agent": "revenue-sre-copilot/1.0",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed provider endpoint
            response_payload = json.loads(response.read().decode("utf-8"))
        result = _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient_email=recipient,
            status="live_sent",
            provider_message_id=response_payload.get("id"),
            safe_error=None,
            idempotency_key=idempotency_key,
        )
        if result["status"] == "live_sent" and not result.get("replayed"):
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE notification_outbox "
                    "SET subject = ?, body = ? WHERE notification_id = ?",
                    (subject, body, notification_id),
                )
        return result
    except HTTPError as exc:
        provider_error = f"Provider returned HTTP {exc.code}."
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = str(payload.get("message") or payload.get("name") or "").strip()
            if message:
                provider_error = f"Provider returned HTTP {exc.code}: {message}"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return _record_attempt(
            database, attempt_id, notification_id, recipient, "live_failed", None,
            provider_error, idempotency_key,
        )
    except URLError:
        return _record_attempt(
            database, attempt_id, notification_id, recipient, "live_failed", None,
            "Could not reach the email provider.", idempotency_key,
        )


def _record_attempt(
    database: Database,
    attempt_id: str,
    notification_id: str,
    recipient_email: str,
    status: str,
    provider_message_id: str | None,
    safe_error: str | None,
    idempotency_key: str,
) -> dict[str, object]:
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM notification_live_attempts WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            # A pre-configuration click did not send anything, so permit retry once
            # real provider settings have been added. Accepted sends remain idempotent.
            if existing["status"] == "live_sent":
                return {**dict(existing), "replayed": True}
            connection.execute(
                """
                UPDATE notification_live_attempts
                SET recipient_email = ?, status = ?, provider_message_id = ?, safe_error = ?,
                    created_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    recipient_email,
                    status,
                    provider_message_id,
                    safe_error,
                    datetime.now(UTC).isoformat(),
                    idempotency_key,
                ),
            )
            return {
                "attempt_id": existing["attempt_id"],
                "notification_id": notification_id,
                "provider": "resend",
                "recipient_email": recipient_email,
                "status": status,
                "provider_message_id": provider_message_id,
                "safe_error": safe_error,
                "replayed": False,
            }
        connection.execute(
            """
            INSERT INTO notification_live_attempts(
                attempt_id, notification_id, provider, recipient_email, status, provider_message_id,
                safe_error, idempotency_key, created_at
            ) VALUES (?, ?, 'resend', ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                notification_id,
                recipient_email,
                status,
                provider_message_id,
                safe_error,
                idempotency_key,
                datetime.now(UTC).isoformat(),
            ),
        )
    return {
        "attempt_id": attempt_id,
        "notification_id": notification_id,
        "provider": "resend",
        "recipient_email": recipient_email,
        "status": status,
        "provider_message_id": provider_message_id,
        "safe_error": safe_error,
        "replayed": False,
    }
