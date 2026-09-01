"""Small, injectable boundary around Razorpay Payment Links."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from recovery_orchestrator.models import ProposedAction, RecoveryCase


class PaymentLinkResource(Protocol):
    def create(self, data: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PaymentLink:
    id: str
    short_url: str
    reference_id: str
    amount_paise: int
    currency: str
    status: str


def stable_reference_id(action_id: str) -> str:
    """Derive a retry-stable reference that fits Razorpay's 40-char limit."""

    digest = hashlib.sha256(action_id.encode()).hexdigest()[:24]
    return f"recovery-{digest}"


class RazorpayPaymentGateway:
    def __init__(self, payment_links: PaymentLinkResource) -> None:
        self._payment_links = payment_links

    @classmethod
    def from_credentials(cls, key_id: str, key_secret: str) -> RazorpayPaymentGateway:
        import razorpay

        client = razorpay.Client(auth=(key_id, key_secret))
        return cls(client.payment_link)

    def create_payment_link(
        self,
        case: RecoveryCase,
        action: ProposedAction,
        *,
        callback_url: str | None = None,
    ) -> PaymentLink:
        reference_id = stable_reference_id(action.action_id)
        amount_paise = int(action.params.get("amount_paise", case.outstanding_balance_paise))
        if amount_paise <= 0 or amount_paise > case.outstanding_balance_paise:
            raise ValueError("payment-link amount must be within the outstanding balance")

        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": case.currency,
            "reference_id": reference_id,
            "description": f"Recovery payment for {case.case_id}",
            "customer": {"name": case.customer.name},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "case_id": case.case_id,
                "action_id": action.action_id,
            },
        }
        if callback_url:
            payload.update(callback_url=callback_url, callback_method="get")

        response = self._payment_links.create(payload)
        return PaymentLink(
            id=str(response["id"]),
            short_url=str(response["short_url"]),
            reference_id=str(response.get("reference_id", reference_id)),
            amount_paise=int(response.get("amount", amount_paise)),
            currency=str(response.get("currency", case.currency)),
            status=str(response["status"]),
        )
