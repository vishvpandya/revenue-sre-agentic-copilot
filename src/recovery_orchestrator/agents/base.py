"""Interfaces keep graph orchestration independent of the LLM provider."""

from __future__ import annotations

from typing import Protocol

from recovery_orchestrator.models import (
    Diagnosis,
    ProposedAction,
    RecoveryCase,
    ReplyInterpretation,
)


class RecoveryAgents(Protocol):
    def diagnose(self, case: RecoveryCase) -> Diagnosis: ...

    def propose(self, case: RecoveryCase, diagnosis: Diagnosis) -> ProposedAction: ...

    def interpret_reply(self, case: RecoveryCase, message: str) -> ReplyInterpretation: ...
