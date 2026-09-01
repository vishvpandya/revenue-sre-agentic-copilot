"""Three-panel, merchant-first Revenue SRE dashboard backed by FastAPI."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import os
from contextlib import suppress
from datetime import UTC, datetime
from html import escape
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import streamlit as st

DEFAULT_BACKEND = "http://127.0.0.1:8017"
IST = ZoneInfo("Asia/Kolkata")
CRITICAL_SUCCESS_RATE_THRESHOLD = 0.80
CRITICAL_DROP_THRESHOLD = 0.20
ENGINEERING_ESCALATION_ERRORS = {
    "sdk_regression",
    "checkout_javascript",
    "android_upi_redirect",
    "mobile_redirect",
}


def api(method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
    headers = kwargs.pop("headers", {})
    if token:
        headers = {**headers, "Authorization": f"Bearer {token}"}
    try:
        response = httpx.request(
            method,
            f"{st.session_state.backend_url.rstrip('/')}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if response.content else None
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        with suppress(ValueError, AttributeError):
            detail = exc.response.json().get("detail", detail)
        raise RuntimeError(str(detail)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Backend is not reachable. Start FastAPI first.") from exc


def configured_backend_url() -> str:
    """Use local FastAPI by default and a secret Cloud URL when it is configured."""

    if value := os.getenv("BACKEND_URL"):
        return value
    try:
        if value := st.secrets.get("BACKEND_URL"):
            return str(value)
    except (FileNotFoundError, KeyError):
        pass
    return DEFAULT_BACKEND


def pct(value: float | None) -> str:
    return f"{(value or 0):.0%}"


def human(value: str) -> str:
    names = {
        "upi_provider_degradation": "UPI provider issue",
        "issuer_otp_degradation": "Bank OTP verification issue",
        "sdk_regression": "recent payment setup issue",
        "checkout_javascript": "checkout page issue",
        "normal_variation": "normal payment variation",
        "awaiting_approval": "Needs your approval",
        "rollback_required": "Rollback required",
        "rolled_back": "Rolled back safely",
    }
    return names.get(value, value.replace("_", " ").capitalize())


def concise_time(value: str | None) -> str:
    if not value:
        return "Recorded"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(IST).strftime("%H:%M IST")


def severity_class(severity: str) -> str:
    return "severity-critical" if severity == "critical" else "severity-watch"


def section_kicker(label: str) -> None:
    st.markdown(f"<div class='section-kicker'>{escape(label)}</div>", unsafe_allow_html=True)


def workflow_guide() -> None:
    st.markdown(
        """
        <div class="workflow-guide">
          <div><span>01</span><strong>Detect</strong><small>Find an abnormal payment drop</small></div>
          <i>→</i>
          <div><span>02</span><strong>Investigate</strong><small>Check evidence and scope</small></div>
          <i>→</i>
          <div><span>03</span><strong>Decide safely</strong><small>Request approval when needed</small></div>
          <i>→</i>
          <div><span>04</span><strong>Recover & measure</strong><small>Prove the outcome</small></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def account_guide_csv(accounts: list[dict[str, str]]) -> str:
    """Create a demo-only CSV of the current synthetic accounts and issues."""

    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["merchant_id", "company", "login_id", "password", "current_open_issues"],
    )
    writer.writeheader()
    writer.writerows(accounts)
    return output.getvalue()


def login() -> None:
    st.markdown(
        "<div class='top-product-bar'><span class='brand-orb'>R</span><span>REVENUE SRE</span>"
        "<em>PAYMENT RELIABILITY COMMAND CENTER</em><b>DEMO</b></div>",
        unsafe_allow_html=True,
    )
    intro, access = st.columns([1.15, 0.85], gap="large")
    with intro:
        section_kicker("Payment operations, made explainable")
        st.markdown(
            "<div class='login-hero'><h1>Know what failed.<br><span>Recover with proof.</span></h1>"
            "<p>Revenue SRE detects payment problems, investigates the evidence, and keeps every "
            "customer-impacting change under your control.</p></div>",
            unsafe_allow_html=True,
        )
        workflow_guide()
        st.markdown(
            "<div class='trust-row'><span>◉ Evidence first</span><span>◉ Human approval</span>"
            "<span>◉ Measured outcomes</span></div>",
            unsafe_allow_html=True,
        )
    with access:
        st.markdown("<div class='access-card-title'>Merchant workspace</div>", unsafe_allow_html=True)
        st.caption("Sign in to see one merchant's private payment health.")
        with st.form("login"):
            login_id = st.text_input("Demo login ID", value="strideworks@demo.revenuesre.local")
            password = st.text_input("Demo password", value="Stride#Demo01", type="password")
            submitted = st.form_submit_button("Open merchant workspace", type="primary", width="stretch")
    if submitted:
        try:
            result = api(
                "POST", "/revenue-sre/auth/login", json={"login_id": login_id, "password": password}
            )
            st.session_state.token, st.session_state.user = result["access_token"], result["user"]
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))
    st.caption("Operations demo: `ops@demo.revenuesre.local` · `RazorpayOps#Demo`")
    with st.expander("Demo control: create new synthetic data", expanded=False):
        st.caption(
            "Creates a new complete payment-data run for all 20 merchants and clears old agent decisions and demo alerts. "
            "Merchant login accounts stay the same."
        )
        with st.form("create-synthetic-data"):
            ops_login = st.text_input("Operations login ID", value="ops@demo.revenuesre.local")
            ops_password = st.text_input(
                "Operations password",
                value="RazorpayOps#Demo",
                type="password",
            )
            generate = st.form_submit_button(
                "Create new synthetic data for 20 merchants", type="primary", width="stretch"
            )
        if generate:
            try:
                result = api(
                    "POST",
                    "/revenue-sre/demo-runs/create",
                    json={"login_id": ops_login, "password": ops_password},
                )
                run = result["run"]
                st.session_state.synthetic_account_guide = result.get("account_guide", [])
                st.success(
                    f"Created fresh data for {run['merchants']} merchants: {run['events']} payment events and {run['incidents']} incidents. "
                    "Sign in again to begin the new demo run."
                )
                st.info(f"Scenario: {run['scenario']} · Demo seed: {run['seed']}")
            except RuntimeError as exc:
                st.error(str(exc))
    account_guide = st.session_state.get("synthetic_account_guide", [])
    if account_guide:
        st.download_button(
            "Download current demo accounts and issues (CSV)",
            data=account_guide_csv(account_guide),
            file_name="revenue-sre-current-demo-accounts.csv",
            mime="text/csv",
            help="Synthetic demo accounts and passwords only. Never use with real merchant accounts.",
        )
