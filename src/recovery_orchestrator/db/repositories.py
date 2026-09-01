"""Persistence repositories with explicit idempotency and transaction boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.models import (
    ActionExecutionStatus,
    BatchBudgetSnapshot,
    ProposedAction,
    RecoveryCase,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    case_id: str
    sequence_no: int
    event_type: str
    actor_type: str
    actor_name: str
    from_status: str | None
    to_status: str | None
    decision: str | None
    reason_codes: list[str]
    action_id: str | None
    external_event_id: str | None
    event_hash: str
    previous_event_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ActionExecution:
    action_id: str
    case_id: str
    action_type: str
    status: str
    reference_id: str | None
    request: dict[str, Any]
    response: dict[str, Any] | None
    error_message: str | None


class CaseRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, case: RecoveryCase, now: datetime) -> None:
        timestamp = _iso(now)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO cases(
                    case_id, status, execution_mode, event_type, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    status = excluded.status,
                    execution_mode = excluded.execution_mode,
                    event_type = excluded.event_type,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    case.case_id,
                    case.status.value,
                    case.execution_mode.value,
                    case.event_type.value,
                    case.model_dump_json(),
                    timestamp,
                    timestamp,
                ),
            )

    def get(self, case_id: str) -> RecoveryCase | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        return RecoveryCase.model_validate_json(row["state_json"]) if row else None

    def list_all(self) -> list[RecoveryCase]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM cases ORDER BY updated_at DESC, case_id"
            ).fetchall()
        return [RecoveryCase.model_validate_json(row["state_json"]) for row in rows]

    def delete_all(self) -> None:
        """Reset demo-owned state in foreign-key-safe order."""

        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM budget_reservations")
            connection.execute("DELETE FROM payment_attempts")
            connection.execute("DELETE FROM action_executions")
            connection.execute("DELETE FROM audit_events")
            connection.execute("DELETE FROM external_events")
            connection.execute("DELETE FROM cases")


class AuditRepository:
    GENESIS_HASH = "0" * 64

    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        *,
        case_id: str,
        event_type: str,
        actor_type: str,
        actor_name: str,
        occurred_at_wall: datetime,
        occurred_at_sim: datetime | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        decision: str | None = None,
        reason_codes: list[str] | None = None,
        action_id: str | None = None,
        external_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        reason_codes = reason_codes or []
        payload = payload or {}
        event_id = str(uuid4())
        wall_iso = _iso(occurred_at_wall)
        sim_iso = _iso(occurred_at_sim)

        with self.database.transaction(immediate=True) as connection:
            prior = connection.execute(
                """
                SELECT sequence_no, event_hash
                FROM audit_events
                WHERE case_id = ?
                ORDER BY sequence_no DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            sequence_no = (prior["sequence_no"] + 1) if prior else 1
            previous_hash = prior["event_hash"] if prior else self.GENESIS_HASH
            canonical = {
                "event_id": event_id,
                "case_id": case_id,
                "sequence_no": sequence_no,
                "occurred_at_wall": wall_iso,
                "occurred_at_sim": sim_iso,
                "actor_type": actor_type,
                "actor_name": actor_name,
                "event_type": event_type,
                "from_status": from_status,
                "to_status": to_status,
                "decision": decision,
                "reason_codes": reason_codes,
                "action_id": action_id,
                "external_event_id": external_event_id,
                "payload": payload,
            }
            event_hash = hashlib.sha256(f"{previous_hash}:{_json(canonical)}".encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, case_id, sequence_no, occurred_at_wall, occurred_at_sim,
                    actor_type, actor_name, event_type, from_status, to_status, decision,
                    reason_codes_json, action_id, external_event_id, payload_json,
                    previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    case_id,
                    sequence_no,
                    wall_iso,
                    sim_iso,
                    actor_type,
                    actor_name,
                    event_type,
                    from_status,
                    to_status,
                    decision,
                    _json(reason_codes),
                    action_id,
                    external_event_id,
                    _json(payload),
                    previous_hash,
                    event_hash,
                ),
            )
        return AuditEvent(
            event_id=event_id,
            case_id=case_id,
            sequence_no=sequence_no,
            event_type=event_type,
            actor_type=actor_type,
            actor_name=actor_name,
            from_status=from_status,
            to_status=to_status,
            decision=decision,
            reason_codes=reason_codes,
            action_id=action_id,
            external_event_id=external_event_id,
            event_hash=event_hash,
            previous_event_hash=previous_hash,
            payload=payload,
        )

    def list_for_case(self, case_id: str) -> list[AuditEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, case_id, sequence_no, event_type, actor_type, actor_name,
                       from_status, to_status, decision, reason_codes_json, action_id,
                       external_event_id, event_hash, previous_event_hash, payload_json
                FROM audit_events
                WHERE case_id = ?
                ORDER BY sequence_no
                """,
                (case_id,),
            ).fetchall()
        return [
            AuditEvent(
                event_id=row["event_id"],
                case_id=row["case_id"],
                sequence_no=row["sequence_no"],
                event_type=row["event_type"],
                actor_type=row["actor_type"],
                actor_name=row["actor_name"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                decision=row["decision"],
                reason_codes=json.loads(row["reason_codes_json"]),
                action_id=row["action_id"],
                external_event_id=row["external_event_id"],
                event_hash=row["event_hash"],
                previous_event_hash=row["previous_event_hash"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def verify_chain(self, case_id: str) -> bool:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE case_id = ? ORDER BY sequence_no",
                (case_id,),
            ).fetchall()
        previous_hash = self.GENESIS_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            if row["sequence_no"] != expected_sequence:
                return False
            if row["previous_event_hash"] != previous_hash:
                return False
            canonical = {
                "event_id": row["event_id"],
                "case_id": row["case_id"],
                "sequence_no": row["sequence_no"],
                "occurred_at_wall": row["occurred_at_wall"],
                "occurred_at_sim": row["occurred_at_sim"],
                "actor_type": row["actor_type"],
                "actor_name": row["actor_name"],
                "event_type": row["event_type"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "decision": row["decision"],
                "reason_codes": json.loads(row["reason_codes_json"]),
                "action_id": row["action_id"],
                "external_event_id": row["external_event_id"],
                "payload": json.loads(row["payload_json"]),
            }
            expected_hash = hashlib.sha256(
                f"{previous_hash}:{_json(canonical)}".encode()
            ).hexdigest()
            if row["event_hash"] != expected_hash:
                return False
            previous_hash = expected_hash
        return True


class ActionLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ActionExecution:
        return ActionExecution(
            action_id=row["action_id"],
            case_id=row["case_id"],
            action_type=row["action_type"],
            status=row["status"],
            reference_id=row["reference_id"],
            request=json.loads(row["request_json"]),
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            error_message=row["error_message"],
        )

    def register_pending(
        self,
        case_id: str,
        action: ProposedAction,
        now: datetime,
        reference_id: str | None = None,
    ) -> tuple[ActionExecution, bool]:
        timestamp = _iso(now)
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO action_executions(
                    action_id, case_id, action_type, status, reference_id,
                    request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    case_id,
                    action.type.value,
                    ActionExecutionStatus.PENDING.value,
                    reference_id,
                    action.model_dump_json(),
                    timestamp,
                    timestamp,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM action_executions WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if row["case_id"] != case_id or row["action_type"] != action.type.value:
                raise ValueError("action_id is already associated with another operation")
        return self._from_row(row), created

    def mark_result(
        self,
        action_id: str,
        status: ActionExecutionStatus,
        now: datetime,
        *,
        response: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> ActionExecution:
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE action_executions
                SET status = ?, response_json = ?, error_message = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (
                    status.value,
                    _json(response) if response is not None else None,
                    error_message,
                    _iso(now),
                    action_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(action_id)
            row = connection.execute(
                "SELECT * FROM action_executions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return self._from_row(row)


@dataclass(frozen=True)
class ExternalEventReceipt:
    event_id: str
    event_type: str
    created: bool


class ExternalEventRepository:
    """Durable inbox used to make webhook delivery at-least-once safe."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def receive(
        self,
        *,
        event_id: str,
        source: str,
        event_type: str,
        signature_valid: bool,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> ExternalEventReceipt:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO external_events(
                    event_id, source, event_type, signature_valid,
                    payload_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    source,
                    event_type,
                    int(signature_valid),
                    _json(payload),
                    _iso(received_at),
                ),
            )
        return ExternalEventReceipt(
            event_id=event_id,
            event_type=event_type,
            created=cursor.rowcount == 1,
        )

    def mark_processed(
        self,
        event_id: str,
        processed_at: datetime,
        *,
        case_id: str | None = None,
        processing_error: str | None = None,
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE external_events
                SET processed_at = ?, case_id = ?, processing_error = ?
                WHERE event_id = ?
                """,
                (_iso(processed_at), case_id, processing_error, event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(event_id)


class BudgetRepository:
    def __init__(self, database: Database, budget_name: str = "batch_discount") -> None:
        self.database = database
        self.budget_name = budget_name

    def initialize(self, cap_paise: int, now: datetime) -> None:
        if cap_paise < 0:
            raise ValueError("budget cap cannot be negative")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO budget_state(budget_name, cap_paise, spent_paise, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(budget_name) DO UPDATE SET
                    cap_paise = excluded.cap_paise,
                    updated_at = excluded.updated_at
                """,
                (self.budget_name, cap_paise, _iso(now)),
            )

    def reserve(
        self,
        action_id: str,
        case_id: str,
        amount_paise: int,
        now: datetime,
    ) -> bool:
        if amount_paise <= 0:
            raise ValueError("reservation must be positive")
        timestamp = _iso(now)
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM budget_reservations WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing:
                if existing["case_id"] != case_id or existing["amount_paise"] != amount_paise:
                    raise ValueError("action already has a different budget reservation")
                return existing["status"] in {"reserved", "spent"}

            state = connection.execute(
                "SELECT * FROM budget_state WHERE budget_name = ?", (self.budget_name,)
            ).fetchone()
            if state is None:
                raise RuntimeError("budget is not initialized")
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(amount_paise), 0) AS total
                FROM budget_reservations
                WHERE budget_name = ? AND status = 'reserved'
                """,
                (self.budget_name,),
            ).fetchone()["total"]
            available = state["cap_paise"] - state["spent_paise"] - reserved
            if amount_paise > available:
                return False
            connection.execute(
                """
                INSERT INTO budget_reservations(
                    action_id, budget_name, case_id, amount_paise, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (action_id, self.budget_name, case_id, amount_paise, timestamp, timestamp),
            )
            return True

    def snapshot(self) -> BatchBudgetSnapshot:
        with self.database.connect() as connection:
            state = connection.execute(
                "SELECT * FROM budget_state WHERE budget_name = ?", (self.budget_name,)
            ).fetchone()
            if state is None:
                raise RuntimeError("budget is not initialized")
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(amount_paise), 0) AS total
                FROM budget_reservations
                WHERE budget_name = ? AND status = 'reserved'
                """,
                (self.budget_name,),
            ).fetchone()["total"]
        return BatchBudgetSnapshot(
            discount_cap_paise=state["cap_paise"],
            discount_reserved_paise=reserved,
            discount_spent_paise=state["spent_paise"],
        )
