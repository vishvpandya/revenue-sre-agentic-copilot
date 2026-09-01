"""Controlled Twilio WhatsApp Sandbox delivery for a personal demo recipient."""

# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.settings import Settings


def _attempt_id(notification_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-live-whatsapp:{notification_id}"))


def _whatsapp_address(value: str) -> str:
    """Accept an E.164 number in .env, but always send Twilio a WhatsApp address."""

    cleaned = value.strip()
    return cleaned if cleaned.startswith("whatsapp:") else f"whatsapp:{cleaned}"


def _reply_interpretation(body: str) -> str:
    """Describe a reply without treating a chat message as authorization to change payments."""

    reply = body.strip()
    words = reply.lower()
    if any(word in words for word in ("approve", "approved", "go ahead", "yes")):
        return (
            "The recipient appears to agree with the proposed next step. This is recorded as an "
            "acknowledgement only; an explicit approval in Revenue SRE is still required before "
            "any action."
        )
    if any(word in words for word in ("check", "review", "tomorrow", "looking", "investigat")):
        return (
            "The recipient acknowledged the request and indicated that their team will review it. "
            "No payment configuration has changed."
        )
    return (
        "A WhatsApp reply was received and recorded as evidence. A person should review its "
        "meaning "
        "before taking any payment action."
    )


def _record_reply_agent_step(
    connection,
    *,
    intervention_id: str | None,
    provider_message_id: str,
    body: str,
    received_at: str,
) -> None:
    """Append an audited acknowledgement step to the relevant investigation, if one exists."""

    if intervention_id is None:
        return
    investigation = connection.execute(
        "SELECT investigation_id FROM sre_interventions WHERE intervention_id = ?",
        (intervention_id,),
    ).fetchone()
    if investigation is None:
        return
    investigation_id = investigation["investigation_id"]
    sequence_no = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
            "FROM sre_agent_steps WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()["next_sequence"]
    )
    step_id = str(uuid5(NAMESPACE_URL, f"{investigation_id}:whatsapp-reply:{provider_message_id}"))
    connection.execute(
        """
        INSERT INTO sre_agent_steps(
            step_id, investigation_id, sequence_no, agent_name, tool_name, question,
            observation_json, conclusion, created_at
        ) VALUES (?, ?, ?, 'Stakeholder Reply Monitor', 'verify_signed_whatsapp_reply', ?, ?, ?, ?)
        """,
        (
            step_id,
            investigation_id,
            sequence_no,
            "Did the responsible team respond to the proposed safe next step?",
            json.dumps(
                {
                    "reply": body.strip(),
                    "provider_message_id": provider_message_id,
                    "verified": True,
                },
                sort_keys=True,
            ),
            _reply_interpretation(body),
            received_at,
        ),
    )


def valid_webhook_signature(
    *, auth_token: str, url: str, form_fields: dict[str, str], signature: str
) -> bool:
    """Validate Twilio's signed webhook before treating a reply as agent evidence."""

    payload = url + "".join(f"{key}{form_fields[key]}" for key in sorted(form_fields))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def record_inbound_reply(
    database: Database,
    *,
    provider_message_id: str,
    from_address: str,
    body: str,
    verification_status: str = "verified",
) -> dict[str, object]:
    """Attach a real reply to the latest live WhatsApp alert for that test number."""

    sender = _whatsapp_address(from_address)
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM whatsapp_inbound_events WHERE provider_message_id = ?",
            (provider_message_id,),
        ).fetchone()
        if existing:
            return {**dict(existing), "replayed": True}
        linked = connection.execute(
            """
            SELECT o.merchant_id, o.notification_id, o.intervention_id
            FROM notification_live_attempts a
            JOIN notification_outbox o ON o.notification_id = a.notification_id
            WHERE a.provider = 'twilio_whatsapp' AND a.status = 'live_sent'
              AND a.recipient_email = ?
            ORDER BY a.created_at DESC LIMIT 1
            """,
            (sender,),
        ).fetchone()
        event_id = str(uuid5(NAMESPACE_URL, f"revenue-sre-whatsapp-inbound:{provider_message_id}"))
        received_at = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO whatsapp_inbound_events(
                event_id, merchant_id, notification_id, from_address, body, provider_message_id,
                verification_status, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                linked["merchant_id"] if linked else None,
                linked["notification_id"] if linked else None,
                sender,
                body.strip(),
                provider_message_id,
                verification_status,
                received_at,
            ),
        )
        _record_reply_agent_step(
            connection,
            intervention_id=linked["intervention_id"] if linked else None,
            provider_message_id=provider_message_id,
            body=body,
            received_at=received_at,
        )
    return {
        "event_id": event_id,
        "merchant_id": linked["merchant_id"] if linked else None,
        "notification_id": linked["notification_id"] if linked else None,
        "from_address": sender,
        "body": body.strip(),
        "interpretation": _reply_interpretation(body),
        "provider_message_id": provider_message_id,
        "verification_status": verification_status,
        "replayed": False,
    }