def sign_out() -> None:
    with suppress(RuntimeError):
        api("POST", "/revenue-sre/auth/logout", token=st.session_state.token)
    for key in (
        "token",
        "user",
        "copilot_messages",
        "merchant_view",
        "last_critical_email_result",
    ):
        st.session_state.pop(key, None)
    st.rerun()


def is_sidebar_critical(finding: dict[str, Any]) -> bool:
    """Only show the urgent sidebar control for a materially severe payment drop."""

    drop = float(finding["baseline_success_rate"]) - float(finding["observed_success_rate"])
    return (
        finding["severity"] == "critical"
        and finding["error_family"] in ENGINEERING_ESCALATION_ERRORS
        and drop >= CRITICAL_DROP_THRESHOLD
        and float(finding["observed_success_rate"]) < CRITICAL_SUCCESS_RATE_THRESHOLD
    )


def merchant_navigation(
    token: str,
    merchant: dict[str, Any],
    findings: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
) -> str:
    st.markdown(
        "<div class='rail-brand'><span class='brand-orb'>R</span><div><strong>REVENUE SRE</strong>"
        "<small>Merchant workspace</small></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='merchant-name'>{escape(str(merchant['name']))}</div>",
        unsafe_allow_html=True,
    )
    st.caption("Private payment operations view")
    critical = next((item for item in findings if is_sidebar_critical(item)), None)
    if critical is not None:
        st.markdown(
            "<div class='critical-alert'>🚨 CRITICAL ALERT — send engineering email</div>",
            unsafe_allow_html=True,
        )
        try:
            route = api(
                "GET",
                f"/revenue-sre/merchant/critical-findings/{critical['finding_id']}/engineering-route",
                token=token,
            )
            st.caption(
                f"Route: {route['recipient_team'].title()} — {route['recipient_name']} "
                f"({route['recipient_email']})"
            )
            st.caption(f"Why: {route['reason']}")
        except RuntimeError as exc:
            st.caption(f"Routing needs attention: {exc}")
        last_result = st.session_state.get("last_critical_email_result")
        if last_result and last_result.get("finding_id") == critical["finding_id"]:
            if last_result.get("status") == "live_sent":
                st.success("Email sent to the configured Resend test inbox. Open Alerts for its delivery record.")
            elif last_result.get("replayed"):
                st.info("This critical alert was already sent once; a duplicate was prevented.")
            else:
                st.warning(last_result.get("safe_error") or "The critical email was not sent.")
        if st.button("Send critical email now", type="primary", key="sidebar-critical-email"):
            try:
                result = api(
                    "POST",
                    f"/revenue-sre/merchant/critical-findings/{critical['finding_id']}/send-urgent-email",
                    token=token,
                )
                st.session_state.critical_email_result = result
                st.session_state.last_critical_email_result = {
                    **result,
                    "finding_id": critical["finding_id"],
                }
                st.session_state.workspace_view = "Alerts"
                st.rerun()
            except RuntimeError as exc:
                st.session_state.critical_email_result = {
                    "status": "not_sent",
                    "safe_error": str(exc),
                }
                st.session_state.last_critical_email_result = {
                    "status": "not_sent",
                    "safe_error": str(exc),
                    "finding_id": critical["finding_id"],
                }
                st.session_state.workspace_view = "Alerts"
                st.rerun()
    view = st.radio(
        "Workspace",
        ["Today", "Ask Copilot", "Alerts", "Team settings"],
        key="workspace_view",
        label_visibility="collapsed",
    )
    pending = [item for item in interventions if item["status"] == "awaiting_approval"]
    if pending:
        st.warning(f"{len(pending)} decision(s) need your answer")
    section_kicker("Current attention")
    if not findings:
        st.success("No payment issue needs attention.")
    for finding in findings:
        st.markdown(
            f"<div class='rail-issue {severity_class(str(finding['severity']))}'>"
            f"<strong>{escape(human(str(finding['error_family'])))}</strong>"
            f"<span>{escape(str(finding['severity']).title())} · "
            f"{pct(float(finding['baseline_success_rate']))} → "
            f"{pct(float(finding['observed_success_rate']))}</span></div>",
            unsafe_allow_html=True,
        )
    st.divider()
    if st.button("Sign out", width="stretch"):
        sign_out()
    return view


def decision_card(token: str, interventions: list[dict[str, Any]]) -> None:
    pending = next((item for item in interventions if item["status"] == "awaiting_approval"), None)
    if pending is None:
        return
    with st.container(border=True):
        st.markdown("### Decision needed")
        st.write(pending["action_summary"])
        st.caption(f"Why it is safe: {pending['policy_reason']}")
        approve, reject = st.columns(2)
        if approve.button(
            "Approve safe action",
            type="primary",
            key=f"approve-{pending['intervention_id']}",
            width="stretch",
        ):
            api(
                "POST",
                f"/revenue-sre/merchant/interventions/{pending['intervention_id']}/approval",
                token=token,
                json={"approve": True},
            )
            st.rerun()
        if reject.button(
            "Keep current setup", key=f"reject-{pending['intervention_id']}", width="stretch"
        ):
            api(
                "POST",
                f"/revenue-sre/merchant/interventions/{pending['intervention_id']}/approval",
                token=token,
                json={"approve": False},
            )
            st.rerun()


