"""Gemini Interactions API implementation with schema-validated agent outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from recovery_orchestrator.models import (
    ActionType,
    Diagnosis,
    ProposedAction,
    RecoveryCase,
    ReplyInterpretation,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


class InteractionsResource(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class StrategyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    rationale: str = Field(min_length=1, max_length=500)
    amount_paise: int | None = Field(default=None, gt=0)
    discount_pct: int | None = Field(default=None, ge=0, le=100)
    follow_up_hours: int | None = Field(default=None, ge=1, le=168)


DIAGNOSIS_INSTRUCTIONS = """You are the Diagnostician in a bounded revenue-recovery system.
Infer only from the supplied case. Return a conservative root cause, customer intent score,
confidence, and short evidence. Never propose or execute an action. Uncertainty is acceptable."""

STRATEGY_INSTRUCTIONS = """You are the Recovery Strategist. Choose exactly one next action
from the schema using the case and diagnosis. You may propose but cannot approve or execute.
Do not exceed the outstanding balance. Prefer the least intrusive effective action. Disputes,
opt-outs, ambiguity, and high-risk situations should be escalated or stopped."""

REPLY_INSTRUCTIONS = """You are the Reply Interpreter. Extract customer intent, an explicit
promise-to-pay datetime, an amount they can pay now in paise, and payment-plan wording when
present. Use one precise intent: will_pay, already_paid, wrong_person, needs_callback, dispute,
opt_out, cannot_pay, needs_plan, technical_issue, or unclear. A claim of payment is evidence to
verify, never proof that money moved. Do not invent missing facts. Datetimes must include a
timezone offset. You cannot approve discounts, move money, or send messages."""


@dataclass(frozen=True)
class GeminiRecoveryAgents:
    interactions: InteractionsResource
    model: str = "gemini-3.5-flash"
    thinking_level: str = "low"
    _client: Any | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        model: str = "gemini-3.5-flash",
        thinking_level: str = "low",
    ) -> GeminiRecoveryAgents:
        from google import genai

        client = genai.Client(api_key=api_key)
        return cls(
            interactions=client.interactions,
            model=model,
            thinking_level=thinking_level,
            _client=client,
        )

    def _parse(
        self,
        *,
        instructions: str,
        input_payload: dict[str, Any],
        output_type: type[OutputT],
    ) -> OutputT:
        input_json = json.dumps(input_payload, sort_keys=True, default=str)
        try:
            interaction = self.interactions.create(
                model=self.model,
                input=f"{instructions}\n\nINPUT JSON:\n{input_json}",
                generation_config={"thinking_level": self.thinking_level},
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": output_type.model_json_schema(),
                },
            )
        except Exception as exc:
            safe_message = " ".join(str(exc).split())[:500]
            raise RuntimeError(f"{type(exc).__name__}: {safe_message}") from exc
        output_text = getattr(interaction, "output_text", None)
        if not output_text:
            raise RuntimeError("Gemini response did not contain structured output text")
        return output_type.model_validate_json(output_text)

    def diagnose(self, case: RecoveryCase) -> Diagnosis:
        return self._parse(
            instructions=DIAGNOSIS_INSTRUCTIONS,
            input_payload={"case": case.model_dump(mode="json")},
            output_type=Diagnosis,
        )

    def propose(self, case: RecoveryCase, diagnosis: Diagnosis) -> ProposedAction:
        decision = self._parse(
            instructions=STRATEGY_INSTRUCTIONS,
            input_payload={
                "case": case.model_dump(mode="json"),
                "diagnosis": diagnosis.model_dump(mode="json"),
            },
            output_type=StrategyDecision,
        )
        params = {
            key: value
            for key, value in {
                "amount_paise": decision.amount_paise,
                "discount_pct": decision.discount_pct,
                "follow_up_hours": decision.follow_up_hours,
            }.items()
            if value is not None
        }
        return ProposedAction(
            action_id=f"{case.case_id}:strategy:{case.strategy_attempt_count + 1}",
            type=decision.action_type,
            params=params,
            rationale=decision.rationale,
        )

    def interpret_reply(self, case: RecoveryCase, message: str) -> ReplyInterpretation:
        return self._parse(
            instructions=REPLY_INSTRUCTIONS,
            input_payload={"case": case.model_dump(mode="json"), "customer_message": message},
            output_type=ReplyInterpretation,
        )
