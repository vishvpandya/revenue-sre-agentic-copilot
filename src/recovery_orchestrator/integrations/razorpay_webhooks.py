"""Signature verification and durable ingestion for Razorpay webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from recovery_orchestrator.db.repositories import ExternalEventRepository


class InvalidWebhookSignature(ValueError):
    pass


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> None:
    if not signature or not secret:
        raise InvalidWebhookSignature("signature and webhook secret are required")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidWebhookSignature("Razorpay webhook signature verification failed")


@dataclass(frozen=True)
class IngestedWebhook:
    event_id: str
    event_type: str
    is_duplicate: bool
    payload: dict[str, Any]


class RazorpayWebhookInbox:
    def __init__(self, repository: ExternalEventRepository, webhook_secret: str) -> None:
        self.repository = repository
        self.webhook_secret = webhook_secret

    def ingest(
        self,
        *,
        raw_body: bytes,
        signature: str,
        received_at: datetime,
        delivery_id: str | None = None,
    ) -> IngestedWebhook:
        verify_webhook_signature(raw_body, signature, self.webhook_secret)
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("webhook body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("webhook body must be a JSON object")

        event_type = payload.get("event")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("webhook event type is missing")
        event_id = delivery_id or f"rzp-{hashlib.sha256(raw_body).hexdigest()}"
        receipt = self.repository.receive(
            event_id=event_id,
            source="razorpay",
            event_type=event_type,
            signature_valid=True,
            payload=payload,
            received_at=received_at,
        )
        return IngestedWebhook(
            event_id=event_id,
            event_type=event_type,
            is_duplicate=not receipt.created,
            payload=payload,
        )
