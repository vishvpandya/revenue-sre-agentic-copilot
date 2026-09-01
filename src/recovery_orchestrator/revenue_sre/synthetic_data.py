"""Deterministic multi-merchant payment evidence for Revenue SRE stages 1 and 2."""

# ruff: noqa: E501

from __future__ import annotations

import json
import random
import secrets
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.revenue_sre.catalog import DEMO_MERCHANTS

SEED = 20260829
BASELINE_START = datetime(2026, 8, 1, tzinfo=UTC)
INCIDENT_START = datetime(2026, 8, 15, 9, tzinfo=UTC)


_INCIDENTS: dict[str, tuple[str, str, str, str, str, str, str]] = {
    "MRC-01": (
        "Merchant checkout release issue",
        "merchant",
        "critical",
        "sdk_regression",
        "rollback_stable_checkout",
        "rollback_success",
        "card",
    ),
    "MRC-02": (
        "Subscription mandates require customer update",
        "customer",
        "medium",
        "expired_mandate",
        "request_payment_update",
        "recovery_success",
        "mandate",
    ),
    "MRC-03": (
        "UPI provider failures increased",
        "network",
        "high",
        "upi_provider_degradation",
        "route_to_fallback",
        "recovery_success",
        "upi",
    ),
    "MRC-04": (
        "Flash-sale payment latency",
        "merchant",
        "high",
        "capacity_pressure",
        "request_engineering_review",
        "no_change",
        "card",
    ),
    "MRC-05": (
        "Issuer OTP authentication failures",
        "network",
        "high",
        "issuer_otp_degradation",
        "inform_and_retry",
        "recovery_success",
        "card",
    ),
    "MRC-06": (
        "International 3DS authentication failures",
        "customer",
        "medium",
        "international_3ds",
        "request_alternate_method",
        "recovery_success",
        "card",
    ),
    "MRC-07": (
        "Genuine payments blocked by a risk rule",
        "merchant",
        "high",
        "risk_false_positive",
        "request_rule_review",
        "negative_side_effect_rollback",
        "card",
    ),
    "MRC-08": (
        "Wallet provider failures",
        "network",
        "medium",
        "wallet_degradation",
        "offer_upi_fallback",
        "recovery_success",
        "wallet",
    ),
    "MRC-09": (
        "Checkout page JavaScript failure",
        "merchant",
        "critical",
        "checkout_javascript",
        "request_engineering_review",
        "rollback_success",
        "upi",
    ),
    "MRC-10": (
        "Successful payments have delayed webhooks",
        "merchant",
        "low",
        "webhook_delay",
        "pause_customer_messages",
        "recovery_success",
        "card",
    ),
    "MRC-11": (
        "UPI provider failures increased",
        "network",
        "high",
        "upi_provider_degradation",
        "route_to_fallback",
        "recovery_success",
        "upi",
    ),
    "MRC-12": (
        "Discounted checkout configuration issue",
        "merchant",
        "medium",
        "coupon_misconfiguration",
        "request_engineering_review",
        "rollback_success",
        "upi",
    ),
    "MRC-13": (
        "Subscription mandate retry needed",
        "customer",
        "medium",
        "expired_mandate",
        "request_payment_update",
        "recovery_success",
        "mandate",
    ),
    "MRC-14": (
        "Android UPI redirect issue",
        "merchant",
        "high",
        "android_upi_redirect",
        "rollback_stable_checkout",
        "rollback_success",
        "upi",
    ),
    "MRC-15": (
        "International issuer declines increased",
        "customer",
        "medium",
        "issuer_decline",
        "request_alternate_method",
        "no_change",
        "card",
    ),
    "MRC-16": (
        "Payment page is slower after release",
        "merchant",
        "medium",
        "page_latency",
        "request_engineering_review",
        "rollback_success",
        "upi",
    ),
    "MRC-17": (
        "Issuer OTP failures during demand surge",
        "network",
        "high",
        "issuer_otp_degradation",
        "inform_and_retry",
        "recovery_success",
        "card",
    ),
    "MRC-18": (
        "Mobile browser redirect regression",
        "merchant",
        "high",
        "mobile_redirect",
        "rollback_stable_checkout",
        "rollback_success",
        "upi",
    ),
    "MRC-19": (
        "Payment captured after delayed authorization",
        "merchant",
        "low",
        "late_authorization",
        "pause_customer_messages",
        "recovery_success",
        "card",
    ),
    "MRC-20": (
        "Payment success is within normal variation",
        "normal_variation",
        "low",
        "normal_variation",
        "take_no_action",
        "no_action",
        "upi",
    ),
}

