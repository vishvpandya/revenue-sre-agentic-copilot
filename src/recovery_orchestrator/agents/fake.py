"""Deterministic fake agents used before external model integration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from recovery_orchestrator.models import (
    ActionType,
    Diagnosis,
    PaymentInstallment,
    PaymentPlan,
    ProposedAction,
    RecoveryCase,
    ReplyInterpretation,
    RevenueEventType,
    RootCause,
)


@dataclass(frozen=True)
class FakeRecoveryAgents:
    forced_action: ActionType | None = None

    def diagnose(self, case: RecoveryCase) -> Diagnosis:
        failure_code = case.signals.payment_failure_code or ""
        invoice_status = case.signals.invoice_status or ""
        if case.has_dispute:
            root_cause = RootCause.DISPUTE
            intent = 0.2
        elif "insufficient_funds" in failure_code or "cashflow" in invoice_status:
            root_cause = RootCause.INSUFFICIENT_FUNDS
            intent = 0.75
        elif "no_response" in invoice_status or case.contact_count >= 3:
            root_cause = RootCause.LOW_INTENT
            intent = 0.15
        elif case.event_type in {
            RevenueEventType.CHECKOUT_FAILURE,
            RevenueEventType.SUBSCRIPTION_FAILURE,
        }:
            root_cause = RootCause.TECHNICAL
            intent = 0.85
        elif case.event_type in {
            RevenueEventType.OVERDUE_INVOICE,
            RevenueEventType.CHECKOUT_ABANDONMENT,
        }:
            root_cause = RootCause.FORGOT
            intent = 0.7
        else:
            root_cause = RootCause.UNKNOWN
            intent = 0.5
        evidence = [f"event_type={case.event_type.value}"]
        if failure_code:
            evidence.append(f"payment_failure_code={failure_code}")
        if invoice_status:
            evidence.append(f"invoice_status={invoice_status}")
        if case.signals.last_customer_reply:
            evidence.append(f"last_reply={case.signals.last_customer_reply[:80]}")
        confidence = max(
            0.35,
            min(
                0.95,
                (
                    case.signals.predicted_payment_probability
                    + (case.signals.recovery_score / 100)
                    + case.customer.prior_ptp_kept_ratio
                )
                / 3,
            ),
        )
        return Diagnosis(
            root_cause=root_cause,
            intent_score=intent,
            confidence=confidence,
            evidence=evidence,
            reasoning="Seeded evidence was mapped to a reproducible recovery diagnosis.",
        )

    def propose(self, case: RecoveryCase, diagnosis: Diagnosis) -> ProposedAction:
        action_type = self.forced_action
        if action_type is None:
            if diagnosis.root_cause is RootCause.DISPUTE:
                action_type = ActionType.ESCALATE
            elif diagnosis.root_cause is RootCause.INSUFFICIENT_FUNDS:
                action_type = ActionType.OFFER_PAYMENT_PLAN
            elif diagnosis.root_cause is RootCause.LOW_INTENT:
                action_type = ActionType.STOP
            elif diagnosis.root_cause is RootCause.FORGOT:
                action_type = ActionType.SEND_REMINDER
            elif diagnosis.root_cause is RootCause.TECHNICAL:
                action_type = ActionType.SEND_PAYMENT_LINK
            else:
                action_type = ActionType.SCHEDULE_FOLLOWUP
        params = {}
        if action_type is ActionType.OFFER_PAYMENT_PLAN:
            total = case.outstanding_balance_paise
            first = (total + 3) // 4
            remaining = total - first
            second = remaining // 2
            third = remaining - second
            timezone = ZoneInfo(case.customer.timezone)
            plan = PaymentPlan(
                collectible_amount_paise=total,
                upfront_amount_paise=first,
                installments=[
                    PaymentInstallment(
                        amount_paise=first,
                        due_at=datetime(2026, 8, 28, 18, 0, tzinfo=timezone),
                    ),
                    PaymentInstallment(
                        amount_paise=second,
                        due_at=datetime(2026, 9, 10, 18, 0, tzinfo=timezone),
                    ),
                    PaymentInstallment(
                        amount_paise=third,
                        due_at=datetime(2026, 9, 25, 18, 0, tzinfo=timezone),
                    ),
                ],
            )
            params["plan"] = plan.model_dump(mode="json")
        return ProposedAction(
            action_id=f"{case.case_id}:strategy:{case.strategy_attempt_count + 1}",
            type=action_type,
            params=params,
            rationale=f"Selected from diagnosed root cause: {diagnosis.root_cause.value}.",
        )

    def interpret_reply(self, case: RecoveryCase, message: str) -> ReplyInterpretation:
        normalized = message.lower()
        ptp_at = None
        can_pay_now_paise = None
        amount_match = re.search(r"(?:₹|rs\.?|inr)?\s*([\d,]+)\s*(?:now|abhi)", normalized)
        if amount_match:
            can_pay_now_paise = int(amount_match.group(1).replace(",", "")) * 100
        if "next friday" in normalized:
            ptp_at = datetime(2026, 9, 4, 18, 0, tzinfo=ZoneInfo(case.customer.timezone))
        elif "tomorrow" in normalized:
            ptp_at = datetime(2026, 8, 29, 11, 0, tzinfo=ZoneInfo(case.customer.timezone))
        if "already paid" in normalized or "payment is done" in normalized:
            intent = "already_paid"
        elif "wrong number" in normalized or "not the customer" in normalized:
            intent = "wrong_person"
        elif "call me" in normalized or "phone me" in normalized:
            intent = "needs_callback"
        elif "dispute" in normalized or "wrong invoice" in normalized:
            intent = "dispute"
        elif "stop" in normalized or "opt out" in normalized:
            intent = "opt_out"
        elif (
            "cannot pay" in normalized
            or "can't pay" in normalized
            or "cash-flow" in normalized
            or "cash flow" in normalized
        ):
            intent = "cannot_pay"
        elif "installment" in normalized or "payment plan" in normalized or "split" in normalized:
            intent = "needs_plan"
        elif "link" in normalized and ("not working" in normalized or "failed" in normalized):
            intent = "technical_issue"
        elif "pay" in normalized:
            intent = "will_pay"
        else:
            intent = "unclear"
        return ReplyInterpretation(
            intent=intent,
            ptp_at=ptp_at,
            can_pay_now_paise=can_pay_now_paise,
            requested_plan_text=message if intent in {"needs_plan", "cannot_pay"} else None,
            reasoning="Deterministic fixture interpretation for local workflow verification.",
        )
