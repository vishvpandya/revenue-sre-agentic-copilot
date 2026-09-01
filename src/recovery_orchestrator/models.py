"""Typed contracts shared by agents, policy, persistence, and API boundaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionMode(StrEnum):
    LIVE_TEST = "live_test"
    SEEDED_SIMULATION = "seeded_simulation"


class PartyType(StrEnum):
    B2C_CUSTOMER = "b2c_customer"
    B2B_VENDOR = "b2b_vendor"


class RevenueEventType(StrEnum):
    CHECKOUT_FAILURE = "checkout_failure"
    CHECKOUT_ABANDONMENT = "checkout_abandonment"
    OVERDUE_INVOICE = "overdue_invoice"
    SUBSCRIPTION_FAILURE = "subscription_failure"


class RecoveryStatus(StrEnum):
    DETECTED = "detected"
    DIAGNOSED = "diagnosed"
    ACTION_PROPOSED = "action_proposed"
    ACTION_GATED = "action_gated"
    EXECUTING = "executing"
    AWAITING_PAYMENT = "awaiting_payment"
    AWAITING_RESPONSE = "awaiting_response"
    PTP_RECORDED = "ptp_recorded"
    PARTIALLY_PAID = "partially_paid"
    AWAITING_HUMAN = "awaiting_human"
    PAID = "paid"
    STOPPED = "stopped"
    ESCALATED = "escalated"
    FAILED = "failed"


class RootCause(StrEnum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TECHNICAL = "technical"
    LOW_INTENT = "low_intent"
    DISPUTE = "dispute"
    FORGOT = "forgot"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    SEND_PAYMENT_LINK = "send_payment_link"
    SEND_REMINDER = "send_reminder"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    OFFER_DISCOUNT_LINK = "offer_discount_link"
    OFFER_PAYMENT_PLAN = "offer_payment_plan"
    ESCALATE = "escalate"
    STOP = "stop"


class GateDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATE = "escalate"
    DEFERRED = "deferred"


class ActionExecutionStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def require_timezone_aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class CustomerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")
    preferred_channel: str = Field(default="whatsapp", pattern=r"^(whatsapp|voice|email)$")
    timezone: str = "Asia/Kolkata"
    orders: int = Field(default=0, ge=0)
    delivered: int = Field(default=0, ge=0)
    rto: int = Field(default=0, ge=0)
    prior_ptp_kept_ratio: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_timezone(self) -> CustomerProfile:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown customer timezone: {self.timezone}") from exc
        return self


class CaseSignals(BaseModel):
    """Merchant/gateway evidence visible to agents but not invented by them."""

    model_config = ConfigDict(extra="forbid")

    days_overdue: int = Field(default=0, ge=0)
    due_at: datetime | None = None
    first_contact_at: datetime | None = None
    last_message_at: datetime | None = None
    reply_due_at: datetime | None = None
    last_link_click_at: datetime | None = None
    payment_failure_code: str | None = Field(default=None, max_length=100)
    checkout_stage: str | None = Field(default=None, max_length=100)
    invoice_status: str | None = Field(default=None, max_length=100)
    mandate_status: str | None = Field(default=None, max_length=100)
    last_customer_reply: str | None = Field(default=None, max_length=2000)
    last_inbound_channel: str | None = Field(default=None, max_length=50)
    stated_affordable_paise: int | None = Field(default=None, ge=0)
    messages_sent: int = Field(default=0, ge=0)
    calls_made: int = Field(default=0, ge=0)
    no_reply_count: int = Field(default=0, ge=0)
    link_click_count: int = Field(default=0, ge=0)
    historical_on_time_payment_ratio: float = Field(default=0.5, ge=0, le=1)
    average_reply_delay_hours: float | None = Field(default=None, ge=0)
    predicted_payment_probability: float = Field(default=0.5, ge=0, le=1)
    recovery_score: int = Field(default=50, ge=0, le=100)
    discount_eligible: bool = False
    risk_flags: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_signal_dates(self) -> CaseSignals:
        require_timezone_aware(self.due_at, "due_at")
        require_timezone_aware(self.first_contact_at, "first_contact_at")
        require_timezone_aware(self.last_message_at, "last_message_at")
        require_timezone_aware(self.reply_due_at, "reply_due_at")
        require_timezone_aware(self.last_link_click_at, "last_link_click_at")
        return self


class PaymentInstallment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_paise: int = Field(gt=0)
    due_at: datetime

    @model_validator(mode="after")
    def validate_due_at(self) -> PaymentInstallment:
        require_timezone_aware(self.due_at, "due_at")
        return self


class PaymentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collectible_amount_paise: int = Field(gt=0)
    upfront_amount_paise: int = Field(gt=0)
    installments: list[PaymentInstallment] = Field(min_length=1)
    discount_paise: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_plan_math(self) -> PaymentPlan:
        if self.upfront_amount_paise != self.installments[0].amount_paise:
            raise ValueError("upfront amount must equal the first installment")
        if sum(item.amount_paise for item in self.installments) != self.collectible_amount_paise:
            raise ValueError("installments must sum to collectible amount")
        dates = [item.due_at for item in self.installments]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("installment dates must be strictly increasing")
        return self


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=100)
    type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=500)


class RecoveryCase(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    case_id: str = Field(min_length=1, max_length=100)
    merchant_id: str = Field(default="merchant-demo", min_length=1, max_length=100)
    party_type: PartyType = PartyType.B2C_CUSTOMER
    source: str = Field(default="synthetic_merchant_event", min_length=1, max_length=100)
    external_reference: str | None = Field(default=None, max_length=100)
    subscription_id: str | None = Field(default=None, max_length=100)
    signals: CaseSignals = Field(default_factory=CaseSignals)
    execution_mode: ExecutionMode
    event_type: RevenueEventType
    status: RecoveryStatus = RecoveryStatus.DETECTED
    customer: CustomerProfile
    amount_due_paise: int = Field(gt=0)
    amount_recovered_paise: int = Field(default=0, ge=0)
    outstanding_balance_paise: int | None = Field(default=None, ge=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    root_cause: RootCause | None = None
    intent_score: float | None = Field(default=None, ge=0, le=1)
    contact_count: int = Field(default=0, ge=0)
    last_contact_at: datetime | None = None
    strategy_attempt_count: int = Field(default=0, ge=0)
    llm_call_count: int = Field(default=0, ge=0)
    transition_count: int = Field(default=0, ge=0)
    discount_reserved_paise: int = Field(default=0, ge=0)
    discount_spent_paise: int = Field(default=0, ge=0)
    ptp_at: datetime | None = None
    payment_plan: PaymentPlan | None = None
    partial_payment_count: int = Field(default=0, ge=0)
    wake_at: datetime | None = None
    wake_reason: str | None = Field(default=None, max_length=100)
    payment_link_id: str | None = Field(default=None, max_length=100)
    payment_id: str | None = Field(default=None, max_length=100)
    awaiting_human_reason: str | None = Field(default=None, max_length=500)
    terminal_reason: str | None = Field(default=None, max_length=500)
    has_dispute: bool = False
    has_opted_out: bool = False

    @model_validator(mode="after")
    def validate_case(self) -> RecoveryCase:
        require_timezone_aware(self.last_contact_at, "last_contact_at")
        require_timezone_aware(self.ptp_at, "ptp_at")
        require_timezone_aware(self.wake_at, "wake_at")
        expected_outstanding = self.amount_due_paise - self.amount_recovered_paise
        if expected_outstanding < 0:
            raise ValueError("recovered amount cannot exceed amount due")
        if self.outstanding_balance_paise is None:
            object.__setattr__(self, "outstanding_balance_paise", expected_outstanding)
        elif self.outstanding_balance_paise != expected_outstanding:
            raise ValueError("outstanding balance must equal due minus recovered")
        return self


class BatchBudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discount_cap_paise: int = Field(ge=0)
    discount_reserved_paise: int = Field(default=0, ge=0)
    discount_spent_paise: int = Field(default=0, ge=0)

    @property
    def remaining_paise(self) -> int:
        return max(
            0,
            self.discount_cap_paise - self.discount_reserved_paise - self.discount_spent_paise,
        )


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: GateDecision
    reasons: list[str] = Field(min_length=1)
    retry_at: datetime | None = None
    discount_required_paise: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_retry_at(self) -> GateResult:
        require_timezone_aware(self.retry_at, "retry_at")
        if self.decision is GateDecision.DEFERRED and self.retry_at is None:
            raise ValueError("deferred decisions require retry_at")
        return self


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: RootCause
    intent_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=5)
    reasoning: str = Field(min_length=1, max_length=500)


class ReplyInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=50)
    ptp_at: datetime | None = None
    can_pay_now_paise: int | None = Field(default=None, ge=0)
    requested_plan_text: str | None = Field(default=None, max_length=500)
    reasoning: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_interpreted_date(self) -> ReplyInterpretation:
        require_timezone_aware(self.ptp_at, "ptp_at")
        return self


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case: RecoveryCase
    outcome: str = Field(min_length=1, max_length=100)
    external_event_id: str | None = Field(default=None, max_length=200)
    response: dict[str, Any] = Field(default_factory=dict)
