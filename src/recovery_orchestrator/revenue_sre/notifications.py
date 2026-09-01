"""Stage 6 contacts, deterministic alert routing, and simulated notification delivery."""

# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from recovery_orchestrator.db.connection import Database

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _now() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str, stable: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"revenue-sre:{prefix}:{stable}"))


def seed_notification_defaults(database: Database) -> None:
    """Create safe in-app demo contacts/rules once for each synthetic merchant."""

    timestamp = _now().isoformat()
    with database.transaction(immediate=True) as connection:
        merchants = connection.execute("SELECT merchant_id, name FROM merchants").fetchall()
        for merchant in merchants:
            merchant_id, name = merchant["merchant_id"], merchant["name"]
            contacts = (
                ("owner", f"{name} Owner", f"owner+{merchant_id.lower()}@example.test", None, 1, 0, "high"),
                ("payments", f"{name} Payments", f"payments+{merchant_id.lower()}@example.test", None, 1, 0, "high"),
                ("engineering", f"{name} Engineering", f"engineering+{merchant_id.lower()}@example.test", None, 1, 0, "medium"),
            )
            for team, contact_name, email, phone, email_opt_in, sms_opt_in, severity in contacts:
                connection.execute(
                    """
                    INSERT INTO notification_contacts(
                        contact_id, merchant_id, team, name, email, phone, email_opt_in, sms_opt_in,
                        minimum_severity, verified, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(merchant_id, team, email) DO NOTHING
                    """,
                    (
                        _id("contact", f"{merchant_id}:{team}"), merchant_id, team, contact_name, email,
                        phone, email_opt_in, sms_opt_in, severity, timestamp,
                    ),
                )
            rules = (
                ("merchant_incident", "engineering", "email", "medium", 60),
                ("network_incident", "owner", "email", "high", 120),
                ("approval_required", "owner", "email", "high", 0),
            )
            for event_type, recipient_team, channel, severity, cooldown in rules:
                connection.execute(
                    """
                    INSERT INTO notification_rules(
                        rule_id, merchant_id, event_type, recipient_team, channel, minimum_severity,
                        cooldown_minutes, active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(merchant_id, event_type, recipient_team, channel) DO NOTHING
                    """,
                    (
                        _id("rule", f"{merchant_id}:{event_type}:{recipient_team}:{channel}"), merchant_id,
                        event_type, recipient_team, channel, severity, cooldown, timestamp,
                    ),
                )


