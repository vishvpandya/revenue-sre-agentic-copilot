"""Stage 5 deterministic policy, bounded execution, verification, and rollback."""

# ruff: noqa: E501

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _intervention_id(investigation_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-intervention:{investigation_id}"))


def _event_id(intervention_id: str, sequence_no: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{intervention_id}:event:{sequence_no}"))


def _investigation(database: Database, investigation_id: str) -> dict[str, object]:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT i.*, f.baseline_success_rate, f.observed_success_rate, f.recent_attempts,
                   f.error_family
            FROM sre_investigations i
            JOIN sre_anomaly_findings f ON f.finding_id = i.finding_id
            WHERE i.investigation_id = ?
            """,
            (investigation_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Investigation not found")
    return dict(row)


def _policy(investigation: dict[str, object]) -> tuple[str, str, str]:
    """Deterministic policy. Agent language cannot override these boundaries."""

    if bool(investigation["approval_required"]):
        return (
            "needs_approval",
            "A merchant-facing change or customer communication needs merchant approval.",
            "awaiting_approval",
        )
    if investigation["scope"] == "network":
        return (
            "approved",
            "The plan uses only an existing payment fallback and status communication in the synthetic demo.",
            "approved",
        )
    return (
        "blocked",
        "The proposed action is outside the automatic-action policy.",
        "rejected",
    )


def _append_event(
    connection, intervention_id: str, event_type: str, actor: str, details: dict[str, object]
) -> None:
    prior = connection.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) AS last_sequence FROM sre_intervention_events WHERE intervention_id = ?",
        (intervention_id,),
    ).fetchone()
    sequence_no = int(prior["last_sequence"]) + 1
    connection.execute(
        """
        INSERT INTO sre_intervention_events(
            event_id, intervention_id, sequence_no, event_type, actor, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _event_id(intervention_id, sequence_no),
            intervention_id,
            sequence_no,
            event_type,
            actor,
            json.dumps(details, sort_keys=True),
            _now(),
        ),
    )


def request_intervention(database: Database, investigation_id: str) -> dict[str, object]:
    investigation = _investigation(database, investigation_id)
    intervention_id = _intervention_id(investigation_id)
    decision, reason, status = _policy(investigation)
    timestamp = _now()
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT intervention_id FROM sre_interventions WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO sre_interventions(
                    intervention_id, investigation_id, merchant_id, status, policy_decision,
                    policy_reason, action_summary, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intervention_id,
                    investigation_id,
                    investigation["merchant_id"],
                    status,
                    decision,
                    reason,
                    investigation["proposed_action"],
                    f"intervention:{investigation_id}",
                    timestamp,
                    timestamp,
                ),
            )
            _append_event(
                connection,
                intervention_id,
                "policy_evaluated",
                "Deterministic Policy Gate",
                {"decision": decision, "reason": reason},
            )
    return get_intervention_by_investigation(database, investigation_id)