def recovery_progress(token: str, interventions: list[dict[str, Any]]) -> None:
    """Make the approval-gated recovery lifecycle legible to a non-technical owner."""

    intervention = next((item for item in interventions if item["status"] != "rejected"), None)
    if intervention is None:
        return
    status = intervention["status"]
    with st.container(border=True):
        st.markdown("### Recovery progress")
        st.caption(
            "Every stage is recorded. No payment change can run without your explicit approval."
        )
        st.write(f"**Proposed action:** {intervention['action_summary']}")
        stages = [
            ("Review", True),
            (
                "Approve",
                status
                in {
                    "approved",
                    "executed",
                    "verified",
                    "replan_required",
                    "rollback_required",
                    "rolled_back",
                },
            ),
            (
                "Run safely",
                status
                in {"executed", "verified", "replan_required", "rollback_required", "rolled_back"},
            ),
            (
                "Measure",
                status in {"verified", "replan_required", "rollback_required", "rolled_back"},
            ),
        ]
        st.caption(
            "  →  ".join(f"{'✓' if complete else '○'} {label}" for label, complete in stages)
        )
        if status == "awaiting_approval":
            st.info(
                "Waiting for your approval above. The system has not changed the payment setup."
            )
        elif status == "approved":
            st.success("You approved this bounded demo action. It is ready to run once.")
            if st.button(
                "Run approved demo action", key=f"execute-{intervention['intervention_id']}"
            ):
                api(
                    "POST",
                    f"/revenue-sre/merchant/interventions/{intervention['intervention_id']}/execute",
                    token=token,
                )
                st.rerun()
        elif status == "executed":
            st.info(
                "The approved demo action ran once. Measure it against an unchanged comparison group."
            )
            if st.button(
                "Measure payment outcome", key=f"verify-{intervention['intervention_id']}"
            ):
                api(
                    "POST",
                    f"/revenue-sre/merchant/interventions/{intervention['intervention_id']}/verify",
                    token=token,
                )
                st.rerun()
        elif status == "rollback_required":
            st.error("Measurement found a worse outcome. A safe rollback is required.")
            if st.button(
                "Roll back the demo action", key=f"rollback-{intervention['intervention_id']}"
            ):
                api(
                    "POST",
                    f"/revenue-sre/merchant/interventions/{intervention['intervention_id']}/rollback",
                    token=token,
                )
                st.rerun()
        else:
            measurement = intervention.get("measurement")
            if measurement:
                st.success(f"Measured outcome: {human(str(measurement['outcome']))}.")
                result = st.columns(3)
                result[0].metric(
                    "Normal baseline", pct(float(measurement["baseline_success_rate"]))
                )
                result[1].metric(
                    "After safe action", pct(float(measurement["treated_success_rate"]))
                )
                result[2].metric(
                    "Unchanged comparison", pct(float(measurement["holdout_success_rate"]))
                )
                st.write(measurement["summary"])
                improvement = max(
                    0.0,
                    float(measurement["treated_success_rate"])
                    - float(measurement["holdout_success_rate"]),
                )
                affected_attempts = int(measurement["affected_attempts"])
                additional_successes = round(improvement * affected_attempts)
                st.info(
                    f"**Estimated impact:** compared with leaving the affected group unchanged, "
                    f"this safe action enabled about **{additional_successes} additional successful "
                    f"payments** across {affected_attempts} affected attempts "
                    f"({improvement:.0%} higher success)."
                )
                if float(measurement["treated_success_rate"]) < float(
                    measurement["baseline_success_rate"]
                ):
                    st.info(
                        "Payments improved, but have not yet returned to the normal baseline. Monitoring continues."
                    )
            elif status == "rolled_back":
                st.warning("The demo action was rolled back safely after measurement.")


def customer_recovery_panel(
    token: str, findings: list[dict[str, Any]], investigations: list[dict[str, Any]]
) -> None:
    """Small approval-gated UPI fallback campaign for Sandbox test customers."""

    confirmed = {item["finding_id"]: item for item in investigations if item["scope"] == "network"}
    candidate = next(
        (
            item
            for item in findings
            if item["payment_method"] == "upi" and item["finding_id"] in confirmed
        ),
        None,
    )
    if candidate is None:
        return
    st.markdown("### Customer recovery for UPI outage")
    st.caption(
        "A separate agent can prepare up to three Sandbox customer messages. One approval sends the whole "
        "small batch; no real money is collected by the test links."
    )
    try:
        batch = api("GET", "/revenue-sre/merchant/customer-recovery", token=token)["batch"]
    except RuntimeError as exc:
        st.warning(str(exc))
        return
    if batch is None:
        if st.button(
            "Prepare Gemini customer-recovery messages",
            key=f"prepare-customers-{candidate['finding_id']}",
        ):
            try:
                api(
                    "POST",
                    f"/revenue-sre/merchant/customer-recovery/{candidate['finding_id']}/prepare",
                    token=token,
                )
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
        return
    with st.container(border=True):
        st.markdown(f"**Customer-recovery batch · {human(batch['status'])}**")
        drafts_ready = all(bool(recipient.get("message_body")) for recipient in batch["recipients"])
        for recipient in batch["recipients"]:
            st.markdown(f"**{recipient['customer_label']}** · {human(recipient['status'])}")
            if recipient.get("message_body"):
                st.write(recipient["message_body"])
            if recipient.get("link_opened_at"):
                st.caption(f"Test link opened · {concise_time(recipient['link_opened_at'])}")
            if recipient.get("payment_completed_at"):
                st.success("Test payment marked complete — no real money collected.")
            elif recipient["status"] == "opened":
                st.info(
                    "Link opened. Gemini will send one helpful follow-up after five minutes if payment is not marked complete."
                )
            elif recipient["status"] == "follow_up_sent":
                st.info(
                    "Gemini follow-up was sent after the five-minute open-without-completion window."
                )
        if batch["status"] == "draft" and not drafts_ready:
            st.warning("Gemini drafts are not ready yet. No customer message has been sent.")
            if st.button(
                "Generate or retry Gemini drafts",
                key=f"retry-customer-drafts-{batch['batch_id']}",
            ):
                try:
                    api(
                        "POST",
                        f"/revenue-sre/merchant/customer-recovery/{candidate['finding_id']}/prepare",
                        token=token,
                    )
                    st.rerun()
                except RuntimeError as exc:
                    st.error(f"Gemini could not prepare the customer drafts: {exc}")
        if (
            batch["status"] == "draft"
            and drafts_ready
            and st.button(
                "Approve and send this customer batch",
                type="primary",
                key=f"send-customer-batch-{batch['batch_id']}",
            )
        ):
            try:
                result = api(
                    "POST",
                    f"/revenue-sre/merchant/customer-recovery/{batch['batch_id']}/approve-and-send",
                    token=token,
                )
                errors = result.get("send_errors") or []
                if errors:
                    st.warning("Some Sandbox messages were not accepted: " + " ".join(errors))
                else:
                    st.success(
                        "The approved test customer messages were sent. Open each WhatsApp test link to continue the demo."
                    )
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
        if batch["status"] in {"sent", "completed"}:
            delivery_button_key = f"customer-delivery-check-{batch['batch_id']}"
            delivery_results_key = f"customer-delivery-results-{batch['batch_id']}"
            if st.button("Check WhatsApp delivery", key=delivery_button_key):
                try:
                    result = api(
                        "GET",
                        f"/revenue-sre/merchant/customer-recovery/{batch['batch_id']}/delivery-status",
                        token=token,
                    )
                    st.session_state[delivery_results_key] = result["recipients"]
                except RuntimeError as exc:
                    st.error(str(exc))
            for delivery in st.session_state.get(delivery_results_key, []):
                label = str(delivery["customer_label"])
                status = str(delivery["status"])
                if status == "delivered":
                    st.success(f"{label}: WhatsApp delivered.")
                elif status in {"failed", "undelivered"}:
                    st.error(
                        f"{label}: WhatsApp was not delivered (Twilio code {delivery.get('error_code')}). "
                        "From that phone, message the Twilio Sandbox to open its 24-hour WhatsApp session, then retry."
                    )
                    if st.button(
                        f"Retry {label} only",
                        key=f"retry-customer-{batch['batch_id']}-{delivery['recipient_id']}",
                    ):
                        try:
                            retry = api(
                                "POST",
                                f"/revenue-sre/merchant/customer-recovery/{batch['batch_id']}/recipients/{delivery['recipient_id']}/retry",
                                token=token,
                            )
                            if retry["status"] == "accepted":
                                st.success(f"Twilio accepted a retry for {label}. Check delivery again in a moment.")
                            else:
                                st.warning(str(retry.get("safe_error") or "Twilio did not accept the retry."))
                        except RuntimeError as exc:
                            st.error(str(exc))
                elif status in {"queued", "sent", "accepted"}:
                    st.info(f"{label}: Twilio status is {status}; delivery is still being processed.")
                else:
                    st.warning(f"{label}: delivery status is {status}.")


