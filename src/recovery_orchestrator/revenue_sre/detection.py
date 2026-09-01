"""Stage 3 statistical detection and privacy-safe network correlation."""

# ruff: noqa: E501

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.revenue_sre.synthetic_data import INCIDENT_START

MIN_RECENT_ATTEMPTS = 20
MIN_SUCCESS_RATE_DROP = 0.08
MIN_Z_SCORE = 3.0
NETWORK_PRIVACY_THRESHOLD = 3


def _severity(drop: float, z_score: float) -> str:
    if drop >= 0.20 or z_score >= 7:
        return "critical"
    if drop >= 0.14 or z_score >= 5:
        return "high"
    return "medium"


def _finding_id(merchant_id: str, method: str, provider: str, error: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-finding:{merchant_id}:{method}:{provider}:{error}"))


def _cluster_id(method: str, provider: str, issuer: str, device: str, error: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre-cluster:{method}:{provider}:{issuer}:{device}:{error}"))


def run_detection(database: Database) -> dict[str, int]:
    """Detect declines against each merchant's own historic baseline.

    This detector never reads hidden ground-truth tables. It uses only event evidence.
    """

    with database.connect() as connection:
        existing_findings = int(
            connection.execute("SELECT COUNT(*) AS count FROM sre_anomaly_findings").fetchone()["count"]
        )
    if existing_findings:
        return {"findings": existing_findings, "clusters": _count_clusters(database)}

    cutoff = INCIDENT_START.isoformat()
    with database.transaction(immediate=True) as connection:
        connection.execute("DELETE FROM sre_detected_clusters")
        connection.execute("DELETE FROM sre_anomaly_findings")
        baseline_rows = connection.execute(
            """
            SELECT merchant_id, payment_method, provider,
                   COUNT(*) AS attempts,
                   AVG(CASE WHEN status = 'paid' THEN 1.0 ELSE 0.0 END) AS success_rate
            FROM sre_payment_events
            WHERE occurred_at < ?
            GROUP BY merchant_id, payment_method, provider
            """,
            (cutoff,),
        ).fetchall()
        baseline = {
            (row["merchant_id"], row["payment_method"], row["provider"]): row
            for row in baseline_rows
        }
        recent_rows = connection.execute(
            """
            SELECT merchant_id, payment_method, provider, issuer, device, error_family,
                   COUNT(*) AS attempts,
                   AVG(CASE WHEN status = 'paid' THEN 1.0 ELSE 0.0 END) AS success_rate
            FROM sre_payment_events
            WHERE occurred_at >= ? AND error_family IS NOT NULL
            GROUP BY merchant_id, payment_method, provider, issuer, device, error_family
            """,
            (cutoff,),
        ).fetchall()
        findings: list[dict[str, object]] = []
        for row in recent_rows:
            key = (row["merchant_id"], row["payment_method"], row["provider"])
            historic = baseline.get(key)
            if historic is None:
                continue
            attempts = int(row["attempts"])
            observed = float(row["success_rate"])
            expected = float(historic["success_rate"])
            drop = expected - observed
            standard_error = math.sqrt(max(expected * (1 - expected) / attempts, 0.000001))
            z_score = drop / standard_error
            if attempts < MIN_RECENT_ATTEMPTS or drop < MIN_SUCCESS_RATE_DROP or z_score < MIN_Z_SCORE:
                continue
            finding = {
                "finding_id": _finding_id(
                    row["merchant_id"], row["payment_method"], row["provider"], row["error_family"]
                ),
                "merchant_id": row["merchant_id"],
                "time_bucket": cutoff,
                "payment_method": row["payment_method"],
                "provider": row["provider"],
                "issuer": row["issuer"],
                "device": row["device"],
                "error_family": row["error_family"],
                "baseline_attempts": int(historic["attempts"]),
                "recent_attempts": attempts,
                "baseline_success_rate": expected,
                "observed_success_rate": observed,
                "z_score": z_score,
                "severity": _severity(drop, z_score),
            }
            findings.append(finding)
            connection.execute(
                """
                INSERT INTO sre_anomaly_findings(
                    finding_id, merchant_id, time_bucket, payment_method, provider, issuer, device,
                    error_family, baseline_attempts, recent_attempts, baseline_success_rate,
                    observed_success_rate, z_score, severity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding["finding_id"], finding["merchant_id"], finding["time_bucket"],
                    finding["payment_method"], finding["provider"], finding["issuer"], finding["device"],
                    finding["error_family"], finding["baseline_attempts"], finding["recent_attempts"],
                    finding["baseline_success_rate"], finding["observed_success_rate"], finding["z_score"],
                    finding["severity"], datetime.now(UTC).isoformat(),
                ),
            )
        _persist_anonymized_clusters(connection, findings)
    return {"findings": len(findings), "clusters": _count_clusters(database)}


def _persist_anonymized_clusters(connection, findings: list[dict[str, object]]) -> None:
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for finding in findings:
        groups[
            (str(finding["payment_method"]), str(finding["provider"]), str(finding["error_family"]))
        ].append(finding)
    for (method, provider, error), grouped in groups.items():
        merchant_count = len({item["merchant_id"] for item in grouped})
        if merchant_count < NETWORK_PRIVACY_THRESHOLD:
            continue
        average_z = sum(float(item["z_score"]) for item in grouped) / merchant_count
        connection.execute(
            """
            INSERT INTO sre_detected_clusters(
                cluster_id, time_bucket, payment_method, provider, issuer, device, error_family,
                affected_merchant_count, average_z_score, severity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _cluster_id(method, provider, "mixed", "mixed", error), INCIDENT_START.isoformat(), method,
                provider, "mixed", "mixed", error, merchant_count, average_z,
                _severity(0.14, average_z), datetime.now(UTC).isoformat(),
            ),
        )


def _count_clusters(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT COUNT(*) AS count FROM sre_detected_clusters").fetchone()["count"])


def merchant_findings(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT finding_id, time_bucket, payment_method, provider, issuer, device, error_family,
                   baseline_attempts, recent_attempts, baseline_success_rate, observed_success_rate,
                   z_score, severity, status
            FROM sre_anomaly_findings WHERE merchant_id = ? ORDER BY z_score DESC
            """,
            (merchant_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def operations_clusters(database: Database) -> list[dict[str, object]]:
    """Return aggregate cluster data only—no merchant IDs or merchant names."""

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT time_bucket, payment_method, provider, issuer, device, error_family,
                   affected_merchant_count, average_z_score, severity
            FROM sre_detected_clusters ORDER BY affected_merchant_count DESC, average_z_score DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
