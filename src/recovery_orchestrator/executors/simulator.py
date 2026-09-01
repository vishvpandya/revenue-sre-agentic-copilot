"""Deterministic adapter that proves orchestration without external side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from recovery_orchestrator.db.repositories import ActionLedger
from recovery_orchestrator.models import (
    ActionExecutionStatus,
    ActionType,
    ExecutionResult,
    ProposedAction,
    RecoveryCase,
    RecoveryStatus,
)


def _updated_case(case: RecoveryCase, **changes: object) -> RecoveryCase:
    values = case.model_dump()
    values.update(changes)
    values["outstanding_balance_paise"] = int(values["amount_due_paise"]) - int(
        values["amount_recovered_paise"]
    )
    return RecoveryCase.model_validate(values)


def _personalized_delivery(case: RecoveryCase, action: ProposedAction) -> dict[str, object]:
    amount = f"₹{case.outstanding_balance_paise / 100:,.2f}"
    channel = case.customer.preferred_channel
    if action.type is ActionType.OFFER_PAYMENT_PLAN:
        plan = action.params.get("plan", {})
        upfront = int(plan.get("upfront_amount_paise", 0))
        body = (
            f"Hi {case.customer.name}, for {case.external_reference}, we can offer a payment "
            f"plan on {amount}. The first installment is ₹{upfront / 100:,.2f}. "
            "Reply CONFIRM to continue."
        )
    elif action.type is ActionType.SEND_PAYMENT_LINK:
        body = (
            f"Hi {case.customer.name}, your payment of {amount} for "
            f"{case.external_reference} did not complete. Use the secure test payment link below."
        )
    else:
        body = (
            f"Hi {case.customer.name}, this is a reminder that {amount} for "
            f"{case.external_reference} is outstanding. Please reply if you need help."
        )
    return {
        "delivery_mode": "simulated_channel",
        "channel": channel,
        "recipient_name": case.customer.name,
        "recipient_phone": case.customer.phone,
        "message": body,
    }


@dataclass
class SeededSimulatorExecutor:
    ledger: ActionLedger
    paying_case_ids: set[str] = field(default_factory=set)

    def execute(
        self,
        case: RecoveryCase,
        action: ProposedAction,
        now: datetime,
    ) -> ExecutionResult:
        reference_id = f"sim-{action.action_id}"[:40]
        existing, created = self.ledger.register_pending(
            case.case_id,
            action,
            now,
            reference_id=reference_id,
        )
        if not created and existing.status == ActionExecutionStatus.SUCCEEDED.value:
            return ExecutionResult(
                case=case,
                outcome="idempotent_replay",
                response=existing.response or {},
            )

        event_id = f"sim-event-{action.action_id}"
        if action.type is ActionType.STOP:
            updated = _updated_case(
                case,
                status=RecoveryStatus.STOPPED,
                terminal_reason="strategy_selected_stop",
            )
            outcome = "stopped"
            response = {"status": "stopped"}
        elif action.type is ActionType.ESCALATE:
            updated = _updated_case(
                case,
                status=RecoveryStatus.AWAITING_HUMAN,
                awaiting_human_reason="strategy_selected_escalation",
            )
            outcome = "awaiting_human"
            response = {"status": "awaiting_human"}
        elif action.type in {
            ActionType.SEND_PAYMENT_LINK,
            ActionType.OFFER_DISCOUNT_LINK,
        }:
            payment_link_id = f"sim_plink_{case.case_id}"
            if case.case_id in self.paying_case_ids:
                updated = _updated_case(
                    case,
                    status=RecoveryStatus.PAID,
                    amount_recovered_paise=case.amount_due_paise,
                    payment_link_id=payment_link_id,
                    payment_id=f"sim_pay_{case.case_id}",
                    terminal_reason="simulated_payment_captured",
                )
                outcome = "payment_captured"
                response = {
                    "status": "paid",
                    "payment_link_id": payment_link_id,
                    "payment_id": updated.payment_id,
                    "amount_paid_paise": updated.amount_recovered_paise,
                }
            else:
                updated = _updated_case(
                    case,
                    status=RecoveryStatus.AWAITING_PAYMENT,
                    payment_link_id=payment_link_id,
                )
                outcome = "awaiting_payment"
                response = {"status": "created", "payment_link_id": payment_link_id}
        else:
            extra_changes: dict[str, object] = {}
            if action.type is ActionType.OFFER_PAYMENT_PLAN:
                extra_changes["payment_plan"] = action.params.get("plan")
            updated = _updated_case(
                case,
                status=RecoveryStatus.AWAITING_RESPONSE,
                **extra_changes,
            )
            outcome = "awaiting_response"
            response = {"status": "message_sent"}

        if action.type in {
            ActionType.SEND_PAYMENT_LINK,
            ActionType.SEND_REMINDER,
            ActionType.OFFER_DISCOUNT_LINK,
            ActionType.OFFER_PAYMENT_PLAN,
        }:
            response.update(_personalized_delivery(case, action))
            if "payment_link_id" in response:
                response["test_payment_url"] = f"https://rzp.test/pay/{response['payment_link_id']}"

        self.ledger.mark_result(
            action.action_id,
            ActionExecutionStatus.SUCCEEDED,
            now,
            response=response,
        )
        return ExecutionResult(
            case=updated,
            outcome=outcome,
            external_event_id=event_id,
            response=response,
        )