# Each pack keeps the same demonstration guarantees, but moves the business stories
# and correlated network signals to different merchants. This avoids a reset that
# merely changes invisible random numbers while leaving every dashboard unchanged.
_SCENARIO_PACKS: tuple[dict[str, object], ...] = (
    {
        "name": "Checkout regression and UPI provider cluster",
        "rotation": 0,
        "shared_clusters": (
            ("upi", "upi_provider_degradation", ("MRC-03", "MRC-08", "MRC-11", "MRC-14")),
            ("card", "issuer_otp_degradation", ("MRC-04", "MRC-05", "MRC-17")),
        ),
    },
    {
        "name": "Issuer OTP cluster and mobile checkout regression",
        "rotation": 4,
        "shared_clusters": (
            ("card", "issuer_otp_degradation", ("MRC-01", "MRC-06", "MRC-12", "MRC-19")),
            ("upi", "upi_provider_degradation", ("MRC-02", "MRC-09", "MRC-16")),
        ),
    },
    {
        "name": "UPI redirect cluster and subscription reliability run",
        "rotation": 9,
        "shared_clusters": (
            ("upi", "upi_provider_degradation", ("MRC-04", "MRC-07", "MRC-13", "MRC-18")),
            ("card", "issuer_otp_degradation", ("MRC-03", "MRC-10", "MRC-15")),
        ),
    },
    {
        "name": "Mixed merchant checkout and bank verification run",
        "rotation": 14,
        "shared_clusters": (
            ("card", "issuer_otp_degradation", ("MRC-02", "MRC-08", "MRC-11", "MRC-20")),
            ("upi", "upi_provider_degradation", ("MRC-05", "MRC-10", "MRC-17")),
        ),
    },
)


def _scenario_incidents(
    scenario_index: int, seed: int
) -> tuple[dict[str, object], dict[str, tuple[str, str, str, str, str, str, str]]]:
    """Shuffle complete incident stories across merchants for each fresh demo seed."""

    scenario = _SCENARIO_PACKS[scenario_index % len(_SCENARIO_PACKS)]
    merchant_ids = list(_INCIDENTS)
    incident_stories = list(_INCIDENTS.values())
    if seed == SEED and scenario_index == 0:
        # The first seeded dataset is kept stable for automated checks and a
        # predictable first-run walkthrough. User-created runs are shuffled below.
        return scenario, dict(_INCIDENTS)
    # A reset changes the business problem itself, not merely a percentage on the
    # same merchant card. The seeded shuffle remains reproducible for that run.
    random.Random(f"revenue-sre-incidents:{seed}:{scenario_index}").shuffle(incident_stories)
    incidents = {
        merchant_id: incident_stories[index]
        for index, merchant_id in enumerate(merchant_ids)
    }
    return scenario, incidents


def _event_id(merchant_id: str, occurred_at: datetime, index: int) -> str:
    return str(
        uuid5(NAMESPACE_URL, f"revenue-sre:{SEED}:{merchant_id}:{occurred_at.isoformat()}:{index}")
    )