def live_customer_recovery_tracker(token: str) -> None:
    """Poll link state so opening/completing a test link appears without a full refresh."""

    @st.fragment(run_every="5s")
    def tracking_feed() -> None:
        try:
            batch = api("GET", "/revenue-sre/merchant/customer-recovery", token=token)["batch"]
        except RuntimeError:
            return
        if batch is None or batch["status"] not in {"sent", "completed"}:
            return
        st.markdown("#### Customer-recovery agent tracking")
        st.caption(
            "Live updates every 5 seconds. Every unfinished test customer receives one Gemini follow-up five minutes after the first message is sent."
        )
        for recipient in batch["recipients"]:
            status = recipient["status"]
            countdown = ""
            if status in {"sent", "opened"} and recipient.get("follow_up_due_at"):
                due_at = datetime.fromisoformat(
                    recipient["follow_up_due_at"].replace("Z", "+00:00")
                )
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=UTC)
                remaining = max(0, int((due_at - datetime.now(UTC)).total_seconds()))
                countdown = f" · Gemini follow-up in {remaining // 60}:{remaining % 60:02d}"
            if status == "sent":
                st.write(
                    f"🟡 {recipient['customer_label']}: Twilio accepted the message; waiting for the test link to open.{countdown}"
                )
            elif status == "opened":
                st.write(
                    f"🔵 {recipient['customer_label']}: link opened; payment not yet completed.{countdown}"
                )
            elif status == "follow_up_sent":
                st.write(
                    f"🟠 {recipient['customer_label']}: opened without completion; Gemini follow-up sent."
                )
            elif status == "completed":
                st.write(f"🟢 {recipient['customer_label']}: test payment completed.")

    tracking_feed()


def issue_cards(
    token: str,
    findings: list[dict[str, Any]],
    investigations: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
) -> None:
    section_kicker("Payment health")
    st.markdown("## Issues requiring attention")
    st.caption(
        "Start with the evidence. The system does not make merchant-facing changes without permission."
    )
    if not findings:
        st.success(
            "Payment health is within the normal range. The system has correctly taken no action."
        )
        return
    investigated = {item["finding_id"]: item for item in investigations}
    requested_for = {item["investigation_id"] for item in interventions}
    for finding in findings:
        with st.container(border=True):
            st.markdown(
                f"<span class='severity-chip {severity_class(str(finding['severity']))}'>"
                f"{escape(str(finding['severity']).upper())}</span>",
                unsafe_allow_html=True,
            )
            st.markdown(f"#### {human(finding['error_family'])}")
            st.write(
                f"Payment success moved from **{pct(finding['baseline_success_rate'])}** to "
                f"**{pct(finding['observed_success_rate'])}** in this affected payment group."
            )
            st.caption(
                f"{finding['recent_attempts']} attempts · {finding['payment_method'].upper()} · {finding['device']}"
            )
            investigation = investigated.get(finding["finding_id"])
            intervention = next(
                (
                    item
                    for item in interventions
                    if investigation is not None
                    and item["investigation_id"] == investigation["investigation_id"]
                ),
                None,
            )
            if intervention and intervention.get("measurement"):
                measurement = intervention["measurement"]
                st.success(
                    f"Recovery is verified: {pct(float(measurement['treated_success_rate']))} after the safe "
                    f"action versus {pct(float(measurement['holdout_success_rate']))} in the unchanged group."
                )
            if investigation is None:
                if st.button(
                    "Ask agents to investigate",
                    key=f"investigate-{finding['finding_id']}",
                    type="primary",
                ):
                    result = api(
                        "POST",
                        f"/revenue-sre/merchant/investigations/{finding['finding_id']}/run",
                        token=token,
                    )
                    automatic_email = result.get("automatic_engineering_email")
                    if automatic_email and automatic_email.get("status") == "live_sent":
                        st.toast(
                            "Critical engineering alert was drafted by Gemini and sent automatically.",
                            icon="📧",
                        )
                    elif automatic_email:
                        st.toast(
                            automatic_email.get("safe_error")
                            or "Automatic engineering email was not sent.",
                            icon="⚠️",
                        )
                    st.rerun()
            elif investigation["investigation_id"] not in requested_for:
                st.caption(f"Agent recommendation: {investigation['proposed_action']}")
                if st.button(
                    "Request a safe, approval-gated action",
                    key=f"request-{investigation['investigation_id']}",
                ):
                    api(
                        "POST",
                        f"/revenue-sre/merchant/interventions/{investigation['investigation_id']}/request",
                        token=token,
                    )
                    st.rerun()


