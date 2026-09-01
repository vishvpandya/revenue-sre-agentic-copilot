"""Portfolio-level scoring and simple copilot answers for the demo."""

from __future__ import annotations

from datetime import datetime

from recovery_orchestrator.models import RecoveryCase, RecoveryStatus

AUTO_MESSAGE_SCORE = 70
CALL_AFTER_NO_REPLIES = 2
MONITOR_AFTER_CONTACTS = 6
REPLY_BUFFER_HOURS = 12


def _matches_case_text(case: RecoveryCase, normalized_question: str) -> bool:
    identifiers = [
        case.case_id,
        case.customer.customer_id,
        case.customer.name,
        case.external_reference,
        case.subscription_id,
    ]
    if any(value and value.lower() in normalized_question for value in identifiers):
        return True
    name_tokens = [
        token
        for token in case.customer.name.lower().replace(".", "").split()
        if len(token) >= 3 and token not in {"pvt", "ltd", "llp"}
    ]
    return bool(name_tokens) and all(token in normalized_question for token in name_tokens[:2])


def portfolio_bucket(case: RecoveryCase) -> str:
    if case.status is RecoveryStatus.PAID:
        return "recovered"
    if case.status in {RecoveryStatus.AWAITING_HUMAN, RecoveryStatus.ESCALATED} or case.has_dispute:
        return "escalated"
    if case.status in {
        RecoveryStatus.PTP_RECORDED,
        RecoveryStatus.PARTIALLY_PAID,
        RecoveryStatus.AWAITING_RESPONSE,
    }:
        return "watchlist"
    return "needs_action"


def recovery_recommendation(case: RecoveryCase, now: datetime) -> tuple[str, str]:
    signals = case.signals
    if case.status is RecoveryStatus.PAID:
        return "closed_no_action", "Already paid; keep visible only for audit and history."
    if case.has_dispute:
        return "human_review", "Invoice/payment dispute is present; do not auto-collect."
    if (
        signals.no_reply_count >= MONITOR_AFTER_CONTACTS
        or case.contact_count >= MONITOR_AFTER_CONTACTS
    ):
        return "monitor_closely", "Many contacts with weak response; put in high-risk manager list."
    if signals.no_reply_count >= CALL_AFTER_NO_REPLIES or signals.calls_made > 0:
        return "call_next", "Repeated message attempts mean the next best step is a call."
    if (
        signals.recovery_score >= AUTO_MESSAGE_SCORE
        and signals.predicted_payment_probability >= 0.7
    ):
        return "auto_message", "High recovery score; safe to send a bounded payment reminder/link."
    if signals.reply_due_at and signals.reply_due_at <= now and not signals.last_customer_reply:
        return "no_reply_12h", "12-hour reply buffer expired; mark no reply and follow up."
    if case.outstanding_balance_paise and case.outstanding_balance_paise > 50_000_00:
        return "manager_approve", "High-value outstanding balance needs manager approval."
    return "prepare_reminder", "Prepare a reminder, but keep it in the review queue."


def portfolio_scan(cases: list[RecoveryCase], now: datetime) -> dict[str, object]:
    rows = []
    for case in cases:
        action, reason = recovery_recommendation(case, now)
        rows.append(
            {
                "case_id": case.case_id,
                "party_name": case.customer.name,
                "bucket": portfolio_bucket(case),
                "recommended_action": action,
                "reason": reason,
                "due_at": case.signals.due_at.isoformat() if case.signals.due_at else None,
                "days_overdue": case.signals.days_overdue,
                "messages_sent": case.signals.messages_sent,
                "calls_made": case.signals.calls_made,
                "no_reply_count": case.signals.no_reply_count,
                "link_click_count": case.signals.link_click_count,
                "recovery_score": case.signals.recovery_score,
                "predicted_payment_probability": case.signals.predicted_payment_probability,
                "outstanding_balance_paise": case.outstanding_balance_paise,
            }
        )
    return {
        "assumptions": {
            "reply_buffer_hours": REPLY_BUFFER_HOURS,
            "auto_message_score": AUTO_MESSAGE_SCORE,
            "call_after_no_replies": CALL_AFTER_NO_REPLIES,
            "monitor_after_contacts": MONITOR_AFTER_CONTACTS,
        },
        "summary": {
            "needs_action": sum(row["bucket"] == "needs_action" for row in rows),
            "watchlist": sum(row["bucket"] == "watchlist" for row in rows),
            "recovered": sum(row["bucket"] == "recovered" for row in rows),
            "escalated": sum(row["bucket"] == "escalated" for row in rows),
            "auto_message": sum(row["recommended_action"] == "auto_message" for row in rows),
            "call_next": sum(row["recommended_action"] == "call_next" for row in rows),
            "monitor_closely": sum(row["recommended_action"] == "monitor_closely" for row in rows),
        },
        "rows": sorted(
            rows,
            key=lambda row: (
                row["bucket"] == "recovered",
                -int(row["days_overdue"]),
                -int(row["outstanding_balance_paise"] or 0),
            ),
        ),
    }


def answer_portfolio_question(
    question: str, cases: list[RecoveryCase], now: datetime
) -> dict[str, object]:
    normalized = question.lower()
    scan = portfolio_scan(cases, now)
    matched = [case for case in cases if _matches_case_text(case, normalized)]
    if not matched:
        risky = [
            row
            for row in scan["rows"]
            if row["recommended_action"] in {"monitor_closely", "call_next", "human_review"}
        ][:5]
        return {
            "answer": (
                "I could not find one exact case in your question, so I pulled the current risk "
                "list. These are the cases a manager should inspect first."
            ),
            "cases": risky,
            "source": "synthetic_portfolio_scan",
        }
    case = matched[0]
    action, reason = recovery_recommendation(case, now)
    signals = case.signals
    answer = (
        f"{case.customer.name} is in the {portfolio_bucket(case).replace('_', ' ')} bucket. "
        f"The outstanding amount is INR {(case.outstanding_balance_paise or 0) / 100:,.2f}. "
        f"It is {signals.days_overdue} day(s) overdue with {signals.messages_sent} message(s), "
        f"{signals.calls_made} call(s), and {signals.no_reply_count} no-reply event(s). "
        f"Recommended next step: {action.replace('_', ' ')}. Reason: {reason}"
    )
    return {
        "answer": answer,
        "cases": [
            {
                "case_id": case.case_id,
                "party_name": case.customer.name,
                "reference": case.external_reference,
                "subscription_id": case.subscription_id,
                "status": case.status.value,
                "recommended_action": action,
                "reason": reason,
                "signals": signals.model_dump(mode="json"),
            }
        ],
        "source": "synthetic_case_history",
    }
