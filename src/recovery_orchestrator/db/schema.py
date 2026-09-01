"""SQLite schema for the local hackathon runtime."""

# ruff: noqa: E501

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    occurred_at_wall TEXT NOT NULL,
    occurred_at_sim TEXT,
    actor_type TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    decision TEXT,
    reason_codes_json TEXT NOT NULL,
    action_id TEXT,
    external_event_id TEXT,
    payload_json TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(case_id, sequence_no),
    UNIQUE(case_id, event_hash)
);

CREATE INDEX IF NOT EXISTS idx_audit_events_case
ON audit_events(case_id, sequence_no);

CREATE TABLE IF NOT EXISTS action_executions (
    action_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    reference_id TEXT UNIQUE,
    request_json TEXT NOT NULL,
    response_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_executions_case
ON action_executions(case_id, created_at);

CREATE TABLE IF NOT EXISTS external_events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK(signature_valid IN (0, 1)),
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    case_id TEXT,
    processing_error TEXT
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    reference_id TEXT NOT NULL UNIQUE,
    payment_link_id TEXT UNIQUE,
    payment_id TEXT,
    amount_due_paise INTEGER NOT NULL CHECK(amount_due_paise > 0),
    amount_paid_paise INTEGER NOT NULL DEFAULT 0 CHECK(amount_paid_paise >= 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES action_executions(action_id)
);

CREATE TABLE IF NOT EXISTS budget_state (
    budget_name TEXT PRIMARY KEY,
    cap_paise INTEGER NOT NULL CHECK(cap_paise >= 0),
    spent_paise INTEGER NOT NULL DEFAULT 0 CHECK(spent_paise >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    action_id TEXT PRIMARY KEY,
    budget_name TEXT NOT NULL,
    case_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
    status TEXT NOT NULL CHECK(status IN ('reserved', 'spent', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(budget_name) REFERENCES budget_state(budget_name),
    FOREIGN KEY(action_id) REFERENCES action_executions(action_id)
);

-- Revenue SRE multi-tenant foundation. These tables deliberately sit beside the
-- earlier recovery-demo tables so the existing demo remains runnable during the pivot.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sector TEXT NOT NULL,
    timezone TEXT NOT NULL,
    baseline_success_rate REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_users (
    user_id TEXT PRIMARY KEY,
    merchant_id TEXT,
    login_id TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('merchant_owner', 'razorpay_ops')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES merchant_users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_token
ON auth_sessions(token_hash);

CREATE TABLE IF NOT EXISTS merchant_profiles (
    merchant_id TEXT PRIMARY KEY,
    enabled_methods_json TEXT NOT NULL,
    platforms_json TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    average_order_value_paise INTEGER NOT NULL,
    monthly_attempts INTEGER NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS sre_payment_events (
    event_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    provider TEXT NOT NULL,
    issuer TEXT NOT NULL,
    device TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
    status TEXT NOT NULL CHECK(status IN ('paid', 'failed', 'pending')),
    error_family TEXT,
    authorization_latency_ms INTEGER NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE INDEX IF NOT EXISTS idx_sre_events_merchant_time
ON sre_payment_events(merchant_id, occurred_at);

CREATE TABLE IF NOT EXISTS sre_demo_runs (
    run_id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    merchant_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    incident_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sre_demo_run_state (
    state_key TEXT PRIMARY KEY,
    scenario_index INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sre_copilot_messages (
    message_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE INDEX IF NOT EXISTS idx_sre_copilot_messages_merchant
ON sre_copilot_messages(merchant_id, created_at);

CREATE TABLE IF NOT EXISTS sre_incidents (
    incident_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('customer', 'merchant', 'network', 'normal_variation')),
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    visible_evidence_json TEXT NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS sre_hidden_ground_truth (
    incident_id TEXT PRIMARY KEY,
    root_cause TEXT NOT NULL,
    expected_action TEXT NOT NULL,
    outcome_script TEXT NOT NULL,
    FOREIGN KEY(incident_id) REFERENCES sre_incidents(incident_id)
);

CREATE TABLE IF NOT EXISTS sre_network_signatures (
    signature_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    time_bucket TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    provider TEXT NOT NULL,
    issuer TEXT NOT NULL,
    device TEXT NOT NULL,
    error_family TEXT NOT NULL,
    deviation_ratio REAL NOT NULL,
    FOREIGN KEY(incident_id) REFERENCES sre_incidents(incident_id)
);

CREATE INDEX IF NOT EXISTS idx_sre_signatures_dimensions
ON sre_network_signatures(time_bucket, payment_method, provider, issuer, error_family);

CREATE TABLE IF NOT EXISTS sre_anomaly_findings (
    finding_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    time_bucket TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    provider TEXT NOT NULL,
    issuer TEXT NOT NULL,
    device TEXT NOT NULL,
    error_family TEXT NOT NULL,
    baseline_attempts INTEGER NOT NULL,
    recent_attempts INTEGER NOT NULL,
    baseline_success_rate REAL NOT NULL,
    observed_success_rate REAL NOT NULL,
    z_score REAL NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(merchant_id, time_bucket, payment_method, provider, error_family)
);

CREATE INDEX IF NOT EXISTS idx_sre_findings_merchant
ON sre_anomaly_findings(merchant_id, time_bucket DESC);

CREATE TABLE IF NOT EXISTS sre_detected_clusters (
    cluster_id TEXT PRIMARY KEY,
    time_bucket TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    provider TEXT NOT NULL,
    issuer TEXT NOT NULL,
    device TEXT NOT NULL,
    error_family TEXT NOT NULL,
    affected_merchant_count INTEGER NOT NULL,
    average_z_score REAL NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(time_bucket, payment_method, provider, issuer, device, error_family)
);

CREATE TABLE IF NOT EXISTS sre_investigations (
    investigation_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL UNIQUE,
    merchant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed', 'needs_review')),
    scope TEXT NOT NULL CHECK(scope IN ('customer', 'merchant', 'network')),
    root_cause_summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    proposed_action TEXT NOT NULL,
    approval_required INTEGER NOT NULL CHECK(approval_required IN (0, 1)),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(finding_id) REFERENCES sre_anomaly_findings(finding_id),
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS sre_agent_steps (
    step_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    question TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(investigation_id, sequence_no),
    FOREIGN KEY(investigation_id) REFERENCES sre_investigations(investigation_id)
);

CREATE INDEX IF NOT EXISTS idx_sre_investigations_merchant
ON sre_investigations(merchant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS sre_interventions (
    intervention_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL UNIQUE,
    merchant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'awaiting_approval', 'approved', 'executed', 'verified', 'replan_required',
        'rollback_required', 'rolled_back', 'rejected'
    )),
    policy_decision TEXT NOT NULL CHECK(policy_decision IN ('approved', 'needs_approval', 'blocked')),
    policy_reason TEXT NOT NULL,
    action_summary TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    executed_at TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(investigation_id) REFERENCES sre_investigations(investigation_id),
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS sre_intervention_measurements (
    intervention_id TEXT PRIMARY KEY,
    baseline_success_rate REAL NOT NULL,
    treated_success_rate REAL NOT NULL,
    holdout_success_rate REAL NOT NULL,
    affected_attempts INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('improved', 'no_improvement', 'negative_side_effect')),
    summary TEXT NOT NULL,
    FOREIGN KEY(intervention_id) REFERENCES sre_interventions(intervention_id)
);

CREATE TABLE IF NOT EXISTS sre_intervention_events (
    event_id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(intervention_id, sequence_no),
    FOREIGN KEY(intervention_id) REFERENCES sre_interventions(intervention_id)
);

CREATE INDEX IF NOT EXISTS idx_sre_interventions_merchant
ON sre_interventions(merchant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS notification_contacts (
    contact_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    team TEXT NOT NULL CHECK(team IN ('owner', 'payments', 'engineering', 'support')),
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    email_opt_in INTEGER NOT NULL DEFAULT 1 CHECK(email_opt_in IN (0, 1)),
    sms_opt_in INTEGER NOT NULL DEFAULT 0 CHECK(sms_opt_in IN (0, 1)),
    minimum_severity TEXT NOT NULL DEFAULT 'high',
    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(merchant_id, team, email),
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS notification_rules (
    rule_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    recipient_team TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('email', 'sms')),
    minimum_severity TEXT NOT NULL,
    cooldown_minutes INTEGER NOT NULL DEFAULT 60 CHECK(cooldown_minutes >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(merchant_id, event_type, recipient_team, channel),
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    intervention_id TEXT,
    recipient_contact_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('email', 'sms')),
    status TEXT NOT NULL CHECK(status IN ('simulated_delivered', 'suppressed_duplicate', 'suppressed_cooldown')),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id),
    FOREIGN KEY(intervention_id) REFERENCES sre_interventions(intervention_id),
    FOREIGN KEY(recipient_contact_id) REFERENCES notification_contacts(contact_id)
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_merchant
ON notification_outbox(merchant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_live_attempts (
    attempt_id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('live_sent', 'live_failed', 'not_configured')),
    provider_message_id TEXT,
    safe_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(notification_id) REFERENCES notification_outbox(notification_id)
);

CREATE TABLE IF NOT EXISTS whatsapp_inbound_events (
    event_id TEXT PRIMARY KEY,
    merchant_id TEXT,
    notification_id TEXT,
    from_address TEXT NOT NULL,
    body TEXT NOT NULL,
    provider_message_id TEXT NOT NULL UNIQUE,
    verification_status TEXT NOT NULL CHECK(verification_status IN ('verified', 'local_demo')),
    received_at TEXT NOT NULL,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id),
    FOREIGN KEY(notification_id) REFERENCES notification_outbox(notification_id)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_inbound_events_merchant
ON whatsapp_inbound_events(merchant_id, received_at DESC);

CREATE TABLE IF NOT EXISTS customer_recovery_batches (
    batch_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    investigation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'approved', 'sent', 'completed')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    sent_at TEXT,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id),
    FOREIGN KEY(finding_id) REFERENCES sre_anomaly_findings(finding_id),
    FOREIGN KEY(investigation_id) REFERENCES sre_investigations(investigation_id),
    UNIQUE(merchant_id, finding_id)
);

CREATE TABLE IF NOT EXISTS customer_recovery_recipients (
    recipient_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    customer_label TEXT NOT NULL,
    whatsapp_address TEXT NOT NULL,
    link_token TEXT NOT NULL UNIQUE,
    message_body TEXT,
    provider_message_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft', 'sent', 'opened', 'completed', 'follow_up_sent')),
    link_opened_at TEXT,
    payment_completed_at TEXT,
    follow_up_due_at TEXT,
    follow_up_message_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES customer_recovery_batches(batch_id),
    UNIQUE(batch_id, whatsapp_address)
);

CREATE INDEX IF NOT EXISTS idx_customer_recovery_batches_merchant
ON customer_recovery_batches(merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_customer_recovery_recipients_due
ON customer_recovery_recipients(status, follow_up_due_at);
"""