def copilot_panel(token: str, brief: dict[str, Any]) -> None:
    section_kicker("Ask the evidence")
    st.markdown("## Revenue SRE Copilot")
    st.caption(
        "Gemini explains only your current merchant's recorded evidence. It cannot approve or change payments."
    )
    messages = st.session_state.setdefault("copilot_messages", [])
    if not messages:
        messages.extend(
            api("GET", "/revenue-sre/merchant/copilot/history", token=token)["messages"]
        )
    if not messages:
        messages.append({"role": "assistant", "content": f"{brief['headline']} {brief['detail']}"})
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("gemini_diagnostic"):
                st.caption(f"Gemini diagnostic: {message['gemini_diagnostic']}")
    question = st.chat_input("Message Revenue SRE Copilot")
    if question:
        messages.append({"role": "user", "content": question})
        try:
            answer = api(
                "POST", "/revenue-sre/merchant/copilot", token=token, json={"question": question}
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": answer["answer"],
                    "gemini_diagnostic": (
                        answer.get("model_error")
                        if answer.get("model_used") == "gemini_unavailable"
                        else None
                    ),
                }
            )
            if answer.get("model_used") == "gemini":
                st.toast("Gemini explained the current merchant evidence.", icon="✨")
            elif answer.get("model_used") == "gemini_unavailable":
                st.toast("Gemini could not be reached. No template answer was used.", icon="⚠️")
            elif answer.get("model_error"):
                st.toast(
                    "Gemini was unavailable, so Copilot used its fact-based fallback.", icon="ℹ️"
                )
        except RuntimeError as exc:
            messages.append(
                {"role": "assistant", "content": f"I could not check that right now: {exc}"}
            )
        st.rerun()


def investigation_context(step: dict[str, Any]) -> tuple[str, str]:
    """Turn a stored agent observation into an explanation for a business owner."""

    observation = step["observation"]
    agent = step["agent_name"]
    if agent == "Scope Investigator":
        baseline = pct(float(observation["baseline_success_rate"]))
        current = pct(float(observation["observed_success_rate"]))
        return (
            "Job: checks whether this is a real payment problem, rather than normal day-to-day variation.",
            f"Checked {observation['attempts']} relevant payment attempts. Your normal success rate is {baseline}; this group is now at {current}, with {observation['failed_attempts']} failed payments. That drop is large enough to investigate.",
        )
    if agent == "Root-Cause Investigator":
        if observation["network_pattern_found"]:
            return (
                "Job: checks anonymized signals from other merchants to see whether the payment network is also affected.",
                f"The same pattern appears across {observation['affected_merchant_count']} merchants. This points to a wider payment-network issue, not only this business's website.",
            )
        return (
            "Job: checks whether other merchants show the same pattern before blaming your website.",
            "No matching pattern was found across other merchants. That makes this more likely to be a problem in this merchant's own checkout setup.",
        )
    if agent == "Stakeholder Reply Monitor":
        return (
            "Job: records and explains a signed reply from the responsible team. It cannot approve or change payments.",
            step["conclusion"],
        )
    version = observation.get("sdk_version", "the current payment setup")
    return (
        "Job: proposes the smallest safe next step. It cannot change your checkout by itself.",
        f"The affected payments use {observation.get('payment_method', 'this payment method').upper()} on {observation.get('device', 'the affected device')}, with checkout setup version {version}. Proposed next step: {step['conclusion']}",
    )


def intervention_context(event: dict[str, Any]) -> tuple[str, str, str]:
    """Explain the safety controls in plain language instead of backend event names."""

    event_type, details = event["event_type"], event["details"]
    if event_type == "policy_evaluated":
        decision = details.get("decision", "unknown")
        return (
            "Safety & approval check",
            "Job: checks whether the proposed change is safe to run automatically or needs your permission.",
            f"Outcome: {details.get('reason', 'The policy was checked.')} Decision: {human(str(decision))}. No payment setup was changed at this stage.",
        )
    if event_type == "merchant_approval_recorded":
        approved = bool(details.get("approved"))
        outcome = (
            "You approved the proposed action."
            if approved
            else "You chose not to make the proposed change."
        )
        return (
            "Merchant decision",
            "Job: records the merchant owner's choice before any merchant-facing change can run.",
            outcome,
        )
    if event_type == "bounded_action_executed":
        return (
            "Bounded Executor",
            "Job: carries out only the already-approved action and protects against running the same action twice.",
            "Outcome: the approved demo action was executed once. The next step is to measure whether payment success improved.",
        )
    if event_type == "treated_vs_holdout_verified":
        return (
            "Results Verifier",
            "Job: compares the changed payment group with a similar unchanged group, so it does not claim success without evidence.",
            f"Outcome: {human(str(details.get('outcome', 'measured')))}. Changed group success: {pct(float(details.get('treated_success_rate', 0)))}; unchanged comparison: {pct(float(details.get('holdout_success_rate', 0)))}.",
        )
    if event_type == "rollback_completed":
        return (
            "Safety Rollback Controller",
            "Job: returns the system to the safer prior state when verification shows the action made payments worse.",
            "Outcome: the demo action was rolled back safely.",
        )
    return (
        human(event_type),
        "Job: records an audited step in the payment-recovery workflow.",
        "Outcome recorded.",
    )


def agent_timeline(
    investigations: list[dict[str, Any]], interventions: list[dict[str, Any]]
) -> None:
    section_kicker("Live audit trail")
    st.markdown("### Agent activity")
    st.caption(
        "Evidence-based specialist workflow · Gemini explains and drafts; policy controls decisions."
    )
    if not investigations and not interventions:
        st.info("No agent has been asked to investigate yet.")
        return
    entries: list[tuple[str, str, str, str]] = []
    for investigation in investigations[:2]:
        for step in investigation["agent_trace"]:
            job, explanation = investigation_context(step)
            entries.append(
                (str(step.get("created_at") or ""), step["agent_name"], job, explanation)
            )
    for intervention in interventions[:2]:
        for event in intervention["events"]:
            title, job, explanation = intervention_context(event)
            entries.append((str(event.get("created_at") or ""), title, job, explanation))
    for timestamp, title, job, explanation in sorted(entries, key=lambda item: item[0]):
        st.markdown(
            f"<div class='timeline-event'><div><span class='timeline-dot'></span>"
            f"<b>{escape(concise_time(timestamp))}</b></div><strong>{escape(title)}</strong>"
            f"<small>{escape(job)}</small><p>{escape(explanation)}</p></div>",
            unsafe_allow_html=True,
        )


def live_agent_timeline(token: str) -> None:
    """Refresh the audit trail independently when a signed WhatsApp reply arrives."""

    @st.fragment(run_every="5s")
    def timeline_feed() -> None:
        investigations = api("GET", "/revenue-sre/merchant/investigations", token=token)[
            "investigations"
        ]
        interventions = api("GET", "/revenue-sre/merchant/interventions", token=token)[
            "interventions"
        ]
        agent_timeline(investigations, interventions)

    timeline_feed()