def seed_synthetic_data(
    database: Database, *, seed: int = SEED, scenario_index: int = 0
) -> dict[str, int | str]:
    """Replace Revenue SRE evidence with one reproducible 20-merchant synthetic run."""

    rng = random.Random(seed)
    scenario, incidents = _scenario_incidents(scenario_index, seed)
    created_events = 0
    with database.transaction(immediate=True) as connection:
        # A new demo run starts with no inherited decisions, notifications, or agent trace.
        # Contacts remain because they are merchant settings rather than run evidence.
        # Inbound WhatsApp replies are linked to their original alert, so clear
        # them before replacing that alert with a fresh demo run.
        # Customer-recovery recipients reference their batch, and a batch references
        # the investigation/finding for the previous run. Clear that child evidence
        # before deleting the older investigation graph.
        connection.execute("DELETE FROM customer_recovery_recipients")
        connection.execute("DELETE FROM customer_recovery_batches")
        connection.execute("DELETE FROM whatsapp_inbound_events")
        connection.execute("DELETE FROM notification_live_attempts")
        connection.execute("DELETE FROM notification_outbox")
        connection.execute("DELETE FROM sre_copilot_messages")
        connection.execute("DELETE FROM sre_intervention_measurements")
        connection.execute("DELETE FROM sre_intervention_events")
        connection.execute("DELETE FROM sre_interventions")
        connection.execute("DELETE FROM sre_agent_steps")
        connection.execute("DELETE FROM sre_investigations")
        connection.execute("DELETE FROM sre_detected_clusters")
        connection.execute("DELETE FROM sre_anomaly_findings")
        connection.execute("DELETE FROM sre_network_signatures")
        connection.execute("DELETE FROM sre_hidden_ground_truth")
        connection.execute("DELETE FROM sre_incidents")
        connection.execute("DELETE FROM sre_payment_events")
        for merchant_index, merchant in enumerate(DEMO_MERCHANTS):
            methods = merchant.methods
            platforms = merchant.platforms
            run_baseline = round(max(0.82, min(0.98, merchant.baseline_success_rate + rng.uniform(-0.035, 0.035))), 3)
            connection.execute(
                "UPDATE merchants SET baseline_success_rate = ? WHERE merchant_id = ?",
                (run_baseline, merchant.merchant_id),
            )
            for hour_index in range(14 * 24):
                occurred_at = BASELINE_START + timedelta(hours=hour_index)
                # 14 days × 24 hours × 20 merchants × 1.5 attempts/hour = 10,080 events.
                # It is large enough for baselines but quick to reset during a judge demo.
                hourly_volume = 1 + ((merchant_index + hour_index) % 2)
                for event_index in range(hourly_volume):
                    method = methods[(hour_index + event_index) % len(methods)]
                    device = platforms[(hour_index + event_index) % len(platforms)]
                    failed = rng.random() > run_baseline
                    connection.execute(
                        """
                        INSERT INTO sre_payment_events(
                            event_id, merchant_id, occurred_at, payment_method, provider, issuer, device,
                            sdk_version, amount_paise, status, error_family, authorization_latency_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _event_id(merchant.merchant_id, occurred_at, event_index),
                            merchant.merchant_id,
                            occurred_at.isoformat(),
                            method,
                            "upi_collect" if method == "upi" else "card_gateway",
                            f"issuer_{1 + ((merchant_index + event_index) % 5)}",
                            device,
                            merchant.sdk_version,
                            1_000_00 + rng.randrange(1_000_00),
                            "failed" if failed else "paid",
                            "baseline_decline" if failed else None,
                            400 + rng.randrange(900),
                        ),
                    )
                    created_events += 1
            title, scope, severity, cause, action, outcome, method = incidents[merchant.merchant_id]
            created_events += _insert_incident_window(
                connection,
                merchant_id=merchant.merchant_id,
                method=method,
                issuer="issuer_1",
                device=merchant.platforms[0],
                sdk_version=merchant.sdk_version,
                error_family=cause,
                normal_variation=scope == "normal_variation",
                failed_attempts=2 if scope == "normal_variation" else rng.randint(8, 14),
                event_offset=merchant_index * 100,
            )
            # Secondary evidence creates two true cross-merchant clusters. The affected
            # merchant membership changes with each scenario pack.
            for cluster_index, (shared_method, shared_error, members) in enumerate(
                scenario["shared_clusters"]
            ):
                if merchant.merchant_id in members:
                    created_events += _insert_incident_window(
                        connection,
                        merchant_id=merchant.merchant_id,
                        method=shared_method,
                        issuer="issuer_1",
                        device=merchant.platforms[0],
                        sdk_version=merchant.sdk_version,
                        error_family=shared_error,
                        normal_variation=False,
                        failed_attempts=rng.randint(8, 14),
                        event_offset=9_000 + cluster_index * 100 + merchant_index,
                    )
            incident_id = f"INC-{merchant.merchant_id.removeprefix('MRC-')}"
            if seed == SEED and scenario_index == 0:
                observed_drop = 0.03 if scope == "normal_variation" else 0.18
            else:
                observed_drop = (
                    rng.uniform(0.02, 0.05)
                    if scope == "normal_variation"
                    else rng.uniform(0.13, 0.28)
                )
            evidence = {
                "affected_attempts": 18 + merchant_index,
                "baseline_success_rate": run_baseline,
                "observed_success_rate": round(
                    run_baseline
                    - observed_drop,
                    3,
                ),
                "payment_method": method,
                "plain_language": merchant.story,
            }
            connection.execute(
                """
                INSERT INTO sre_incidents(
                    incident_id, merchant_id, title, scope, status, severity, started_at, summary,
                    visible_evidence_json
                ) VALUES (?, ?, ?, ?, 'detected', ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    merchant.merchant_id,
                    title,
                    scope,
                    severity,
                    INCIDENT_START.isoformat(),
                    merchant.story,
                    json.dumps(evidence, sort_keys=True),
                ),
            )
            connection.execute(
                """INSERT INTO sre_hidden_ground_truth(incident_id, root_cause, expected_action, outcome_script)
                VALUES (?, ?, ?, ?)""",
                (incident_id, cause, action, outcome),
            )
            provider = "upi_collect" if method == "upi" else "card_gateway"
            issuer = "issuer_otp" if cause == "issuer_otp_degradation" else "mixed"
            device = "android" if cause == "android_upi_redirect" else "mixed"
            connection.execute(
                """
                INSERT INTO sre_network_signatures(
                    signature_id, incident_id, time_bucket, payment_method, provider, issuer, device,
                    error_family, deviation_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"SIG-{merchant.merchant_id.removeprefix('MRC-')}",
                    incident_id,
                    INCIDENT_START.isoformat(),
                    method,
                    provider,
                    issuer,
                    device,
                    cause,
                    0.03 if scope == "normal_variation" else 0.18,
                ),
            )
            for cluster_index, (shared_method, shared_error, members) in enumerate(
                scenario["shared_clusters"]
            ):
                if merchant.merchant_id in members:
                    connection.execute(
                        """
                        INSERT INTO sre_network_signatures(
                            signature_id, incident_id, time_bucket, payment_method, provider, issuer, device,
                            error_family, deviation_ratio
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"SIG-{merchant.merchant_id.removeprefix('MRC-')}-SHARED-{cluster_index}",
                            incident_id,
                            INCIDENT_START.isoformat(),
                            shared_method,
                            "upi_collect" if shared_method == "upi" else "card_gateway",
                            "issuer_otp" if shared_error == "issuer_otp_degradation" else "mixed",
                            "mixed",
                            shared_error,
                            0.12,
                        ),
                    )
        connection.execute(
            """
            INSERT INTO sre_demo_runs(run_id, seed, merchant_count, event_count, incident_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                seed,
                len(DEMO_MERCHANTS),
                created_events,
                len(_INCIDENTS),
                datetime.now(UTC).isoformat(),
            ),
        )
    return {
        "merchants": len(DEMO_MERCHANTS),
        "events": created_events,
        "incidents": len(_INCIDENTS),
        "seed": seed,
        "scenario": str(scenario["name"]),
    }


def create_new_synthetic_run(database: Database) -> dict[str, int | str]:
    """Generate a new, recorded random seed while keeping the scenario guarantees intact."""

    with database.connect() as connection:
        previous = connection.execute(
            "SELECT scenario_index FROM sre_demo_run_state WHERE state_key = 'current'"
        ).fetchone()
    # Existing datasets predate scenario state and are assumed to be scenario zero.
    # Therefore the very first user-triggered generation moves to scenario one.
    scenario_index = (int(previous["scenario_index"]) + 1) % len(_SCENARIO_PACKS) if previous else 1
    seed = secrets.randbelow(2_000_000_000) + 1
    result = seed_synthetic_data(
        database,
        seed=seed,
        scenario_index=scenario_index,
    )
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO sre_demo_run_state(state_key, scenario_index, seed, updated_at)
            VALUES ('current', ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                scenario_index = excluded.scenario_index,
                seed = excluded.seed,
                updated_at = excluded.updated_at
            """,
            (scenario_index, seed, datetime.now(UTC).isoformat()),
        )
    return result


