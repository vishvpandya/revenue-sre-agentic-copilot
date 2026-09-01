"""Reproducible, clearly labelled synthetic portfolio for the judge demonstration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from recovery_orchestrator.db.repositories import AuditRepository, CaseRepository
from recovery_orchestrator.models import (
    CustomerProfile,
    ExecutionMode,
    PartyType,
    RecoveryCase,
    RecoveryStatus,
    RevenueEventType,
)

BASE_NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def _dt(days_offset: int, hour: int = 10) -> datetime:
    return BASE_NOW + timedelta(days=days_offset, hours=hour - BASE_NOW.hour)


def _signals(
    *,
    due_days_ago: int,
    messages_sent: int,
    calls_made: int,
    no_reply_count: int,
    paid_probability: float,
    recovery_score: int,
    invoice_status: str | None = None,
    failure_code: str | None = None,
    checkout_stage: str | None = None,
    mandate_status: str | None = None,
    last_reply: str | None = None,
    last_channel: str | None = None,
    link_clicks: int = 0,
    avg_reply_hours: float | None = None,
    on_time_ratio: float = 0.5,
    discount_eligible: bool = False,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    last_message_at = _dt(-1) if messages_sent else None
    reply_due_at = last_message_at + timedelta(hours=12) if last_message_at else None
    return {
        "days_overdue": due_days_ago,
        "due_at": _dt(-due_days_ago),
        "first_contact_at": _dt(-min(due_days_ago, max(messages_sent + calls_made, 1))),
        "last_message_at": last_message_at,
        "reply_due_at": reply_due_at,
        "last_link_click_at": _dt(-1, 14) if link_clicks else None,
        "payment_failure_code": failure_code,
        "checkout_stage": checkout_stage,
        "invoice_status": invoice_status,
        "mandate_status": mandate_status,
        "last_customer_reply": last_reply,
        "last_inbound_channel": last_channel,
        "messages_sent": messages_sent,
        "calls_made": calls_made,
        "no_reply_count": no_reply_count,
        "link_click_count": link_clicks,
        "historical_on_time_payment_ratio": on_time_ratio,
        "average_reply_delay_hours": avg_reply_hours,
        "predicted_payment_probability": paid_probability,
        "recovery_score": recovery_score,
        "discount_eligible": discount_eligible,
        "risk_flags": flags or [],
    }


def _customer(
    customer_id: str,
    name: str,
    phone: str,
    *,
    preferred_channel: str = "whatsapp",
    orders: int = 0,
    delivered: int = 0,
    rto: int = 0,
    prior_ptp_kept_ratio: float = 0.5,
) -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id,
        name=name,
        phone=phone,
        preferred_channel=preferred_channel,
        orders=orders,
        delivered=delivered,
        rto=rto,
        prior_ptp_kept_ratio=prior_ptp_kept_ratio,
    )


def _case(
    *,
    case_id: str,
    customer: CustomerProfile,
    party_type: PartyType,
    event_type: RevenueEventType,
    reference: str,
    amount_due_paise: int,
    signals: dict[str, Any],
    recovered_paise: int = 0,
    status: RecoveryStatus = RecoveryStatus.DETECTED,
    subscription_id: str | None = None,
    has_dispute: bool = False,
    partial_count: int = 0,
    terminal_reason: str | None = None,
    contact_count: int | None = None,
) -> RecoveryCase:
    return RecoveryCase(
        merchant_id="MRC-DEMO-001",
        execution_mode=ExecutionMode.SEEDED_SIMULATION,
        source="synthetic_seeded_portfolio_v2",
        case_id=case_id,
        party_type=party_type,
        event_type=event_type,
        external_reference=reference,
        subscription_id=subscription_id,
        customer=customer,
        signals=signals,
        amount_due_paise=amount_due_paise,
        amount_recovered_paise=recovered_paise,
        status=status,
        has_dispute=has_dispute,
        partial_payment_count=partial_count,
        terminal_reason=terminal_reason,
        contact_count=signals["messages_sent"] + signals["calls_made"]
        if contact_count is None
        else contact_count,
    )


def seeded_cases() -> list[RecoveryCase]:
    """Twenty synthetic companies/customers with visible collection history."""

    return [
        _case(
            case_id="CASE-CUST-1001",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_FAILURE,
            reference="ORDER-80421",
            customer=_customer(
                "CUST-1001",
                "Asha Mehta",
                "+919900001001",
                orders=8,
                delivered=8,
                prior_ptp_kept_ratio=0.9,
            ),
            signals=_signals(
                due_days_ago=1,
                messages_sent=1,
                calls_made=0,
                no_reply_count=0,
                link_clicks=1,
                paid_probability=0.86,
                recovery_score=88,
                failure_code="gateway_timeout",
                checkout_stage="payment_authorization",
                on_time_ratio=0.92,
                flags=["repeat_customer", "high_intent_checkout"],
            ),
            amount_due_paise=1_499_00,
        ),
        _case(
            case_id="CASE-CUST-1002",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_ABANDONMENT,
            reference="CART-99104",
            customer=_customer(
                "CUST-1002", "Rohan Shah", "+919900001002", orders=2, delivered=1, rto=1
            ),
            signals=_signals(
                due_days_ago=2,
                messages_sent=2,
                calls_made=0,
                no_reply_count=2,
                paid_probability=0.42,
                recovery_score=44,
                checkout_stage="address_page_exit",
                on_time_ratio=0.35,
                flags=["cart_abandoned", "one_prior_rto"],
            ),
            amount_due_paise=3_250_00,
        ),
        _case(
            case_id="CASE-CUST-1003-PAID",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_FAILURE,
            reference="ORDER-80435",
            customer=_customer(
                "CUST-1003",
                "Neha Kapoor",
                "+919900001003",
                orders=5,
                delivered=5,
                prior_ptp_kept_ratio=1.0,
            ),
            signals=_signals(
                due_days_ago=0,
                messages_sent=1,
                calls_made=0,
                no_reply_count=0,
                paid_probability=0.98,
                recovery_score=96,
                failure_code="initial_attempt_failed",
                checkout_stage="payment_authorization",
                on_time_ratio=1.0,
                flags=["recovered_by_self_service"],
            ),
            amount_due_paise=2_799_00,
            recovered_paise=2_799_00,
            status=RecoveryStatus.PAID,
            terminal_reason="razorpay_payment_captured",
        ),
        _case(
            case_id="CASE-CUST-1004-PARTIAL",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_FAILURE,
            reference="ORDER-80501",
            customer=_customer(
                "CUST-1004",
                "Imran Qureshi",
                "+919900001004",
                preferred_channel="voice",
                orders=4,
                delivered=4,
            ),
            signals=_signals(
                due_days_ago=5,
                messages_sent=2,
                calls_made=1,
                no_reply_count=0,
                paid_probability=0.69,
                recovery_score=68,
                failure_code="insufficient_funds",
                checkout_stage="bank_authorization",
                last_reply="Call me after salary credit.",
                last_channel="voice_transcript",
                avg_reply_hours=8,
                flags=["partial_payment_received"],
            ),
            amount_due_paise=12_000_00,
            recovered_paise=4_000_00,
            status=RecoveryStatus.PARTIALLY_PAID,
            partial_count=1,
        ),
        _case(
            case_id="CASE-VENDOR-2001",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-ZENITH-884",
            customer=_customer(
                "VENDOR-2001",
                "Zenith Retail Pvt Ltd",
                "+919900002001",
                orders=24,
                delivered=24,
                prior_ptp_kept_ratio=0.8,
            ),
            signals=_signals(
                due_days_ago=21,
                messages_sent=1,
                calls_made=1,
                no_reply_count=1,
                link_clicks=2,
                paid_probability=0.72,
                recovery_score=74,
                invoice_status="accepted_but_unpaid",
                last_reply="Finance team said payment is in approval.",
                last_channel="whatsapp",
                avg_reply_hours=9,
                on_time_ratio=0.78,
                flags=["high_value", "finance_approval_pending"],
            ),
            amount_due_paise=1_25_000_00,
        ),
        _case(
            case_id="CASE-VENDOR-2002",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-NORTHSTAR-219",
            customer=_customer(
                "VENDOR-2002", "Northstar Supplies", "+919900002002", orders=11, delivered=11
            ),
            signals=_signals(
                due_days_ago=14,
                messages_sent=2,
                calls_made=1,
                no_reply_count=0,
                paid_probability=0.63,
                recovery_score=65,
                invoice_status="cashflow_constraint_reported",
                last_reply="Full payment is difficult this week.",
                last_channel="whatsapp",
                avg_reply_hours=6,
                discount_eligible=True,
                flags=["temporary_cashflow_issue"],
            ),
            amount_due_paise=32_000_00,
        ),
        _case(
            case_id="CASE-VENDOR-2003-NOREPLY",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-SILVERLINE-552",
            customer=_customer(
                "VENDOR-2003", "Silverline Wholesale", "+919900002003", orders=9, delivered=9
            ),
            signals=_signals(
                due_days_ago=48,
                messages_sent=5,
                calls_made=2,
                no_reply_count=7,
                paid_probability=0.18,
                recovery_score=20,
                invoice_status="no_response_after_reminders",
                on_time_ratio=0.15,
                flags=["seven_contacts_no_response", "low_engagement"],
            ),
            amount_due_paise=27_800_00,
        ),
        _case(
            case_id="CASE-SUB-3001",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.SUBSCRIPTION_FAILURE,
            reference="PLAN-GROWTH-ANNUAL",
            subscription_id="sub_demo_3001",
            customer=_customer(
                "VENDOR-3001", "Orbit Analytics LLP", "+919900003001", orders=6, delivered=6
            ),
            signals=_signals(
                due_days_ago=3,
                messages_sent=1,
                calls_made=0,
                no_reply_count=1,
                paid_probability=0.67,
                recovery_score=70,
                failure_code="mandate_debit_failed",
                mandate_status="pending",
                flags=["subscription_renewal", "service_active"],
            ),
            amount_due_paise=18_000_00,
        ),
        _case(
            case_id="CASE-DISPUTE-4001",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-DISPUTED-071",
            customer=_customer(
                "VENDOR-4001",
                "BluePeak Distribution",
                "+919900004001",
                preferred_channel="email",
                orders=14,
                delivered=13,
            ),
            signals=_signals(
                due_days_ago=30,
                messages_sent=3,
                calls_made=1,
                no_reply_count=0,
                paid_probability=0.26,
                recovery_score=28,
                invoice_status="disputed_quantity",
                last_reply="Invoice quantity does not match goods received.",
                last_channel="email",
                avg_reply_hours=3,
                flags=["active_dispute", "do_not_auto_collect"],
            ),
            amount_due_paise=44_500_00,
            has_dispute=True,
        ),
        _case(
            case_id="CASE-VENDOR-5001-PAID",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-PRISM-118",
            customer=_customer(
                "VENDOR-5001", "Prism Sports Stores", "+919900005001", orders=18, delivered=18
            ),
            signals=_signals(
                due_days_ago=0,
                messages_sent=1,
                calls_made=0,
                no_reply_count=0,
                paid_probability=0.94,
                recovery_score=92,
                invoice_status="paid_after_first_reminder",
                on_time_ratio=0.88,
                discount_eligible=True,
                flags=["loyal_b2b_partner"],
            ),
            amount_due_paise=58_000_00,
            recovered_paise=58_000_00,
            status=RecoveryStatus.PAID,
            terminal_reason="paid_after_first_reminder",
        ),
        _case(
            case_id="CASE-VENDOR-5002",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-URBAN-228",
            customer=_customer(
                "VENDOR-5002", "Urban Kicks Franchise", "+919900005002", orders=31, delivered=31
            ),
            signals=_signals(
                due_days_ago=7,
                messages_sent=1,
                calls_made=0,
                no_reply_count=0,
                link_clicks=1,
                paid_probability=0.81,
                recovery_score=84,
                invoice_status="accepted_but_unpaid",
                on_time_ratio=0.86,
                flags=["high_confidence_auto_message"],
            ),
            amount_due_paise=76_500_00,
        ),
        _case(
            case_id="CASE-VENDOR-5003",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-TERRA-331",
            customer=_customer(
                "VENDOR-5003", "Terra Fit Distributors", "+919900005003", orders=7, delivered=7
            ),
            signals=_signals(
                due_days_ago=11,
                messages_sent=2,
                calls_made=0,
                no_reply_count=2,
                paid_probability=0.52,
                recovery_score=51,
                invoice_status="accepted_but_unpaid",
                flags=["needs_call_after_two_no_replies"],
            ),
            amount_due_paise=23_400_00,
        ),
        _case(
            case_id="CASE-VENDOR-5004",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-NOVA-444",
            customer=_customer(
                "VENDOR-5004", "Nova Athleisure LLP", "+919900005004", orders=12, delivered=12
            ),
            signals=_signals(
                due_days_ago=34,
                messages_sent=4,
                calls_made=2,
                no_reply_count=4,
                paid_probability=0.31,
                recovery_score=33,
                invoice_status="callback_missed_twice",
                last_reply="Will ask accounts to clear.",
                last_channel="voice_transcript",
                flags=["broken_callback", "monitor_closely"],
            ),
            amount_due_paise=64_200_00,
        ),
        _case(
            case_id="CASE-CUST-5005",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_ABANDONMENT,
            reference="CART-55005",
            customer=_customer("CUST-5005", "Meera Iyer", "+919900005005", orders=1, delivered=1),
            signals=_signals(
                due_days_ago=0,
                messages_sent=0,
                calls_made=0,
                no_reply_count=0,
                paid_probability=0.59,
                recovery_score=61,
                checkout_stage="upi_app_return_failed",
                flags=["fresh_cart_abandonment"],
            ),
            amount_due_paise=6_499_00,
        ),
        _case(
            case_id="CASE-CUST-5006-PAID",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_FAILURE,
            reference="ORDER-55006",
            customer=_customer(
                "CUST-5006", "Dev Malhotra", "+919900005006", orders=10, delivered=10
            ),
            signals=_signals(
                due_days_ago=0,
                messages_sent=0,
                calls_made=0,
                no_reply_count=0,
                paid_probability=0.97,
                recovery_score=95,
                failure_code="bank_otp_timeout",
                on_time_ratio=0.95,
                flags=["paid_on_retry_without_agent"],
            ),
            amount_due_paise=9_999_00,
            recovered_paise=9_999_00,
            status=RecoveryStatus.PAID,
            terminal_reason="customer_completed_retry",
        ),
        _case(
            case_id="CASE-VENDOR-5007",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-ELITE-707",
            customer=_customer(
                "VENDOR-5007", "Elite Teamwear Co", "+919900005007", orders=4, delivered=4
            ),
            signals=_signals(
                due_days_ago=16,
                messages_sent=3,
                calls_made=0,
                no_reply_count=1,
                paid_probability=0.56,
                recovery_score=58,
                invoice_status="promise_to_pay_open",
                last_reply="We can clear half this Friday.",
                last_channel="whatsapp",
                avg_reply_hours=11,
                discount_eligible=True,
                flags=["partial_payment_likely"],
            ),
            amount_due_paise=41_000_00,
        ),
        _case(
            case_id="CASE-VENDOR-5008",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-PEAK-808",
            customer=_customer(
                "VENDOR-5008", "Peak Performance Traders", "+919900005008", orders=21, delivered=21
            ),
            signals=_signals(
                due_days_ago=6,
                messages_sent=1,
                calls_made=0,
                no_reply_count=1,
                paid_probability=0.78,
                recovery_score=80,
                invoice_status="accepted_but_unpaid",
                on_time_ratio=0.82,
                flags=["good_history_first_no_reply"],
            ),
            amount_due_paise=89_900_00,
        ),
        _case(
            case_id="CASE-SUB-5009",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.SUBSCRIPTION_FAILURE,
            reference="PLAN-PRO-MONTHLY",
            subscription_id="sub_demo_5009",
            customer=_customer(
                "VENDOR-5009", "Motion Metrics Pvt Ltd", "+919900005009", orders=3, delivered=3
            ),
            signals=_signals(
                due_days_ago=9,
                messages_sent=2,
                calls_made=1,
                no_reply_count=1,
                paid_probability=0.46,
                recovery_score=47,
                failure_code="mandate_authentication_required",
                mandate_status="halted",
                flags=["subscription_halted", "service_risk"],
            ),
            amount_due_paise=12_500_00,
        ),
        _case(
            case_id="CASE-VENDOR-5010",
            party_type=PartyType.B2B_VENDOR,
            event_type=RevenueEventType.OVERDUE_INVOICE,
            reference="INV-RAJ-SUPPLY-910",
            customer=_customer(
                "VENDOR-5010", "Raj Supply Chain", "+919900005010", orders=15, delivered=15
            ),
            signals=_signals(
                due_days_ago=62,
                messages_sent=6,
                calls_made=3,
                no_reply_count=8,
                paid_probability=0.12,
                recovery_score=14,
                invoice_status="legal_notice_candidate",
                flags=["very_overdue", "high_risk_probability", "manager_review"],
            ),
            amount_due_paise=1_10_000_00,
        ),
        _case(
            case_id="CASE-CUST-5011",
            party_type=PartyType.B2C_CUSTOMER,
            event_type=RevenueEventType.CHECKOUT_FAILURE,
            reference="ORDER-55111",
            customer=_customer(
                "CUST-5011",
                "Farah Khan",
                "+919900005011",
                preferred_channel="email",
                orders=3,
                delivered=3,
            ),
            signals=_signals(
                due_days_ago=1,
                messages_sent=1,
                calls_made=0,
                no_reply_count=0,
                paid_probability=0.71,
                recovery_score=73,
                failure_code="card_declined_soft",
                checkout_stage="card_authorization",
                last_reply="Can you email the link?",
                last_channel="whatsapp",
                avg_reply_hours=2,
                flags=["requested_email_link"],
            ),
            amount_due_paise=4_250_00,
        ),
    ]


def seed_portfolio(
    cases: CaseRepository,
    audit: AuditRepository,
    now: datetime,
    *,
    reset: bool = False,
) -> list[RecoveryCase]:
    if reset:
        cases.delete_all()
    existing = cases.list_all()
    if existing:
        return existing

    portfolio = seeded_cases()
    for case in portfolio:
        cases.save(case, now)
        event_type = "case_paid" if case.status is RecoveryStatus.PAID else "case_detected"
        audit.append(
            case_id=case.case_id,
            event_type=event_type,
            actor_type="system",
            actor_name="synthetic_event_router",
            occurred_at_wall=now,
            occurred_at_sim=now,
            to_status=case.status.value,
            payload={
                "source": case.source,
                "customer_id": case.customer.customer_id,
                "external_reference": case.external_reference,
                "signals": case.signals.model_dump(mode="json"),
            },
        )
    return portfolio