def live_whatsapp_replies(token: str) -> None:
    """Show real sandbox replies without rerunning the rest of the merchant workspace."""

    @st.fragment(run_every="5s")
    def reply_feed() -> None:
        replies = api("GET", "/revenue-sre/merchant/whatsapp-replies", token=token)["replies"]
        st.markdown("### WhatsApp replies received")
        st.caption(
            "Live updates every 5 seconds. Signed Twilio webhook events are recorded as evidence for this merchant."
        )
        if not replies:
            st.info("Waiting for a WhatsApp reply from your configured test number.")
            return
        for reply in replies:
            with st.container(border=True):
                st.markdown(f"**Reply received · {concise_time(reply['received_at'])}**")
                st.write(reply["body"])
                st.info(reply["interpretation"])
                st.caption(
                    f"Verification: {reply['verification_status']} · "
                    f"Twilio message ID: {reply['provider_message_id']}"
                )

    reply_feed()


def email_compose_controls(
    token: str, notification: dict[str, Any], *, urgent: bool = False
) -> None:
    """Draft first, then require a separate confirmation for controlled email delivery."""

    notification_id = notification["notification_id"]
    draft_key = f"email-draft-{notification_id}"
    if st.button(
        "Generate urgent Gemini email draft" if urgent else "Draft email with Gemini",
        key=f"draft-email-{notification_id}",
    ):
        try:
            st.session_state[draft_key] = api(
                "POST",
                f"/revenue-sre/merchant/notifications/{notification_id}/draft-email",
                token=token,
            )
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))
    draft = st.session_state.get(draft_key)
    if not draft:
        return
    if draft.get("model_used") == "gemini":
        st.success(
            "Gemini drafted this email from the current merchant evidence. Review it before sending."
        )
    elif draft.get("model_error"):
        st.warning(f"Gemini draft is unavailable: {draft['model_error']}")
    subject = st.text_input(
        "Email subject",
        value=draft["subject"],
        key=f"email-subject-{notification_id}",
    )
    body = st.text_area(
        "Review and edit email",
        value=draft["body"],
        key=f"email-body-{notification_id}",
        height=180,
    )
    label = "Confirm and send urgent email" if urgent else "Send email to configured test inbox"
    if st.button(
        label, type="primary" if urgent else "secondary", key=f"send-email-{notification_id}"
    ):
        try:
            result = api(
                "POST",
                f"/revenue-sre/merchant/notifications/{notification_id}/send-live-email",
                token=token,
                json={"subject": subject, "body": body},
            )
            if result["status"] == "live_sent":
                st.success(
                    "Email provider accepted the reviewed alert for the configured test inbox."
                )
                if result.get("provider_message_id"):
                    st.caption(f"Email delivery ID: {result['provider_message_id']}")
            elif result.get("replayed"):
                st.info("This alert was already sent; a duplicate email was prevented.")
            else:
                st.warning(result.get("safe_error") or "The email could not be sent.")
        except RuntimeError as exc:
            st.error(str(exc))


def urgent_email_prompt(token: str, findings: list[dict[str, Any]]) -> None:
    """Show one sign-in prompt for a critical issue; confirmation is still mandatory."""

    critical = next((item for item in findings if item["severity"] == "critical"), None)
    if critical is None:
        return
    finding_id = critical["finding_id"]
    state_key = f"urgent-email-ready-{finding_id}"
    if state_key not in st.session_state:
        try:
            notification = api(
                "POST",
                f"/revenue-sre/merchant/critical-findings/{finding_id}/prepare-urgent-email",
                token=token,
            )
            draft = api(
                "POST",
                f"/revenue-sre/merchant/notifications/{notification['notification_id']}/draft-email",
                token=token,
            )
            st.session_state[state_key] = {"notification": notification, "draft": draft}
        except RuntimeError as exc:
            st.session_state[state_key] = {"error": str(exc)}
    state = st.session_state[state_key]

    @st.dialog("Critical payment issue — engineering email ready")
    def show_prompt() -> None:
        st.error(
            f"Payment success dropped from {pct(critical['baseline_success_rate'])} to "
            f"{pct(critical['observed_success_rate'])}."
        )
        st.write(
            "Gemini prepared an internal engineering email. Review it; it will not send until you confirm."
        )
        if state.get("error"):
            st.warning(state["error"])
            return
        notification = state["notification"]
        draft = state["draft"]
        if draft.get("model_used") == "gemini":
            st.success("Gemini draft ready.")
        else:
            st.warning(f"Gemini is unavailable: {draft.get('model_error')}")
        subject = st.text_input(
            "Email subject", value=draft["subject"], key=f"urgent-subject-{finding_id}"
        )
        body = st.text_area(
            "Review email before sending",
            value=draft["body"],
            key=f"urgent-body-{finding_id}",
            height=200,
        )
        if st.button("OK — send urgent email", type="primary", key=f"urgent-send-{finding_id}"):
            result = api(
                "POST",
                f"/revenue-sre/merchant/notifications/{notification['notification_id']}/send-live-email",
                token=token,
                json={"subject": subject, "body": body},
            )
            if result["status"] == "live_sent":
                st.success("Email provider accepted the urgent alert.")
                st.session_state[f"urgent-email-sent-{finding_id}"] = True
            else:
                st.warning(result.get("safe_error") or "The urgent email could not be sent.")

    if not st.session_state.get(f"urgent-email-sent-{finding_id}"):
        show_prompt()


