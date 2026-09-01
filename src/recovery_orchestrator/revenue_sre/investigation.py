"""Stage 4 specialist-agent investigation loop with persistent evidence and trace."""

# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    question: str
    payload: dict[str, object]


def _investigation_id(finding_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-investigation:{finding_id}"))


def _step_id(investigation_id: str, sequence_no: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{investigation_id}:{sequence_no}"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _finding(database: Database, finding_id: str) -> dict[str, object]:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT f.*, m.name AS merchant_name, p.sdk_version
            FROM sre_anomaly_findings f
            JOIN merchants m ON m.merchant_id = f.merchant_id
            JOIN merchant_profiles p ON p.merchant_id = f.merchant_id
            WHERE f.finding_id = ?
            """,
            (finding_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Payment-health finding not found")
    return dict(row)


def _recent_evidence(database: Database, finding: dict[str, object]) -> ToolObservation:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS attempts,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts,
                   AVG(authorization_latency_ms) AS average_latency_ms
            FROM sre_payment_events
            WHERE merchant_id = ? AND payment_method = ? AND provider = ? AND error_family = ?
              AND occurred_at >= ?
            """,
            (
                finding["merchant_id"],
                finding["payment_method"],
                finding["provider"],
                finding["error_family"],
                finding["time_bucket"],
            ),
        ).fetchone()
    return ToolObservation(
        tool_name="inspect_recent_payment_events",
        question="How large is this payment-health change for this merchant?",
        payload={
            "attempts": int(row["attempts"] or 0),
            "failed_attempts": int(row["failed_attempts"] or 0),
            "average_latency_ms": round(float(row["average_latency_ms"] or 0)),
            "baseline_success_rate": finding["baseline_success_rate"],
            "observed_success_rate": finding["observed_success_rate"],
        },
    )


def _network_evidence(database: Database, finding: dict[str, object]) -> ToolObservation:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT affected_merchant_count, average_z_score, severity
            FROM sre_detected_clusters
            WHERE payment_method = ? AND provider = ? AND error_family = ?
            """,
            (finding["payment_method"], finding["provider"], finding["error_family"]),
        ).fetchone()
    return ToolObservation(
        tool_name="check_anonymized_network_pattern",
        question="Are independently affected merchants showing the same failure pattern?",
        payload={
            "network_pattern_found": row is not None,
            "affected_merchant_count": int(row["affected_merchant_count"]) if row else 0,
            "average_z_score": round(float(row["average_z_score"]), 2) if row else None,
            "network_severity": row["severity"] if row else None,
        },
    )


def _merchant_context(finding: dict[str, object]) -> ToolObservation:
    return ToolObservation(
        tool_name="inspect_merchant_integration_context",
        question="Is there merchant integration evidence that changes the likely scope?",
        payload={
            "sdk_version": finding["sdk_version"],
            "payment_method": finding["payment_method"],
            "device": finding["device"],
        },
    )


def _scope_and_cause(
    finding: dict[str, object], network: ToolObservation, merchant_context: ToolObservation
) -> tuple[str, str, float]:
    error = str(finding["error_family"])
    network_found = bool(network.payload["network_pattern_found"])
    if network_found:
        count = network.payload["affected_merchant_count"]
        return (
            "network",
            f"Similar {finding['payment_method'].upper()} failures are occurring across {count} anonymized merchants. This does not point to one merchant website.",
            0.90,
        )
    if error in {"expired_mandate", "international_3ds", "issuer_decline"}:
        return (
            "customer",
            "The pattern is limited to a payment instrument or authentication journey, not the merchant's full checkout.",
            0.78,
        )
    if error in {"sdk_regression", "checkout_javascript", "android_upi_redirect", "mobile_redirect", "coupon_misconfiguration", "page_latency"}:
        version = merchant_context.payload["sdk_version"]
        return (
            "merchant",
            f"The evidence is isolated to this merchant's checkout configuration (current SDK {version}).",
            0.87,
        )
    return (
        "merchant",
        "The evidence is currently isolated to this merchant and needs a bounded technical review.",
        0.66,
    )


def _plan(scope: str, finding: dict[str, object]) -> tuple[str, bool, str]:
    error = str(finding["error_family"])
    if scope == "network":
        return (
            "Keep checkout available, offer the merchant's existing alternate payment method where available, and send a plain-language status update. Do not ask the software team to change code.",
            False,
            "completed",
        )
    if scope == "customer":
        return (
            "Ask the affected customer to use an alternate approved method or update their payment mandate. Do not send repeated messages automatically.",
            True,
            "needs_review",
        )
    if error in {"sdk_regression", "checkout_javascript", "android_upi_redirect", "mobile_redirect"}:
        return (
            "Ask the software team to restore the previous stable payment setup, then measure whether payment success returns to normal.",
            True,
            "needs_review",
        )
    return (
        "Ask the software team to review the checkout configuration and approve a bounded corrective change before it runs.",
        True,
        "needs_review",
    )


def run_investigation(database: Database, finding_id: str) -> dict[str, object]:
    """Run three specialist roles from visible evidence and persist their complete trace."""

    finding = _finding(database, finding_id)
    recent = _recent_evidence(database, finding)
    network = _network_evidence(database, finding)
    merchant_context = _merchant_context(finding)
    scope, cause, confidence = _scope_and_cause(finding, network, merchant_context)
    action, approval_required, status = _plan(scope, finding)
    investigation_id = _investigation_id(finding_id)
    steps = [
        (
            "Scope Investigator",
            recent,
            "The payment-success decline is materially different from this merchant's own baseline.",
        ),
        (
            "Root-Cause Investigator",
            network,
            cause,
        ),
        (
            "Recovery Planner",
            merchant_context,
            action,
        ),
    ]
    evidence = {
        "recent_payment_events": recent.payload,
        "network_pattern": network.payload,
        "merchant_context": merchant_context.payload,
    }
    timestamp = _now()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO sre_investigations(
                investigation_id, finding_id, merchant_id, status, scope, root_cause_summary,
                confidence, proposed_action, approval_required, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_id) DO UPDATE SET
                status = excluded.status, scope = excluded.scope,
                root_cause_summary = excluded.root_cause_summary, confidence = excluded.confidence,
                proposed_action = excluded.proposed_action, approval_required = excluded.approval_required,
                evidence_json = excluded.evidence_json, updated_at = excluded.updated_at
            """,
            (
                investigation_id, finding_id, finding["merchant_id"], status, scope, cause,
                confidence, action, int(approval_required), json.dumps(evidence, sort_keys=True), timestamp, timestamp,
            ),
        )
        connection.execute("DELETE FROM sre_agent_steps WHERE investigation_id = ?", (investigation_id,))
        for sequence_no, (agent_name, observation, conclusion) in enumerate(steps, start=1):
            connection.execute(
                """
                INSERT INTO sre_agent_steps(
                    step_id, investigation_id, sequence_no, agent_name, tool_name, question,
                    observation_json, conclusion, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _step_id(investigation_id, sequence_no), investigation_id, sequence_no, agent_name,
                    observation.tool_name, observation.question, json.dumps(observation.payload, sort_keys=True),
                    conclusion, timestamp,
                ),
            )
    return get_investigation(database, investigation_id)


def get_investigation(database: Database, investigation_id: str) -> dict[str, object]:
    with database.connect() as connection:
        investigation = connection.execute(
            "SELECT * FROM sre_investigations WHERE investigation_id = ?", (investigation_id,)
        ).fetchone()
        steps = connection.execute(
            "SELECT sequence_no, agent_name, tool_name, question, observation_json, conclusion, created_at FROM sre_agent_steps WHERE investigation_id = ? ORDER BY sequence_no",
            (investigation_id,),
        ).fetchall()
    if investigation is None:
        raise KeyError("Investigation not found")
    result = dict(investigation)
    result["approval_required"] = bool(result["approval_required"])
    result["evidence"] = json.loads(result.pop("evidence_json"))
    result["agent_trace"] = [
        {**dict(step), "observation": json.loads(step["observation_json"])} for step in steps
    ]
    for step in result["agent_trace"]:
        step.pop("observation_json")
    return result


def investigations_for_merchant(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT investigation_id FROM sre_investigations WHERE merchant_id = ? ORDER BY updated_at DESC",
            (merchant_id,),
        ).fetchall()
    return [get_investigation(database, row["investigation_id"]) for row in rows]


def run_all_investigations(database: Database) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT finding_id FROM sre_anomaly_findings WHERE status = 'open' ORDER BY severity DESC, z_score DESC"
        ).fetchall()
    return [run_investigation(database, row["finding_id"]) for row in rows]
