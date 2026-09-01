"""Pure deterministic policy gate. This module must never call an LLM or perform I/O."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recovery_orchestrator.models import (
    ActionType,
    BatchBudgetSnapshot,
    GateDecision,
    GateResult,
    PaymentPlan,
    ProposedAction,
    RecoveryCase,
    RecoveryStatus,
)


class QuietHoursConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str


class ContactPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_contacts_per_case: int = Field(gt=0)
    cooldown_hours: int = Field(ge=0)
    quiet_hours_local: QuietHoursConfig


class DiscountPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve_max_pct: Decimal = Field(ge=0, le=100)
    hard_max_pct: Decimal = Field(ge=0, le=100)


class PaymentPlanPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    min_upfront_pct: Decimal = Field(gt=0, le=100)
    max_installments: int = Field(gt=0)
    max_duration_days: int = Field(gt=0)
    require_human_above_inr: int = Field(ge=0)


class EscalationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory_review_above_inr: int = Field(ge=0)
    review_high_value_customer_facing_actions: bool
    escalate_on_dispute: bool
    escalate_on_optout: bool


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_discount_cap_inr: int = Field(ge=0)


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_strategy_attempts_per_cycle: int = Field(gt=0)
    max_llm_calls_per_case: int = Field(gt=0)
    max_transitions_per_case: int = Field(gt=0)


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact: ContactPolicy
    discount: DiscountPolicy
    payment_plan: PaymentPlanPolicy
    escalation: EscalationPolicy
    budget: BudgetPolicy
    agent: AgentPolicy
    kill_switch: bool


CONTACT_ACTIONS = {
    ActionType.SEND_PAYMENT_LINK,
    ActionType.SEND_REMINDER,
    ActionType.OFFER_DISCOUNT_LINK,
    ActionType.OFFER_PAYMENT_PLAN,
}


def load_policy_config(path: str | Path) -> PolicyConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return PolicyConfig.model_validate(raw)


def _parse_clock_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid policy time: {value}") from exc
    if parsed.tzinfo is not None:
        raise ValueError("quiet-hour times must not include a timezone")
    return parsed


def _quiet_hours_retry_at(
    now: datetime,
    timezone_name: str,
    quiet_hours: QuietHoursConfig,
) -> datetime | None:
    local_tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(local_tz)
    start = _parse_clock_time(quiet_hours.start)
    end = _parse_clock_time(quiet_hours.end)
    current = local_now.time().replace(tzinfo=None)

    if start == end:
        return None

    crosses_midnight = start > end
    is_quiet = (current >= start or current < end) if crosses_midnight else start <= current < end
    if not is_quiet:
        return None

    retry_date: date
    if crosses_midnight and current >= start:
        retry_date = local_now.date() + timedelta(days=1)
    else:
        retry_date = local_now.date()
    retry_local = datetime.combine(retry_date, end, tzinfo=local_tz)
    return retry_local.astimezone(UTC)


def _discount_required_paise(action: ProposedAction, case: RecoveryCase) -> tuple[Decimal, int]:
    raw_pct = action.params.get("discount_pct", 0)
    try:
        pct = Decimal(str(raw_pct))
    except InvalidOperation as exc:
        raise ValueError("discount_pct must be numeric") from exc
    if pct < 0:
        raise ValueError("discount_pct cannot be negative")
    amount = (Decimal(case.amount_due_paise) * pct / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return pct, int(amount)


def _reject(reason: str) -> GateResult:
    return GateResult(decision=GateDecision.REJECTED, reasons=[reason])


def _escalate(reason: str, discount_required_paise: int = 0) -> GateResult:
    return GateResult(
        decision=GateDecision.ESCALATE,
        reasons=[reason],
        discount_required_paise=discount_required_paise,
    )


def evaluate(
    action: ProposedAction,
    case: RecoveryCase,
    budget: BatchBudgetSnapshot,
    now: datetime,
    config: PolicyConfig,
) -> GateResult:
    """Return a deterministic decision for the supplied immutable inputs."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)

    if case.status is RecoveryStatus.PAID:
        return _reject("case_already_paid")
    if config.kill_switch:
        return _reject("kill_switch")
    if case.has_dispute and config.escalation.escalate_on_dispute:
        return _escalate("customer_dispute")
    if case.has_opted_out and config.escalation.escalate_on_optout:
        return _escalate("customer_opt_out")
    if case.strategy_attempt_count >= config.agent.max_strategy_attempts_per_cycle:
        return _escalate("strategy_attempt_limit")
    if case.llm_call_count >= config.agent.max_llm_calls_per_case:
        return _escalate("llm_call_limit")
    if case.transition_count >= config.agent.max_transitions_per_case:
        return _reject("transition_limit")

    if action.type in CONTACT_ACTIONS:
        if case.contact_count >= config.contact.max_contacts_per_case:
            return _reject("max_contacts")
        quiet_retry = _quiet_hours_retry_at(
            now,
            case.customer.timezone,
            config.contact.quiet_hours_local,
        )
        if quiet_retry is not None:
            return GateResult(
                decision=GateDecision.DEFERRED,
                reasons=["quiet_hours"],
                retry_at=quiet_retry,
            )
        if case.last_contact_at is not None:
            cooldown_retry = case.last_contact_at.astimezone(UTC) + timedelta(
                hours=config.contact.cooldown_hours
            )
            if cooldown_retry > now:
                return GateResult(
                    decision=GateDecision.DEFERRED,
                    reasons=["contact_cooldown"],
                    retry_at=cooldown_retry,
                )

    plan: PaymentPlan | None = None
    if action.type is ActionType.OFFER_PAYMENT_PLAN:
        if not config.payment_plan.enabled:
            return _reject("payment_plans_disabled")
        try:
            plan = PaymentPlan.model_validate(action.params.get("plan"))
        except ValidationError:
            return _reject("invalid_payment_plan")
        if plan.collectible_amount_paise != case.outstanding_balance_paise:
            return _reject("plan_does_not_match_outstanding_balance")
        upfront_pct = (
            Decimal(plan.upfront_amount_paise) * 100 / Decimal(plan.collectible_amount_paise)
        )
        if upfront_pct < config.payment_plan.min_upfront_pct:
            return _reject("plan_upfront_below_minimum")
        if len(plan.installments) > config.payment_plan.max_installments:
            return _reject("plan_too_many_installments")
        if plan.installments[-1].due_at > now + timedelta(
            days=config.payment_plan.max_duration_days
        ):
            return _reject("plan_duration_too_long")

    high_value_threshold_paise = config.escalation.mandatory_review_above_inr * 100
    if (
        action.type in CONTACT_ACTIONS
        and config.escalation.review_high_value_customer_facing_actions
        and case.amount_due_paise > high_value_threshold_paise
    ):
        return _escalate("high_value_customer_facing_action")
    if (
        plan is not None
        and case.amount_due_paise > config.payment_plan.require_human_above_inr * 100
    ):
        return _escalate("high_value_payment_plan")

    try:
        discount_pct, discount_paise = _discount_required_paise(action, case)
    except ValueError:
        return _reject("invalid_discount")
    if plan is not None:
        discount_paise = max(discount_paise, plan.discount_paise)
        if plan.discount_paise:
            discount_pct = Decimal(plan.discount_paise) * 100 / Decimal(case.amount_due_paise)

    if discount_pct > config.discount.hard_max_pct:
        return _reject("discount_above_hard_max")
    if discount_pct > config.discount.auto_approve_max_pct:
        return _escalate("discount_above_auto_approve", discount_paise)
    if discount_paise > budget.remaining_paise:
        return _escalate("batch_discount_budget_exceeded", discount_paise)

    return GateResult(
        decision=GateDecision.APPROVED,
        reasons=["all_policy_checks_passed"],
        discount_required_paise=discount_paise,
    )