def alerts_panel(token: str, interventions: list[dict[str, Any]]) -> None:
    notifications = api("GET", "/revenue-sre/merchant/notifications", token=token)["notifications"]
    st.markdown("### Alerts")
    st.caption(
        "Create an alert first. Emails require review and send only to the configured Resend test inbox; "
        "WhatsApp is limited to your personal Twilio Sandbox number."
    )
    critical_result = st.session_state.pop("critical_email_result", None)
    if critical_result:
        route = critical_result.get("route")
        if route:
            st.info(
                f"Routing decision: {route['recipient_name']} in {route['recipient_team'].title()} "
                f"was selected from Team settings. {route['reason']}"
            )
        if critical_result.get("status") == "live_sent":
            st.success(
                "Gemini drafted and the email provider accepted the critical engineering alert "
                "for the configured test inbox."
            )
        elif critical_result.get("replayed"):
            st.info("This critical email was already sent; a duplicate was prevented.")
        else:
            st.warning(critical_result.get("safe_error") or "The critical email was not sent.")
    for item in interventions:
        if st.button(
            "Create demo alert (no real message)", key=f"notify-{item['intervention_id']}"
        ):
            api(
                "POST",
                f"/revenue-sre/merchant/interventions/{item['intervention_id']}/notify",
                token=token,
            )
            st.rerun()
    if not notifications:
        st.info("No alerts have been created yet.")
    for notification in notifications:
        with st.container(border=True):
            st.markdown(f"**{notification['subject']}** · {human(notification['status'])}")
            st.caption(
                f"To: {notification['recipient_name']} ({notification['team']}) · {notification['channel'].upper()}"
            )
            st.write(notification["body"])
            email_compose_controls(token, notification)
            if st.button(
                "Send WhatsApp to my test number",
                key=f"whatsapp-{notification['notification_id']}",
            ):
                result = api(
                    "POST",
                    f"/revenue-sre/merchant/notifications/{notification['notification_id']}/send-live-whatsapp",
                    token=token,
                )
                if result["status"] == "live_sent":
                    st.success(
                        "Twilio accepted the WhatsApp alert for your configured test number."
                    )
                    if result.get("provider_message_id"):
                        st.caption(f"Twilio delivery ID: {result['provider_message_id']}")
                elif result["status"] == "not_configured":
                    st.warning(result["safe_error"])
                elif result.get("replayed"):
                    st.info(
                        "This alert was already sent once; the system prevented a duplicate message."
                    )
                else:
                    st.error(result.get("safe_error") or "Twilio could not send the alert.")
    live_whatsapp_replies(token)


def team_panel(token: str) -> None:
    config = api("GET", "/revenue-sre/merchant/team-and-alerts", token=token)
    notifications = api("GET", "/revenue-sre/merchant/notifications", token=token)["notifications"]
    st.markdown("### Team settings")
    st.caption("Saved routing contacts. Sending an alert does not add a new contact row.")
    st.dataframe(config["contacts"], width="stretch", hide_index=True)
    st.markdown("#### Recent alert routing")
    if notifications:
        routing_rows = [
            {
                "sent / created": item["created_at"],
                "team": item["team"],
                "recipient": item["recipient_name"],
                "channel": item["channel"],
                "status": item["status"],
                "subject": item["subject"],
            }
            for item in notifications[:5]
        ]
        st.dataframe(routing_rows, width="stretch", hide_index=True)
    else:
        st.info("No alert has been routed yet for this company.")
    with st.form("contact"):
        team = st.selectbox("Team", ["owner", "payments", "engineering", "support"])
        name = st.text_input("Name")
        email = st.text_input("Email")
        severity = st.selectbox("Minimum severity", ["low", "medium", "high", "critical"], index=2)
        if st.form_submit_button("Add contact"):
            api(
                "POST",
                "/revenue-sre/merchant/team-and-alerts/contacts",
                token=token,
                json={
                    "team": team,
                    "name": name,
                    "email": email or None,
                    "minimum_severity": severity,
                },
            )
            st.rerun()


def merchant_home(user: dict[str, Any]) -> None:
    token = st.session_state.token
    overview = api("GET", "/revenue-sre/merchant/overview", token=token)
    health = api("GET", "/revenue-sre/merchant/payment-health", token=token)
    brief = api("GET", "/revenue-sre/merchant/morning-brief", token=token)
    investigations = api("GET", "/revenue-sre/merchant/investigations", token=token)[
        "investigations"
    ]
    interventions = api("GET", "/revenue-sre/merchant/interventions", token=token)["interventions"]
    merchant, findings = overview["merchant"], health["findings"]
    left, centre, right = st.columns([1.35, 4.8, 1.8], gap="medium")
    with left:
        view = merchant_navigation(token, merchant, findings, interventions)
    with centre:
        section_kicker("Payment reliability command center")
        st.markdown(
            f"<div class='workspace-hero'><div><h1>Good morning, {escape(str(merchant['name']))}</h1>"
            "<p>Your payment reliability brief, with evidence and safe next steps.</p></div>"
            "<div class='live-badge'><span></span> Live synthetic monitoring</div></div>",
            unsafe_allow_html=True,
        )
        metrics = st.columns(3)
        metrics[0].metric("Normal success", pct(merchant["baseline_success_rate"]))
        metrics[1].metric("Issues now", len(findings))
        metrics[2].metric("Open incidents", len(overview["incidents"]))
        with st.container(border=True):
            st.markdown("<div class='brief-label'>TODAY'S SIGNAL</div>", unsafe_allow_html=True)
            st.markdown(f"### {brief['headline']}")
            st.write(brief["detail"])
            st.caption(f"Agent record: {brief['agent_status']}")
        if view == "Today":
            decision_card(token, interventions)
            recovery_progress(token, interventions)
            issue_cards(token, findings, investigations, interventions)
            customer_recovery_panel(token, findings, investigations)
            live_customer_recovery_tracker(token)
        elif view == "Ask Copilot":
            copilot_panel(token, brief)
        elif view == "Alerts":
            alerts_panel(token, interventions)
        else:
            team_panel(token)
    with right:
        live_agent_timeline(token)


def operations_home() -> None:
    result = api("GET", "/revenue-sre/ops/detected-clusters", token=st.session_state.token)
    section_kicker("Restricted control plane")
    st.title("Razorpay Operations")
    st.caption("Anonymized network view. Merchant names and customer data are intentionally hidden.")
    for cluster in result["clusters"]:
        with st.container(border=True):
            st.markdown(f"### {human(cluster['error_family'])} · {cluster['severity'].title()}")
            st.write(
                f"Detected across **{cluster['affected_merchant_count']} anonymized merchants**."
            )
    if st.button("Run all specialist investigations", type="primary"):
        completed = api("POST", "/revenue-sre/ops/investigations/run", token=st.session_state.token)
        st.success(f"Completed {completed['investigations_started']} investigations.")