def current_demo_account_guide(database: Database) -> list[dict[str, str]]:
    """Return a downloadable, synthetic-only account guide for the current run."""

    passwords = {merchant.merchant_id: merchant.password for merchant in DEMO_MERCHANTS}
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT m.merchant_id, m.name, u.login_id, f.payment_method, f.error_family,
                   f.severity, f.baseline_success_rate, f.observed_success_rate
            FROM merchants AS m
            JOIN merchant_users AS u
              ON u.merchant_id = m.merchant_id AND u.role = 'merchant_owner'
            LEFT JOIN sre_anomaly_findings AS f
              ON f.merchant_id = m.merchant_id AND f.status = 'open'
            ORDER BY m.merchant_id, f.severity DESC, f.error_family
            """
        ).fetchall()
    guide: dict[str, dict[str, str]] = {}
    for row in rows:
        merchant_id = str(row["merchant_id"])
        entry = guide.setdefault(
            merchant_id,
            {
                "merchant_id": merchant_id,
                "company": str(row["name"]),
                "login_id": str(row["login_id"]),
                "password": passwords[merchant_id],
                "current_open_issues": "No open payment issue",
            },
        )
        if row["error_family"] is not None:
            issue = (
                f"{str(row['payment_method']).upper()} | {row['error_family']} | "
                f"{row['severity']} | {float(row['baseline_success_rate']):.0%} -> "
                f"{float(row['observed_success_rate']):.0%}"
            )
            if entry["current_open_issues"] == "No open payment issue":
                entry["current_open_issues"] = issue
            else:
                entry["current_open_issues"] += f"; {issue}"
    return list(guide.values())




def ensure_synthetic_data(database: Database) -> dict[str, int]:
    """Seed the fixed demo only once; never erase agent state on application restart."""

    with database.connect() as connection:
        event_count = int(
            connection.execute("SELECT COUNT(*) AS count FROM sre_payment_events").fetchone()["count"]
        )
    if event_count:
        return {
            "merchants": len(DEMO_MERCHANTS),
            "events": event_count,
            "incidents": len(_INCIDENTS),
        }
    return seed_synthetic_data(database)


def _insert_incident_window(
    connection,
    *,
    merchant_id: str,
    method: str,
    issuer: str,
    device: str,
    sdk_version: str,
    error_family: str,
    normal_variation: bool,
    failed_attempts: int,
    event_offset: int,
) -> int:
    """Insert a 30-attempt current window; the error label identifies the investigated cohort."""

    provider = "upi_collect" if method == "upi" else "card_gateway"
    for index in range(30):
        occurred_at = INCIDENT_START + timedelta(minutes=index)
        connection.execute(
            """
            INSERT INTO sre_payment_events(
                event_id, merchant_id, occurred_at, payment_method, provider, issuer, device,
                sdk_version, amount_paise, status, error_family, authorization_latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _event_id(merchant_id, occurred_at, event_offset + index),
                merchant_id,
                occurred_at.isoformat(),
                method,
                provider,
                issuer,
                device,
                sdk_version,
                2_000_00 + index * 100,
                "failed" if index < failed_attempts else "paid",
                error_family,
                1_300 if index < failed_attempts else 550,
            ),
        )
    return 30