def list_contacts(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM notification_contacts WHERE merchant_id = ? ORDER BY team, name", (merchant_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def list_rules(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM notification_rules WHERE merchant_id = ? ORDER BY event_type, recipient_team", (merchant_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def add_contact(
    database: Database,
    *,
    merchant_id: str,
    team: str,
    name: str,
    email: str | None,
    phone: str | None,
    email_opt_in: bool,
    sms_opt_in: bool,
    minimum_severity: str,
) -> dict[str, object]:
    if team not in {"owner", "payments", "engineering", "support"}:
        raise ValueError("Unknown contact team")
    if minimum_severity not in _SEVERITY_RANK:
        raise ValueError("Unknown severity")
    if not email and not phone:
        raise ValueError("Email or phone is required")
    contact_id = _id("contact", f"{merchant_id}:{team}:{email or phone}")
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO notification_contacts(
                contact_id, merchant_id, team, name, email, phone, email_opt_in, sms_opt_in,
                minimum_severity, verified, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(merchant_id, team, email) DO UPDATE SET
                name = excluded.name, phone = excluded.phone, email_opt_in = excluded.email_opt_in,
                sms_opt_in = excluded.sms_opt_in, minimum_severity = excluded.minimum_severity
            """,
            (contact_id, merchant_id, team, name, email, phone, int(email_opt_in), int(sms_opt_in), minimum_severity, _now().isoformat()),
        )
    return next(contact for contact in list_contacts(database, merchant_id) if contact["contact_id"] == contact_id)


def dispatch_for_intervention(database: Database, intervention_id: str) -> list[dict[str, object]]:
    """Route alerts deterministically; adapter writes an explicitly simulated outbox item."""

    with database.connect() as connection:
        intervention = connection.execute(
            """
            SELECT x.*, i.scope, i.root_cause_summary, f.severity, m.name AS merchant_name
            FROM sre_interventions x
            JOIN sre_investigations i ON i.investigation_id = x.investigation_id
            JOIN sre_anomaly_findings f ON f.finding_id = i.finding_id
            JOIN merchants m ON m.merchant_id = x.merchant_id
            WHERE x.intervention_id = ?
            """,
            (intervention_id,),
        ).fetchone()
        if intervention is None:
            raise KeyError("Intervention not found")
        event_type = "approval_required" if intervention["status"] == "awaiting_approval" else (
            "network_incident" if intervention["scope"] == "network" else "merchant_incident"
        )
        rules = connection.execute(
            "SELECT * FROM notification_rules WHERE merchant_id = ? AND event_type = ? AND active = 1",
            (intervention["merchant_id"], event_type),
        ).fetchall()
    sent: list[dict[str, object]] = []
    for rule in rules:
        if _SEVERITY_RANK[intervention["severity"]] < _SEVERITY_RANK[rule["minimum_severity"]]:
            continue
        with database.connect() as connection:
            contacts = connection.execute(
                """
                SELECT * FROM notification_contacts
                WHERE merchant_id = ? AND team = ? AND verified = 1
                  AND ((? = 'email' AND email_opt_in = 1) OR (? = 'sms' AND sms_opt_in = 1))
                """,
                (intervention["merchant_id"], rule["recipient_team"], rule["channel"], rule["channel"]),
            ).fetchall()
        for contact in contacts:
            sent.append(_dispatch_one(database, dict(intervention), dict(rule), dict(contact), event_type))
    return sent


def prepare_critical_engineering_email(
    database: Database, merchant_id: str, finding_id: str
) -> dict[str, object]:
    """Create one reviewable urgent-email outbox item for a critical finding.

    This prepares an email only. A merchant owner must still review and confirm the
    Gemini draft before the controlled provider adapter is allowed to send it.
    """

    route = critical_engineering_route(database, merchant_id, finding_id)
    with database.connect() as connection:
        finding = connection.execute(
            """
            SELECT f.*, m.name AS merchant_name
            FROM sre_anomaly_findings f JOIN merchants m ON m.merchant_id = f.merchant_id
            WHERE f.finding_id = ? AND f.merchant_id = ?
            """,
            (finding_id, merchant_id),
        ).fetchone()
        contact = connection.execute(
            "SELECT * FROM notification_contacts WHERE contact_id = ?",
            (route["recipient_contact_id"],),
        ).fetchone()
    if finding is None:
        raise KeyError("Payment finding not found")
    if finding["severity"] != "critical":
        raise ValueError("Only critical payment findings can trigger the urgent email prompt")
    if contact is None:
        raise ValueError("Add an engineering email contact in Team settings before preparing an urgent email")

    key = f"critical-finding:{finding_id}:{contact['contact_id']}:email"
    notification_id = _id("notification", key)
    subject = f"Urgent: {finding['merchant_name']} payment success drop needs engineering review"
    body = (
        f"Hi {contact['name']}, Revenue SRE detected a critical payment-success drop for "
        f"{finding['payment_method'].upper()} on {finding['device']}. "
        f"Success moved from {float(finding['baseline_success_rate']):.0%} to "
        f"{float(finding['observed_success_rate']):.0%} across {finding['recent_attempts']} attempts. "
        "Please review the payment configuration and incident evidence."
    )
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM notification_outbox WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing:
            return {**dict(existing), "route": route}
        connection.execute(
            """
            INSERT INTO notification_outbox(
                notification_id, merchant_id, intervention_id, recipient_contact_id, channel, status,
                subject, body, idempotency_key, created_at
            ) VALUES (?, ?, NULL, ?, 'email', 'simulated_delivered', ?, ?, ?, ?)
            """,
            (notification_id, merchant_id, contact["contact_id"], subject, body, key, _now().isoformat()),
        )
    return {
        "notification_id": notification_id,
        "merchant_id": merchant_id,
        "recipient_contact_id": contact["contact_id"],
        "channel": "email",
        "status": "simulated_delivered",
        "subject": subject,
        "body": body,
        "route": route,
    }


def critical_engineering_route(
    database: Database, merchant_id: str, finding_id: str
) -> dict[str, object]:
    """Resolve the Team-settings recipient for a critical software escalation."""

    with database.connect() as connection:
        finding = connection.execute(
            """
            SELECT finding_id, severity, error_family
            FROM sre_anomaly_findings
            WHERE finding_id = ? AND merchant_id = ?
            """,
            (finding_id, merchant_id),
        ).fetchone()
        if finding is None:
            raise KeyError("Payment finding not found")
        if finding["severity"] != "critical":
            raise ValueError("Only critical payment findings can trigger an engineering email")
        contact = connection.execute(
            """
            SELECT * FROM notification_contacts
            WHERE merchant_id = ? AND team = 'engineering' AND email_opt_in = 1
              AND email IS NOT NULL AND minimum_severity IN ('low', 'medium', 'high', 'critical')
            ORDER BY
              CASE WHEN email LIKE '%@example.test' THEN 1 ELSE 0 END,
              created_at DESC
            LIMIT 1
            """,
            (merchant_id,),
        ).fetchone()
    if contact is None:
        raise ValueError("Add an engineering email contact in Team settings before sending this alert")
    if _SEVERITY_RANK[str(finding["severity"])] < _SEVERITY_RANK[str(contact["minimum_severity"])]:
        raise ValueError("The Engineering contact's minimum severity does not include this alert")
    return {
        "recipient_contact_id": contact["contact_id"],
        "recipient_name": contact["name"],
        "recipient_team": contact["team"],
        "recipient_email": contact["email"],
        "reason": (
            f"Critical {finding['error_family']} is a software/checkout escalation; "
            f"the Engineering team accepts {contact['minimum_severity']} and above alerts."
        ),
    }


def _dispatch_one(
    database: Database, intervention: dict[str, object], rule: dict[str, object], contact: dict[str, object], event_type: str
) -> dict[str, object]:
    key = f"{intervention['intervention_id']}:{contact['contact_id']}:{rule['channel']}:{event_type}"
    notification_id = _id("notification", key)
    now = _now()
    subject, body = _message(intervention, contact, event_type)
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM notification_outbox WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing:
            return {**dict(existing), "status": "suppressed_duplicate"}
        latest = connection.execute(
            """
            SELECT created_at FROM notification_outbox
            WHERE merchant_id = ? AND recipient_contact_id = ? AND channel = ? AND subject = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (intervention["merchant_id"], contact["contact_id"], rule["channel"], subject),
        ).fetchone()
        if latest and now - datetime.fromisoformat(latest["created_at"]) < timedelta(minutes=rule["cooldown_minutes"]):
            return {
                "notification_id": notification_id,
                "status": "suppressed_cooldown",
                "recipient": contact["email"] or contact["phone"],
            }
        connection.execute(
            """
            INSERT INTO notification_outbox(
                notification_id, merchant_id, intervention_id, recipient_contact_id, channel, status,
                subject, body, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, 'simulated_delivered', ?, ?, ?, ?)
            """,
            (notification_id, intervention["merchant_id"], intervention["intervention_id"], contact["contact_id"], rule["channel"], subject, body, key, now.isoformat()),
        )
    return {
        "notification_id": notification_id,
        "status": "simulated_delivered",
        "channel": rule["channel"],
        "recipient": contact["email"] or contact["phone"],
        "subject": subject,
    }


def _message(intervention: dict[str, object], contact: dict[str, object], event_type: str) -> tuple[str, str]:
    if event_type == "approval_required":
        subject = "Approval needed: payment reliability action"
        body = f"Hi {contact['name']}, a payment issue needs your approval. Proposed action: {intervention['action_summary']}"
    elif event_type == "network_incident":
        subject = "Payment network issue detected"
        body = "We detected a shared payment-network pattern. The agents are monitoring it; no website code change is requested."
    else:
        subject = "Payment failures need engineering attention"
        body = f"Hi {contact['name']}, {intervention['root_cause_summary']} Proposed next step: {intervention['action_summary']}"
    return subject, body


def list_outbox(database: Database, merchant_id: str) -> list[dict[str, object]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT o.notification_id, o.intervention_id, o.channel, o.status, o.subject, o.body,
                   o.created_at, c.team, c.name AS recipient_name, c.email, c.phone
            FROM notification_outbox o JOIN notification_contacts c ON c.contact_id = o.recipient_contact_id
            WHERE o.merchant_id = ? ORDER BY o.created_at DESC
            """,
            (merchant_id,),
        ).fetchall()
    return [dict(row) for row in rows]