st.set_page_config(page_title="Revenue SRE", page_icon="⚡", layout="wide")
st.markdown(
    """<style>
    :root { --panel:#121c2a; --line:#273a52; --muted:#9fb0c3; --text:#f5f8fc; --cyan:#55d8ff; --mint:#4de3ad; --danger:#ff7373; --amber:#ffc85b; }
    .stApp { background: radial-gradient(circle at 75% -20%, #17334a 0, transparent 34%), linear-gradient(145deg, #08111d 0%, #0b1422 55%, #07111b 100%); color:var(--text); }
    .block-container { max-width:1540px; padding-top:1.6rem; padding-bottom:3rem; }
    [data-testid="stVerticalBlockBorderWrapper"] { border:1px solid var(--line); border-radius:18px; background:linear-gradient(135deg,rgba(21,33,50,.95),rgba(12,22,35,.95)); box-shadow:0 14px 30px rgba(0,0,0,.16); }
    [data-testid="stMetric"] { padding:1rem .9rem; border:1px solid var(--line); border-radius:14px; background:rgba(17,29,45,.72); }
    [data-testid="stMetricLabel"] { color:var(--muted); font-size:.76rem; letter-spacing:.05em; text-transform:uppercase; }
    [data-testid="stMetricValue"] { color:var(--text); }
    .section-kicker { color:var(--cyan); font-size:.72rem; font-weight:800; letter-spacing:.13em; text-transform:uppercase; margin:.3rem 0 .45rem; }
    .top-product-bar,.rail-brand { display:flex; align-items:center; gap:.65rem; color:#eaf4ff; font-size:.76rem; font-weight:800; letter-spacing:.13em; }
    .top-product-bar { padding:.4rem 0 1.8rem; } .top-product-bar em { font-style:normal; color:var(--muted); font-weight:600; letter-spacing:.06em; } .top-product-bar b { margin-left:auto; padding:.22rem .55rem; color:#0a1b1c; background:var(--mint); border-radius:99px; font-size:.65rem; }
    .brand-orb { width:28px; height:28px; display:inline-grid; place-items:center; background:linear-gradient(135deg,var(--cyan),var(--mint)); border-radius:9px; color:#07111d; font-weight:900; letter-spacing:0; }
    .login-hero { padding:.7rem 0 1.1rem; max-width:650px; } .login-hero h1 { font-size:clamp(2.5rem,4.7vw,4.4rem); line-height:1.02; letter-spacing:-.055em; margin:0 0 1.15rem; } .login-hero h1 span { color:var(--cyan); } .login-hero p { color:var(--muted); font-size:1.08rem; line-height:1.7; max-width:590px; }
    .access-card-title { font-weight:800; font-size:1.25rem; margin-top:1rem; } .trust-row { display:flex; gap:1rem; flex-wrap:wrap; color:#b7c5d4; font-size:.85rem; margin-top:1.25rem; }
    .workflow-guide { display:flex; align-items:stretch; gap:.5rem; margin:1.2rem 0 .4rem; padding:1rem; border:1px solid var(--line); border-radius:16px; background:rgba(16,29,45,.66); } .workflow-guide>div { flex:1; min-width:110px; } .workflow-guide span { display:block; color:var(--cyan); font-size:.69rem; font-weight:900; letter-spacing:.12em; } .workflow-guide strong { display:block; margin:.22rem 0; font-size:.88rem; } .workflow-guide small { color:var(--muted); line-height:1.35; display:block; } .workflow-guide i { align-self:center; color:#4f6882; font-style:normal; }
    .merchant-name { font-size:1.65rem; line-height:1.1; font-weight:800; letter-spacing:-.04em; margin:1.2rem 0 .3rem; } .rail-brand small { display:block; color:var(--muted); font-weight:500; letter-spacing:0; margin-top:.15rem; }
    .rail-issue { padding:.75rem .8rem; margin:.5rem 0; border-radius:12px; border-left:3px solid var(--amber); background:rgba(255,200,91,.07); } .rail-issue.severity-critical { border-left-color:var(--danger); background:rgba(255,115,115,.08); } .rail-issue strong,.rail-issue span { display:block; } .rail-issue strong { font-size:.85rem; } .rail-issue span { color:var(--muted); font-size:.75rem; margin-top:.25rem; }
    .workspace-hero { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; margin-bottom:1rem; } .workspace-hero h1 { letter-spacing:-.045em; margin:0; font-size:clamp(2rem,3.4vw,3.25rem); } .workspace-hero p { margin:.55rem 0 0; color:var(--muted); } .live-badge { white-space:nowrap; margin-top:.35rem; color:#c9f9e5; background:rgba(77,227,173,.12); border:1px solid rgba(77,227,173,.28); padding:.42rem .65rem; border-radius:99px; font-size:.76rem; } .live-badge span { display:inline-block; width:7px; height:7px; background:var(--mint); border-radius:50%; margin-right:.38rem; box-shadow:0 0 12px var(--mint); }
    .brief-label { color:var(--cyan); font-size:.68rem; font-weight:900; letter-spacing:.12em; }
    .severity-chip { display:inline-block; border-radius:99px; padding:.22rem .5rem; font-size:.67rem; font-weight:900; letter-spacing:.08em; background:rgba(255,200,91,.14); color:var(--amber); } .severity-chip.severity-critical { background:rgba(255,115,115,.15); color:#ff9d9d; }
    @keyframes revenueSreBlink { 50% { opacity: 0.35; } }
    .critical-alert {
        color: #ff6b6b; font-weight: 800; margin: 0.8rem 0 0.35rem;
        animation: revenueSreBlink 1.1s step-start infinite;
    }
    .timeline-event { position:relative; margin:.65rem 0; padding:.85rem .85rem .8rem 1rem; border-left:1px solid #33506b; background:rgba(16,28,44,.68); border-radius:0 12px 12px 0; } .timeline-event div { color:var(--cyan); font-size:.72rem; margin-bottom:.35rem; } .timeline-dot { display:inline-block; width:8px; height:8px; margin-right:.35rem; border-radius:50%; background:var(--mint); box-shadow:0 0 10px var(--mint); } .timeline-event strong { display:block; font-size:.87rem; } .timeline-event small { display:block; color:var(--muted); margin:.3rem 0; line-height:1.35; } .timeline-event p { margin:.35rem 0 0; font-size:.83rem; line-height:1.45; color:#d8e4f0; }
    [data-testid="stButton"] button { border-radius:10px; font-weight:700; min-height:2.55rem; } [data-testid="stButton"] button[kind="primary"] { background:#ff4b4b; color:#fff; border:0; } [data-testid="stButton"] button[kind="secondary"] { border-color:#3b5673; }
    @media (max-width:800px) { .workflow-guide { display:grid; grid-template-columns:1fr 1fr; } .workflow-guide i { display:none; } .workspace-hero { display:block; } .live-badge { display:inline-block; margin-top:1rem; } .top-product-bar em { display:none; } }
    </style>""",
    unsafe_allow_html=True,
)
if not st.session_state.get("backend_url"):
    st.session_state.backend_url = configured_backend_url()
if "token" not in st.session_state:
    login()
else:
    try:
        if st.session_state.user["role"] == "razorpay_ops":
            operations_home()
        else:
            merchant_home(st.session_state.user)
    except RuntimeError as exc:
        st.error(str(exc))