def inbound_replies(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM whatsapp_inbound_events
            WHERE merchant_id = ? ORDER BY received_at DESC
            """,
            (merchant_id,),
        ).fetchall()
    return [
        {**dict(row), "interpretation": _reply_interpretation(str(row["body"]))} for row in rows
    ]


def _record_attempt(
    database: Database,
    *,
    attempt_id: str,
    notification_id: str,
    recipient: str,
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
            # Only a provider-accepted send is a true duplicate. A prior click made
            # before credentials were configured must remain retryable.
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
                    recipient,
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
                "provider": "twilio_whatsapp",
                "recipient": recipient,
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
            ) VALUES (?, ?, 'twilio_whatsapp', ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                notification_id,
                recipient,
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
        "provider": "twilio_whatsapp",
        "recipient": recipient,
        "status": status,
        "provider_message_id": provider_message_id,
        "safe_error": safe_error,
        "replayed": False,
    }


def deliver_controlled_whatsapp(
    database: Database, settings: Settings, notification_id: str
) -> dict[str, object]:
    """Send one alert to TWILIO_TEST_RECIPIENT, if it joined the Sandbox."""

    with database.connect() as connection:
        notification = connection.execute(
            "SELECT * FROM notification_outbox WHERE notification_id = ?", (notification_id,)
        ).fetchone()
    if notification is None:
        raise KeyError("Notification not found")

    attempt_id = _attempt_id(notification_id)
    idempotency_key = f"twilio-whatsapp:{notification_id}"
    recipient = (
        _whatsapp_address(settings.twilio_test_recipient)
        if settings.twilio_test_recipient
        else "not-configured"
    )
    required = (
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_whatsapp_from,
        settings.twilio_test_recipient,
    )
    if not all(required):
        return _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient=recipient,
            status="not_configured",
            provider_message_id=None,
            safe_error=(
                "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, "
                "and TWILIO_TEST_RECIPIENT in .env."
            ),
            idempotency_key=idempotency_key,
        )

    account_sid = settings.twilio_account_sid.get_secret_value()
    auth_token = settings.twilio_auth_token.get_secret_value()
    sender = _whatsapp_address(settings.twilio_whatsapp_from or "")
    body = f"[Revenue SRE test alert] {notification['subject']}\n\n{notification['body']}"
    payload = urlencode({"From": sender, "To": recipient, "Body": body}).encode("utf-8")
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Twilio API endpoint
            response_payload = json.loads(response.read().decode("utf-8"))
        return _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient=recipient,
            status="live_sent",
            provider_message_id=response_payload.get("sid"),
            safe_error=None,
            idempotency_key=idempotency_key,
        )
    except HTTPError as exc:
        provider_error = f"Twilio returned HTTP {exc.code}."
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            code = error_payload.get("code")
            message = " ".join(str(error_payload.get("message", "")).split())[:240]
            message = re.sub(r"\bAC[a-zA-Z0-9]{32}\b", "[account SID hidden]", message)
            if code and message:
                provider_error = f"Twilio error {code}: {message}"
            elif message:
                provider_error = f"Twilio returned HTTP {exc.code}: {message}"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient=recipient,
            status="live_failed",
            provider_message_id=None,
            safe_error=provider_error,
            idempotency_key=idempotency_key,
        )
    except URLError:
        return _record_attempt(
            database,
            attempt_id=attempt_id,
            notification_id=notification_id,
            recipient=recipient,
            status="live_failed",
            provider_message_id=None,
            safe_error="Could not reach Twilio.",
            idempotency_key=idempotency_key,
        )


def send_customer_recovery_whatsapp(
    settings: Settings, recipient: str, body: str
) -> dict[str, str | None]:
    """Send one approved customer-recovery message to a Sandbox-joined test recipient."""

    required = (
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_whatsapp_from,
    )
    if not all(required):
        return {
            "provider_message_id": None,
            "safe_error": "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM in .env.",
        }
    account_sid = settings.twilio_account_sid.get_secret_value()
    auth_token = settings.twilio_auth_token.get_secret_value()
    sender = _whatsapp_address(settings.twilio_whatsapp_from or "")
    payload = urlencode({"From": sender, "To": _whatsapp_address(recipient), "Body": body}).encode(
        "utf-8"
    )
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed Twilio API endpoint
            payload = json.loads(response.read().decode("utf-8"))
        return {"provider_message_id": str(payload.get("sid") or ""), "safe_error": None}
    except HTTPError as exc:
        return {"provider_message_id": None, "safe_error": f"Twilio returned HTTP {exc.code}."}
    except URLError:
        return {"provider_message_id": None, "safe_error": "Could not reach Twilio."}


def customer_recovery_delivery_status(
    settings: Settings, provider_message_id: str
) -> dict[str, str | int | None]:
    """Read Twilio's final delivery state without exposing credentials to the dashboard."""

    required = (settings.twilio_account_sid, settings.twilio_auth_token)
    if not all(required):
        return {"status": "not_configured", "error_code": None, "safe_error": "Twilio is not configured."}
    account_sid = settings.twilio_account_sid.get_secret_value()
    auth_token = settings.twilio_auth_token.get_secret_value()
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode("ascii")
    request = Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{provider_message_id}.json",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed provider endpoint
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "status": str(payload.get("status") or "unknown"),
            "error_code": payload.get("error_code"),
            "safe_error": None,
        }
    except HTTPError as exc:
        return {
            "status": "unknown",
            "error_code": exc.code,
            "safe_error": "Twilio could not return the delivery status.",
        }
    except URLError:
        return {
            "status": "unknown",
            "error_code": None,
            "safe_error": "Could not reach Twilio to check delivery status.",
        }
