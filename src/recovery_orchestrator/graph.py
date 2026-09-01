"""LangGraph workflow for a bounded recovery decision cycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from recovery_orchestrator.agents.base import RecoveryAgents
from recovery_orchestrator.clock import SimClock
from recovery_orchestrator.db.repositories import (
    AuditRepository,
    BudgetRepository,
    CaseRepository,
)
from recovery_orchestrator.executors.simulator import SeededSimulatorExecutor
from recovery_orchestrator.models import (
    Diagnosis,
    GateDecision,
    GateResult,
    ProposedAction,
    RecoveryCase,
    RecoveryStatus,
)
from recovery_orchestrator.policy.engine import PolicyConfig, evaluate


class RecoveryGraphState(TypedDict, total=False):
    case: dict[str, Any]
    diagnosis: dict[str, Any]
    proposed_action: dict[str, Any]
    gate_result: dict[str, Any]
    execution: dict[str, Any]


@dataclass(frozen=True)
class WorkflowDependencies:
    agents: RecoveryAgents
    policy: PolicyConfig
    clock: SimClock
    cases: CaseRepository
    audit: AuditRepository
    budgets: BudgetRepository
    executor: SeededSimulatorExecutor


def _validated_case(state: RecoveryGraphState) -> RecoveryCase:
    return RecoveryCase.model_validate(state["case"])


def _replace_case(case: RecoveryCase, **changes: object) -> RecoveryCase:
    values = case.model_dump()
    values.update(changes)
    return RecoveryCase.model_validate(values)


def build_recovery_graph(
    dependencies: WorkflowDependencies,
    *,
    checkpointer: InMemorySaver | None = None,
):
    def diagnose_node(state: RecoveryGraphState) -> RecoveryGraphState:
        case = _validated_case(state)
        diagnosis = dependencies.agents.diagnose(case)
        updated = _replace_case(
            case,
            status=RecoveryStatus.DIAGNOSED,
            root_cause=diagnosis.root_cause,
            intent_score=diagnosis.intent_score,
            llm_call_count=case.llm_call_count + 1,
            transition_count=case.transition_count + 1,
        )
        dependencies.cases.save(updated, dependencies.clock.now())
        dependencies.audit.append(
            case_id=case.case_id,
            event_type="diagnosis_completed",
            actor_type="agent",
            actor_name="diagnostician",
            occurred_at_wall=dependencies.clock.now(),
            occurred_at_sim=dependencies.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            payload=diagnosis.model_dump(mode="json"),
        )
        return {
            "case": updated.model_dump(mode="json"),
            "diagnosis": diagnosis.model_dump(mode="json"),
        }

    def strategy_node(state: RecoveryGraphState) -> RecoveryGraphState:
        case = _validated_case(state)
        diagnosis = Diagnosis.model_validate(state["diagnosis"])
        action = dependencies.agents.propose(case, diagnosis)
        updated = _replace_case(
            case,
            status=RecoveryStatus.ACTION_PROPOSED,
            strategy_attempt_count=case.strategy_attempt_count + 1,
            llm_call_count=case.llm_call_count + 1,
            transition_count=case.transition_count + 1,
        )
        dependencies.cases.save(updated, dependencies.clock.now())
        dependencies.audit.append(
            case_id=case.case_id,
            event_type="action_proposed",
            actor_type="agent",
            actor_name="recovery_strategist",
            occurred_at_wall=dependencies.clock.now(),
            occurred_at_sim=dependencies.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            action_id=action.action_id,
            payload=action.model_dump(mode="json"),
        )
        return {
            "case": updated.model_dump(mode="json"),
            "proposed_action": action.model_dump(mode="json"),
        }

    def gate_node(state: RecoveryGraphState) -> RecoveryGraphState:
        case = _validated_case(state)
        action = ProposedAction.model_validate(state["proposed_action"])
        result = evaluate(
            action,
            case,
            dependencies.budgets.snapshot(),
            dependencies.clock.now(),
            dependencies.policy,
        )
        changes: dict[str, object] = {"transition_count": case.transition_count + 1}
        if result.decision is GateDecision.APPROVED:
            changes["status"] = RecoveryStatus.ACTION_GATED
        elif result.decision is GateDecision.ESCALATE:
            changes.update(
                status=RecoveryStatus.AWAITING_HUMAN,
                awaiting_human_reason=result.reasons[0],
            )
        elif result.decision is GateDecision.DEFERRED:
            changes.update(
                status=RecoveryStatus.AWAITING_RESPONSE,
                wake_at=result.retry_at,
                wake_reason=result.reasons[0],
            )
        else:
            changes.update(
                status=RecoveryStatus.STOPPED,
                terminal_reason=result.reasons[0],
            )
        updated = _replace_case(case, **changes)
        dependencies.cases.save(updated, dependencies.clock.now())
        dependencies.audit.append(
            case_id=case.case_id,
            event_type="policy_evaluated",
            actor_type="system",
            actor_name="deterministic_policy_gate",
            occurred_at_wall=dependencies.clock.now(),
            occurred_at_sim=dependencies.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            decision=result.decision.value,
            reason_codes=result.reasons,
            action_id=action.action_id,
            payload=result.model_dump(mode="json"),
        )
        return {
            "case": updated.model_dump(mode="json"),
            "gate_result": result.model_dump(mode="json"),
        }

    def route_after_gate(state: RecoveryGraphState) -> str:
        result = GateResult.model_validate(state["gate_result"])
        return "execute" if result.decision is GateDecision.APPROVED else "end"

    def execute_node(state: RecoveryGraphState) -> RecoveryGraphState:
        case = _validated_case(state)
        action = ProposedAction.model_validate(state["proposed_action"])
        result = dependencies.executor.execute(case, action, dependencies.clock.now())
        updated = _replace_case(
            result.case,
            transition_count=result.case.transition_count + 1,
        )
        dependencies.cases.save(updated, dependencies.clock.now())
        dependencies.audit.append(
            case_id=case.case_id,
            event_type="action_executed",
            actor_type="tool",
            actor_name="seeded_simulator",
            occurred_at_wall=dependencies.clock.now(),
            occurred_at_sim=dependencies.clock.now(),
            from_status=case.status.value,
            to_status=updated.status.value,
            action_id=action.action_id,
            external_event_id=result.external_event_id,
            payload={"outcome": result.outcome, "response": result.response},
        )
        return {
            "case": updated.model_dump(mode="json"),
            "execution": {
                "outcome": result.outcome,
                "external_event_id": result.external_event_id,
                "response": result.response,
            },
        }

    builder = StateGraph(RecoveryGraphState)
    builder.add_node("diagnose", diagnose_node)
    builder.add_node("strategize", strategy_node)
    builder.add_node("gate", gate_node)
    builder.add_node("execute", execute_node)
    builder.add_edge(START, "diagnose")
    builder.add_edge("diagnose", "strategize")
    builder.add_edge("strategize", "gate")
    builder.add_conditional_edges(
        "gate",
        route_after_gate,
        {"execute": "execute", "end": END},
    )
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