def approve_intervention(
    database: Database, intervention_id: str, *, approved: bool, actor: str
) -> dict[str, object]:
    with database.transaction(immediate=True) as connection:
        intervention = connection.execute(
            "SELECT * FROM sre_interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if intervention is None:
            raise KeyError("Intervention not found")
        if intervention["status"] != "awaiting_approval":
            raise ValueError("Intervention is not awaiting approval")
        status = "approved" if approved else "rejected"
        connection.execute(
            "UPDATE sre_interventions SET status = ?, updated_at = ? WHERE intervention_id = ?",
            (status, _now(), intervention_id),
        )
        _append_event(
            connection,
            intervention_id,
            "merchant_approval_recorded",
            actor,
            {"approved": approved},
        )
    return get_intervention(database, intervention_id)


def execute_intervention(database: Database, intervention_id: str) -> dict[str, object]:
    """Execute only a bounded simulated action; repeat calls are intentionally idempotent."""

    with database.transaction(immediate=True) as connection:
        intervention = connection.execute(
            "SELECT * FROM sre_interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if intervention is None:
            raise KeyError("Intervention not found")
        if intervention["status"] == "executed":
            pass
        elif intervention["status"] != "approved":
            raise ValueError("Only approved interventions can execute")
        else:
            connection.execute(
                "UPDATE sre_interventions SET status = 'executed', executed_at = ?, updated_at = ? WHERE intervention_id = ?",
                (_now(), _now(), intervention_id),
            )
            _append_event(
                connection,
                intervention_id,
                "bounded_action_executed",
                "Bounded Executor",
                {
                    "mode": "synthetic_simulation",
                    "idempotency_key": intervention["idempotency_key"],
                },
            )
    return get_intervention(database, intervention_id)


def verify_intervention(database: Database, intervention_id: str) -> dict[str, object]:
    """Measure a synthetic treated group against holdout; only the executor reads outcome script."""

    with database.transaction(immediate=True) as connection:
        intervention = connection.execute(
            "SELECT * FROM sre_interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if intervention is None:
            raise KeyError("Intervention not found")
        if intervention["status"] in {
            "verified",
            "replan_required",
            "rollback_required",
            "rolled_back",
        }:
            pass
        elif intervention["status"] != "executed":
            raise ValueError("Only executed interventions can be verified")
        else:
            _persist_measurement(connection, intervention_id, intervention)
    return get_intervention(database, intervention_id)


def _persist_measurement(connection, intervention_id: str, intervention) -> None:
    investigation = connection.execute(
        """
            SELECT f.baseline_success_rate, f.observed_success_rate, f.recent_attempts, g.outcome_script
            FROM sre_investigations i
            JOIN sre_anomaly_findings f ON f.finding_id = i.finding_id
            JOIN sre_hidden_ground_truth g ON g.incident_id = (
                'INC-' || REPLACE(i.merchant_id, 'MRC-', '')
            )
            WHERE i.investigation_id = ?
            """,
        (intervention["investigation_id"],),
    ).fetchone()
    if investigation is None:
        raise RuntimeError("Synthetic outcome script is unavailable")
    baseline = float(investigation["baseline_success_rate"])
    holdout = float(investigation["observed_success_rate"])
    script = investigation["outcome_script"]
    if script in {"recovery_success", "rollback_success"}:
        treated = min(0.99, holdout + 0.24)
        outcome, next_status = "improved", "verified"
        summary = "The treated payment flow improved materially compared with the unchanged holdout group."
    elif script == "negative_side_effect_rollback":
        treated = max(0.0, holdout - 0.08)
        outcome, next_status = "negative_side_effect", "rollback_required"
        summary = (
            "The treated payment flow performed worse than the holdout group. Rollback is required."
        )
    else:
        treated = min(0.99, holdout + 0.01)
        outcome, next_status = "no_improvement", "replan_required"
        summary = "The treated payment flow did not improve enough compared with the holdout group."
    connection.execute(
        """
            INSERT INTO sre_intervention_measurements(
                intervention_id, baseline_success_rate, treated_success_rate, holdout_success_rate,
                affected_attempts, outcome, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
        (
            intervention_id,
            baseline,
            treated,
            holdout,
            investigation["recent_attempts"],
            outcome,
            summary,
        ),
    )
    connection.execute(
        "UPDATE sre_interventions SET status = ?, verified_at = ?, updated_at = ? WHERE intervention_id = ?",
        (next_status, _now(), _now(), intervention_id),
    )
    _append_event(
        connection,
        intervention_id,
        "treated_vs_holdout_verified",
        "Verifier Agent",
        {"outcome": outcome, "treated_success_rate": treated, "holdout_success_rate": holdout},
    )


def rollback_intervention(database: Database, intervention_id: str) -> dict[str, object]:
    with database.transaction(immediate=True) as connection:
        intervention = connection.execute(
            "SELECT * FROM sre_interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if intervention is None:
            raise KeyError("Intervention not found")
        if intervention["status"] != "rollback_required":
            raise ValueError("Rollback is not required for this intervention")
        connection.execute(
            "UPDATE sre_interventions SET status = 'rolled_back', updated_at = ? WHERE intervention_id = ?",
            (_now(), intervention_id),
        )
        _append_event(
            connection,
            intervention_id,
            "rollback_completed",
            "Rollback Controller",
            {"mode": "synthetic_simulation"},
        )
    return get_intervention(database, intervention_id)


def get_intervention_by_investigation(
    database: Database, investigation_id: str
) -> dict[str, object]:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT intervention_id FROM sre_interventions WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Intervention not found")
    return get_intervention(database, row["intervention_id"])


def get_intervention(database: Database, intervention_id: str) -> dict[str, object]:
    with database.connect() as connection:
        intervention = connection.execute(
            "SELECT * FROM sre_interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        measurement = connection.execute(
            "SELECT * FROM sre_intervention_measurements WHERE intervention_id = ?",
            (intervention_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT sequence_no, event_type, actor, details_json, created_at FROM sre_intervention_events WHERE intervention_id = ? ORDER BY sequence_no",
            (intervention_id,),
        ).fetchall()
    if intervention is None:
        raise KeyError("Intervention not found")
    result = dict(intervention)
    result["measurement"] = dict(measurement) if measurement else None
    result["events"] = [
        {**dict(event), "details": json.loads(event["details_json"])} for event in events
    ]
    for event in result["events"]:
        event.pop("details_json")
    return result


def interventions_for_merchant(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT intervention_id FROM sre_interventions WHERE merchant_id = ? ORDER BY updated_at DESC",
            (merchant_id,),
        ).fetchall()
    return [get_intervention(database, row["intervention_id"]) for row in rows]
