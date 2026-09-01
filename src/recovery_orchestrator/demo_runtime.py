"""Application service used by FastAPI to run the judge-visible demo."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from recovery_orchestrator.agents.base import RecoveryAgents
from recovery_orchestrator.agents.fake import FakeRecoveryAgents
from recovery_orchestrator.agents.gemini_agents import GeminiRecoveryAgents
from recovery_orchestrator.clock import SimClock
from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.db.repositories import (
    ActionLedger,
    AuditRepository,
    BudgetRepository,
    CaseRepository,
)
from recovery_orchestrator.demo_data import seed_portfolio
from recovery_orchestrator.executors.simulator import SeededSimulatorExecutor
from recovery_orchestrator.graph import WorkflowDependencies, build_recovery_graph
from recovery_orchestrator.models import ProposedAction, RecoveryCase, RecoveryStatus
from recovery_orchestrator.policy.engine import load_policy_config
from recovery_orchestrator.portfolio import (
    answer_portfolio_question,
    portfolio_bucket,
    portfolio_scan,
)
from recovery_orchestrator.settings import Settings

DEMO_START = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def _replace_case(case: RecoveryCase, **changes: object) -> RecoveryCase:
    values = case.model_dump()
    values.update(changes)
    values["outstanding_balance_paise"] = int(values["amount_due_paise"]) - int(
        values["amount_recovered_paise"]
    )
    return RecoveryCase.model_validate(values)


@dataclass
class DemoRuntime:
    settings: Settings
    database: Database
    clock: SimClock
    cases: CaseRepository
    audit: AuditRepository
    budgets: BudgetRepository
    ledger: ActionLedger

    @classmethod
    def create(cls, settings: Settings) -> DemoRuntime:
        database = Database(settings.database_path)
        database.initialize()
        runtime = cls(
            settings=settings,
            database=database,
            clock=SimClock(DEMO_START),
            cases=CaseRepository(database),
            audit=AuditRepository(database),
            budgets=BudgetRepository(database),
            ledger=ActionLedger(database),
        )
        policy = load_policy_config("config/policy.yaml")
        runtime.budgets.initialize(policy.budget.batch_discount_cap_inr * 100, runtime.clock.now())
        seed_portfolio(runtime.cases, runtime.audit, runtime.clock.now())
        return runtime

    @property
    def gemini_configured(self) -> bool:
        return self.settings.gemini_api_key is not None

    def _agents(self, live_ai: bool) -> RecoveryAgents:
        if live_ai:
            if self.settings.gemini_api_key is None:
                raise ValueError("GEMINI_API_KEY is not configured")
            return GeminiRecoveryAgents.from_api_key(
                self.settings.gemini_api_key.get_secret_value(),
                model=self.settings.gemini_model,
                thinking_level=self.settings.gemini_thinking_level,
            )
        return FakeRecoveryAgents()

    def reset(self) -> list[RecoveryCase]:
        self.clock = SimClock(DEMO_START)
        return seed_portfolio(
            self.cases,
            self.audit,
            self.clock.now(),
            reset=True,
        )

    def list_cases(
        self,
        query: str = "",
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        bucket: str = "all",
    ) -> list[RecoveryCase]:
        cases = self.cases.list_all()
        needle = query.strip().lower()
        filtered_cases = cases
        if bucket != "all":
            filtered_cases = [case for case in filtered_cases if portfolio_bucket(case) == bucket]
        if date_from is not None:
            filtered_cases = [
                case
                for case in filtered_cases
                if case.signals.due_at is None or case.signals.due_at >= date_from
            ]
        if date_to is not None:
            filtered_cases = [
                case
                for case in filtered_cases
                if case.signals.due_at is None or case.signals.due_at <= date_to
            ]
        if not needle:
            return filtered_cases
        return [
            case
            for case in filtered_cases
            if needle
            in " ".join(
                filter(
                    None,
                    [
                        case.case_id,
                        case.customer.customer_id,
                        case.customer.name,
                        case.external_reference,
                        case.subscription_id,
                    ],
                )
            ).lower()
        ]

    def scan_portfolio(self) -> dict[str, object]:
        return portfolio_scan(self.cases.list_all(), self.clock.now())

    def ask_copilot(self, question: str) -> dict[str, object]:
        if not question.strip():
            raise ValueError("question cannot be empty")
        return answer_portfolio_question(question, self.cases.list_all(), self.clock.now())

    def detail(self, case_id: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        return {
            "case": case.model_dump(mode="json"),
            "audit": [asdict(event) for event in self.audit.list_for_case(case_id)],
            "audit_chain_valid": self.audit.verify_chain(case_id),
        }

    def run_case(self, case_id: str, *, live_ai: bool) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        if case.status is RecoveryStatus.PAID:
            return {
                "mode": "NO-OP · ALREADY PAID",
                "trace": {
                    "gate_result": {
                        "decision": "rejected",
                        "reasons": ["case_already_paid"],
                    }
                },
                **self.detail(case_id),
            }
        agents = self._agents(live_ai)
        policy = load_policy_config("config/policy.yaml")
        dependencies = WorkflowDependencies(
            agents=agents,
            policy=policy,
            clock=self.clock,
            cases=self.cases,
            audit=self.audit,
            budgets=self.budgets,
            executor=SeededSimulatorExecutor(self.ledger),
        )
        graph = build_recovery_graph(dependencies)
        result = graph.invoke(
            {"case": case.model_dump(mode="json")},
            config={"configurable": {"thread_id": f"{case.case_id}-{case.transition_count}"}},
        )
        return {
            "mode": "LIVE GEMINI" if live_ai else "SEEDED AI SIMULATION",
            "trace": {
                key: result.get(key)
                for key in ("diagnosis", "proposed_action", "gate_result", "execution")
                if result.get(key) is not None
            },
            **self.detail(case_id),
        }

    def interpret_reply(
        self,
        case_id: str,
        message: str,
        *,
        live_ai: bool,
        channel: str = "whatsapp",
    ) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        if not message.strip():
            raise ValueError("customer reply cannot be empty")
        interpretation = self._agents(live_ai).interpret_reply(case, message)
        changes: dict[str, object] = {
            "llm_call_count": case.llm_call_count + 1,
            "transition_count": case.transition_count + 1,
            "signals": case.signals.model_copy(
                update={
                    "last_customer_reply": message,
                    "last_inbound_channel": channel,
                    "stated_affordable_paise": interpretation.can_pay_now_paise,
                }
            ),
        }
        if interpretation.intent == "opt_out":
            changes.update(
                has_opted_out=True,
                status=RecoveryStatus.STOPPED,
                terminal_reason="customer_opt_out",
            )
        elif interpretation.intent == "dispute":
            changes.update(
                has_dispute=True,
                status=RecoveryStatus.AWAITING_HUMAN,
                awaiting_human_reason="customer_dispute",
            )
        elif interpretation.intent == "wrong_person":
            changes.update(
                status=RecoveryStatus.STOPPED,
                terminal_reason="wrong_contact_details",
            )
        elif interpretation.intent == "already_paid":
            changes.update(
                status=RecoveryStatus.AWAITING_HUMAN,
                awaiting_human_reason="customer_claims_paid_requires_verification",
            )
        elif interpretation.intent == "needs_callback":
            callback_at = interpretation.ptp_at or self.clock.now() + timedelta(days=1)
            changes.update(
                status=RecoveryStatus.AWAITING_RESPONSE,
                wake_at=callback_at,
                wake_reason="callback_requested",
            )
        elif interpretation.intent in {"cannot_pay", "needs_plan"}:
            changes.update(
                status=RecoveryStatus.DETECTED,
                root_cause="insufficient_funds",
                wake_at=None,
                wake_reason="replan_for_affordability",
            )
        elif interpretation.intent == "technical_issue":
            changes.update(
                status=RecoveryStatus.DETECTED,
                root_cause="technical",
                wake_at=None,
                wake_reason="replan_for_payment_failure",
            )
        elif interpretation.ptp_at is not None:
            changes.update(
                status=RecoveryStatus.PTP_RECORDED,
                ptp_at=interpretation.ptp_at,
                wake_at=interpretation.ptp_at,
                wake_reason="promise_to_pay_due",
            )
        else:
            changes["status"] = RecoveryStatus.AWAITING_RESPONSE
        updated = _replace_case(case, **changes)
        self.cases.save(updated, self.clock.now())
        self.audit.append(
            case_id=case_id,
            event_type="reply_interpreted",
            actor_type="agent",
            actor_name="reply_interpreter",
            occurred_at_wall=self.clock.now(),
            occurred_at_sim=self.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            payload={
                "customer_message": message,
                "interpretation": interpretation.model_dump(mode="json"),
                "channel": channel,
                "mode": "live_gemini" if live_ai else "seeded_simulation",
            },
        )
        return {
            "mode": "LIVE GEMINI" if live_ai else "SEEDED AI SIMULATION",
            "interpretation": interpretation.model_dump(mode="json"),
            **self.detail(case_id),
        }

    def advance_time(self, hours: int) -> dict[str, Any]:
        now = self.clock.advance(hours=hours)
        woken: list[dict[str, str]] = []
        for case in self.cases.list_all():
            if case.wake_at is None or case.wake_at > now:
                continue
            if case.status is RecoveryStatus.PTP_RECORDED and case.outstanding_balance_paise:
                event_type = "promise_missed"
                wake_reason = "broken_promise_replan"
            elif case.status is RecoveryStatus.AWAITING_RESPONSE:
                event_type = "scheduled_followup_due"
                wake_reason = "scheduled_followup_replan"
            else:
                continue
            updated = _replace_case(
                case,
                status=RecoveryStatus.DETECTED,
                wake_at=None,
                wake_reason=wake_reason,
                transition_count=case.transition_count + 1,
            )
            self.cases.save(updated, now)
            self.audit.append(
                case_id=case.case_id,
                event_type=event_type,
                actor_type="clock",
                actor_name="simulation_scheduler",
                occurred_at_wall=now,
                occurred_at_sim=now,
                from_status=case.status.value,
                to_status=updated.status.value,
                payload={"scheduled_for": case.wake_at.isoformat(), "wake_reason": wake_reason},
            )
            woken.append({"case_id": case.case_id, "event": event_type})
        return {"simulation_time": now.isoformat(), "woken_cases": woken}

    def apply_payment(self, case_id: str, amount_paise: int) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        if amount_paise <= 0 or amount_paise > case.outstanding_balance_paise:
            raise ValueError("payment must be positive and within the outstanding balance")
        recovered = case.amount_recovered_paise + amount_paise
        paid = recovered == case.amount_due_paise
        updated = _replace_case(
            case,
            amount_recovered_paise=recovered,
            partial_payment_count=case.partial_payment_count + (0 if paid else 1),
            status=RecoveryStatus.PAID if paid else RecoveryStatus.PARTIALLY_PAID,
            payment_id=f"sim_pay_{case.case_id}_{case.partial_payment_count + 1}",
            terminal_reason="simulated_payment_captured" if paid else None,
            transition_count=case.transition_count + 1,
        )
        self.cases.save(updated, self.clock.now())
        self.audit.append(
            case_id=case_id,
            event_type="payment_captured",
            actor_type="webhook",
            actor_name="razorpay_test_simulator",
            occurred_at_wall=self.clock.now(),
            occurred_at_sim=self.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            external_event_id=f"sim-payment-{case.case_id}-{updated.partial_payment_count}",
            payload={
                "amount_paid_paise": amount_paise,
                "total_recovered_paise": recovered,
                "remaining_paise": updated.outstanding_balance_paise,
                "simulation": True,
            },
        )
        return self.detail(case_id)

    def human_review(self, case_id: str, *, approve: bool) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        if case.status is not RecoveryStatus.AWAITING_HUMAN:
            raise ValueError("case is not awaiting human review")
        proposed_events = [
            event
            for event in self.audit.list_for_case(case_id)
            if event.event_type == "action_proposed"
        ]
        if not proposed_events:
            raise ValueError("no proposed action is available for review")
        action = ProposedAction.model_validate(proposed_events[-1].payload)

        if not approve:
            updated = _replace_case(
                case,
                status=RecoveryStatus.STOPPED,
                terminal_reason="human_review_denied",
                awaiting_human_reason=None,
                transition_count=case.transition_count + 1,
            )
            self.cases.save(updated, self.clock.now())
            self.audit.append(
                case_id=case_id,
                event_type="human_review_decided",
                actor_type="human",
                actor_name="merchant_operator",
                occurred_at_wall=self.clock.now(),
                occurred_at_sim=self.clock.now(),
                from_status=case.status.value,
                to_status=updated.status.value,
                decision="denied",
                action_id=action.action_id,
                payload={"review_reason": case.awaiting_human_reason},
            )
            return self.detail(case_id)

        gated = _replace_case(
            case,
            status=RecoveryStatus.ACTION_GATED,
            awaiting_human_reason=None,
            transition_count=case.transition_count + 1,
        )
        self.cases.save(gated, self.clock.now())
        self.audit.append(
            case_id=case_id,
            event_type="human_review_decided",
            actor_type="human",
            actor_name="merchant_operator",
            occurred_at_wall=self.clock.now(),
            occurred_at_sim=self.clock.now(),
            from_status=case.status.value,
            to_status=gated.status.value,
            decision="approved",
            action_id=action.action_id,
            payload={"review_reason": case.awaiting_human_reason},
        )
        result = SeededSimulatorExecutor(self.ledger).execute(gated, action, self.clock.now())
        updated = _replace_case(
            result.case,
            transition_count=result.case.transition_count + 1,
        )
        self.cases.save(updated, self.clock.now())
        self.audit.append(
            case_id=case_id,
            event_type="action_executed",
            actor_type="tool",
            actor_name="seeded_simulator_after_human_approval",
            occurred_at_wall=self.clock.now(),
            occurred_at_sim=self.clock.now(),
            from_status=gated.status.value,
            to_status=updated.status.value,
            action_id=action.action_id,
            external_event_id=result.external_event_id,
            payload={"outcome": result.outcome, "response": result.response},
        )
        return self.detail(case_id)

    def metrics(self) -> dict[str, int | float | str]:
        cases = self.cases.list_all()
        at_risk = sum(case.amount_due_paise for case in cases)
        recovered = sum(case.amount_recovered_paise for case in cases)
        buckets = [portfolio_bucket(case) for case in cases]
        return {
            "simulation_time": self.clock.now().isoformat(),
            "cases": len(cases),
            "amount_at_risk_paise": at_risk,
            "amount_recovered_paise": recovered,
            "open_cases": sum(
                case.status not in {RecoveryStatus.PAID, RecoveryStatus.STOPPED} for case in cases
            ),
            "human_review_cases": sum(
                case.status is RecoveryStatus.AWAITING_HUMAN for case in cases
            ),
            "recovery_rate_pct": round(recovered * 100 / at_risk, 2) if at_risk else 0.0,
            "needs_action_cases": buckets.count("needs_action"),
            "watchlist_cases": buckets.count("watchlist"),
            "recovered_cases": buckets.count("recovered"),
            "escalated_cases": buckets.count("escalated"),
        }