def validate_synthetic_data(database: Database) -> dict[str, int | bool]:
    """Return machine-checkable seed quality evidence; never exposes ground truth through APIs."""

    with database.connect() as connection:
        merchants = connection.execute("SELECT COUNT(*) AS count FROM merchants").fetchone()[
            "count"
        ]
        users = connection.execute(
            "SELECT COUNT(*) AS count FROM merchant_users WHERE role = 'merchant_owner'"
        ).fetchone()["count"]
        events = connection.execute("SELECT COUNT(*) AS count FROM sre_payment_events").fetchone()[
            "count"
        ]
        incidents = connection.execute("SELECT COUNT(*) AS count FROM sre_incidents").fetchone()[
            "count"
        ]
        truth = connection.execute(
            "SELECT COUNT(*) AS count FROM sre_hidden_ground_truth"
        ).fetchone()["count"]
        network_clusters = connection.execute(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT error_family FROM sre_network_signatures s
                JOIN sre_incidents i ON i.incident_id = s.incident_id
                GROUP BY error_family HAVING COUNT(*) >= 3
            )
            """
        ).fetchone()["count"]
    return {
        "valid": merchants == 20
        and users == 20
        and events > 6_000
        and incidents == 20
        and truth == 20
        and network_clusters >= 2,
        "merchants": merchants,
        "merchant_users": users,
        "events": events,
        "incidents": incidents,
        "hidden_truth_records": truth,
        "network_clusters": network_clusters,
    }
