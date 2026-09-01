"""FastAPI entry point for local development and webhook delivery."""

# ruff: noqa: B008, E501

from __future__ import annotations

from datetime import UTC, datetime, time
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from recovery_orchestrator.clock import WallClock
from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.db.repositories import ExternalEventRepository
from recovery_orchestrator.demo_runtime import DemoRuntime
from recovery_orchestrator.integrations.razorpay_webhooks import (
    InvalidWebhookSignature,
    RazorpayWebhookInbox,
)
from recovery_orchestrator.revenue_sre.auth import new_session_token
from recovery_orchestrator.revenue_sre.copilot import (
    ask_copilot,
    copilot_history,
    draft_engineering_email,
    morning_brief,
)
from recovery_orchestrator.revenue_sre.customer_recovery import (
    approve_and_send_batch,
    customer_delivery_statuses,
    latest_batch,
    prepare_batch,
    record_link_open,
    record_payment_complete,
    retry_undelivered_customer_message,
    run_follow_up_monitor,
)
from recovery_orchestrator.revenue_sre.detection import (
    merchant_findings,
    operations_clusters,
    run_detection,
)
from recovery_orchestrator.revenue_sre.intervention import (
    approve_intervention,
    execute_intervention,
    interventions_for_merchant,
    request_intervention,
    rollback_intervention,
    verify_intervention,
)
from recovery_orchestrator.revenue_sre.investigation import (
    get_investigation,
    investigations_for_merchant,
    run_all_investigations,
    run_investigation,
)
from recovery_orchestrator.revenue_sre.live_email import deliver_controlled_email
from recovery_orchestrator.revenue_sre.live_monitor import live_payment_summary, record_live_payment
from recovery_orchestrator.revenue_sre.live_whatsapp import (
    deliver_controlled_whatsapp,
    inbound_replies,
    record_inbound_reply,
    valid_webhook_signature,
)
from recovery_orchestrator.revenue_sre.notifications import (
    add_contact,
    critical_engineering_route,
    dispatch_for_intervention,
    list_contacts,
    list_outbox,
    list_rules,
    prepare_critical_engineering_email,
    seed_notification_defaults,
)
from recovery_orchestrator.revenue_sre.repository import AuthenticatedUser, RevenueSRERepository
from recovery_orchestrator.revenue_sre.synthetic_data import (
    create_new_synthetic_run,
    current_demo_account_guide,
    ensure_synthetic_data,
    validate_synthetic_data,
)
from recovery_orchestrator.settings import Settings


class RunCaseRequest(BaseModel):
    live_ai: bool = False


class CustomerReplyRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    live_ai: bool = False
    channel: str = Field(default="whatsapp", pattern=r"^(whatsapp|voice_transcript|email)$")


class ApplyPaymentRequest(BaseModel):
    amount_paise: int = Field(gt=0)


class HumanReviewRequest(BaseModel):
    approve: bool


class AdvanceTimeRequest(BaseModel):
    hours: int = Field(gt=0, le=24 * 60)


class CopilotRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class LoginRequest(BaseModel):
    login_id: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class ApprovalRequest(BaseModel):
    approve: bool


class CreateSyntheticDataRequest(LoginRequest):
    """Operations credentials required by the landing-page demo reset control."""


class NotificationContactRequest(BaseModel):
    team: str = Field(pattern=r"^(owner|payments|engineering|support)$")
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=30)
    email_opt_in: bool = True
    sms_opt_in: bool = False
    minimum_severity: str = Field(default="high", pattern=r"^(low|medium|high|critical)$")


class LiveEmailSendRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


class RevenueSRECopilotRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class LivePaymentEventRequest(BaseModel):
    payment_method: str = Field(default="upi", pattern=r"^(upi|card)$")
    status: str = Field(default="failed", pattern=r"^(paid|failed|pending)$")


def _parse_date_start(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.combine(datetime.fromisoformat(value).date(), time.min)
    return parsed.replace(tzinfo=UTC)


def _parse_date_end(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    parsed = datetime.combine(datetime.fromisoformat(value).date(), time.max)
    return parsed.replace(tzinfo=UTC)


def _case_summary(case) -> dict:
    return {
        "case_id": case.case_id,
        "customer_id": case.customer.customer_id,
        "party_name": case.customer.name,
        "party_type": case.party_type.value,
        "event_type": case.event_type.value,
        "external_reference": case.external_reference,
        "subscription_id": case.subscription_id,
        "status": case.status.value,
        "amount_due_paise": case.amount_due_paise,
        "amount_recovered_paise": case.amount_recovered_paise,
        "outstanding_balance_paise": case.outstanding_balance_paise,
        "source": case.source,
        "phone": case.customer.phone,
        "preferred_channel": case.customer.preferred_channel,
        "wake_at": case.wake_at.isoformat() if case.wake_at else None,
        "due_at": case.signals.due_at.isoformat() if case.signals.due_at else None,
        "days_overdue": case.signals.days_overdue,
        "messages_sent": case.signals.messages_sent,
        "calls_made": case.signals.calls_made,
        "no_reply_count": case.signals.no_reply_count,
        "recovery_score": case.signals.recovery_score,
        "predicted_payment_probability": case.signals.predicted_payment_probability,
    }


def create_app(
    settings: Settings | None = None,
    *,
    webhook_inbox: RazorpayWebhookInbox | None = None,
) -> FastAPI:
    resolved = settings or Settings()
    database = Database(resolved.database_path)
    database.initialize()
    revenue_sre = RevenueSRERepository(database)
    revenue_sre.seed_tenants()
    ensure_synthetic_data(database)
    run_detection(database)
    seed_notification_defaults(database)
    demo = DemoRuntime.create(resolved)

    inbox = webhook_inbox
    if inbox is None and resolved.razorpay_webhook_secret is not None:
        inbox = RazorpayWebhookInbox(
            ExternalEventRepository(database),
            resolved.razorpay_webhook_secret.get_secret_value(),
        )

    app = FastAPI(
        title="Revenue SRE — Agentic Payment Reliability Copilot",
        version="0.4.0",
    )
    app.state.settings = resolved
    app.state.database = database
    app.state.webhook_inbox = inbox
    app.state.demo = demo
    app.state.revenue_sre = revenue_sre

    bearer_scheme = HTTPBearer(auto_error=False)

    def current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> AuthenticatedUser:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
        user = revenue_sre.user_from_token(credentials.credentials)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is invalid or expired"
            )
        return user

    def merchant_user(user: AuthenticatedUser = Depends(current_user)) -> AuthenticatedUser:
        if user.role != "merchant_owner" or user.merchant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Merchant account required"
            )
        return user

    def operations_user(user: AuthenticatedUser = Depends(current_user)) -> AuthenticatedUser:
        if user.role != "razorpay_ops":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Razorpay Operations account required"
            )
        return user

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": resolved.app_env}

    @app.post("/revenue-sre/auth/login")
    def revenue_sre_login(body: LoginRequest) -> dict[str, object]:
        token = new_session_token()
        user = revenue_sre.authenticate(body.login_id, body.password, token)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid demo login"
            )
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "login_id": user.login_id,
                "role": user.role,
                "merchant_id": user.merchant_id,
                "merchant_name": user.merchant_name,
            },
        }

    @app.post("/revenue-sre/demo-runs/create")
    def revenue_sre_create_demo_run(body: CreateSyntheticDataRequest) -> dict[str, object]:
        """Generate a fresh, complete portfolio only for the demo Operations account."""

        operator = revenue_sre.authenticate(body.login_id, body.password, new_session_token())
        if operator is None or operator.role != "razorpay_ops":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Razorpay Operations credentials are required to create a new demo run",
            )
        result = create_new_synthetic_run(database)
        detection = run_detection(database)
        seed_notification_defaults(database)
        return {
            "created": True,
            "run": result,
            "detection": detection,
            "account_guide": current_demo_account_guide(database),
        }

    @app.get("/revenue-sre/ops/current-account-guide")
    def revenue_sre_current_account_guide(
        user: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, object]:
        return {"accounts": current_demo_account_guide(database)}

    @app.get("/revenue-sre/auth/me")
    def revenue_sre_me(user: AuthenticatedUser = Depends(current_user)) -> dict[str, object]:
        return {
            "login_id": user.login_id,
            "role": user.role,
            "merchant_id": user.merchant_id,
            "merchant_name": user.merchant_name,
        }

    @app.post("/revenue-sre/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def revenue_sre_logout(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
        revenue_sre.revoke_token(credentials.credentials)

    @app.get("/revenue-sre/merchant/overview")
    def revenue_sre_merchant_overview(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return revenue_sre.merchant_overview(user.merchant_id or "")

    @app.get("/revenue-sre/merchant/payment-health")
    def revenue_sre_merchant_payment_health(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {
            "merchant_id": user.merchant_id,
            "findings": merchant_findings(database, user.merchant_id or ""),
        }

    @app.get("/revenue-sre/merchant/morning-brief")
    def revenue_sre_morning_brief(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return morning_brief(database, user.merchant_id or "")

    @app.post("/revenue-sre/merchant/copilot")
    def revenue_sre_copilot(
        body: RevenueSRECopilotRequest,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        gemini_key = (
            resolved.gemini_api_key.get_secret_value()
            if resolved.gemini_api_key is not None
            else None
        )
        return ask_copilot(
            database,
            user.merchant_id or "",
            body.question,
            gemini_api_key=gemini_key,
            gemini_model=resolved.gemini_model,
        )

    @app.get("/revenue-sre/merchant/copilot/history")
    def revenue_sre_copilot_history(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {"messages": copilot_history(database, user.merchant_id or "")}

    @app.get("/revenue-sre/merchant/live-payment-health")
    def revenue_sre_live_payment_health(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return live_payment_summary(database, user.merchant_id or "")

    @app.post("/revenue-sre/merchant/live-payment-events")
    def revenue_sre_record_live_payment(
        body: LivePaymentEventRequest,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return record_live_payment(
            database, user.merchant_id or "", body.payment_method, body.status
        )

    @app.post("/revenue-sre/merchant/investigations/{finding_id}/run")
    def revenue_sre_run_merchant_investigation(
        finding_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        permitted_ids = {
            item["finding_id"] for item in merchant_findings(database, user.merchant_id or "")
        }
        if finding_id not in permitted_ids:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found")
        investigation = run_investigation(database, finding_id)
        # Critical checkout regressions are operational incidents: alert the configured
        # engineering inbox automatically after the evidence is confirmed. This does
        # not approve or create any customer payment link.
        if (
            investigation["scope"] == "merchant"
            and str(investigation["root_cause_summary"])
            and any(
                term in str(investigation["root_cause_summary"]).lower()
                for term in ("sdk", "checkout", "redirect")
            )
        ):
            try:
                notification = prepare_critical_engineering_email(
                    database, user.merchant_id or "", finding_id
                )
                draft = draft_engineering_email(
                    database,
                    user.merchant_id or "",
                    str(notification["notification_id"]),
                    gemini_api_key=(
                        resolved.gemini_api_key.get_secret_value()
                        if resolved.gemini_api_key is not None
                        else None
                    ),
                    gemini_model=resolved.gemini_model,
                )
                if draft["model_used"] == "gemini":
                    investigation["automatic_engineering_email"] = deliver_controlled_email(
                        database,
                        resolved,
                        str(notification["notification_id"]),
                        subject=str(draft["subject"]),
                        body=str(draft["body"]),
                    )
                else:
                    investigation["automatic_engineering_email"] = {
                        "status": "not_sent",
                        "safe_error": "Gemini could not draft the automatic engineering alert.",
                    }
            except (KeyError, RuntimeError, ValueError) as exc:
                investigation["automatic_engineering_email"] = {
                    "status": "not_sent",
                    "safe_error": str(exc),
                }
        return investigation

    @app.get("/revenue-sre/merchant/investigations")
    def revenue_sre_merchant_investigations(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {
            "merchant_id": user.merchant_id,
            "investigations": investigations_for_merchant(database, user.merchant_id or ""),
        }

    @app.post("/revenue-sre/merchant/interventions/{investigation_id}/request")
    def revenue_sre_request_intervention(
        investigation_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        owned_ids = {
            item["investigation_id"]
            for item in investigations_for_merchant(database, user.merchant_id or "")
        }
        if investigation_id not in owned_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found"
            )
        return request_intervention(database, investigation_id)

    @app.get("/revenue-sre/merchant/interventions")
    def revenue_sre_merchant_interventions(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {
            "merchant_id": user.merchant_id,
            "interventions": interventions_for_merchant(database, user.merchant_id or ""),
        }

    @app.post("/revenue-sre/merchant/interventions/{intervention_id}/notify")
    def revenue_sre_notify_intervention(
        intervention_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        allowed_ids = {
            item["intervention_id"]
            for item in interventions_for_merchant(database, user.merchant_id or "")
        }
        if intervention_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Intervention not found"
            )
        return {"deliveries": dispatch_for_intervention(database, intervention_id)}

    @app.get("/revenue-sre/merchant/team-and-alerts")
    def revenue_sre_team_and_alerts(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        merchant_id = user.merchant_id or ""
        return {
            "contacts": list_contacts(database, merchant_id),
            "rules": list_rules(database, merchant_id),
        }

    @app.post("/revenue-sre/merchant/team-and-alerts/contacts")
    def revenue_sre_add_contact(
        body: NotificationContactRequest,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return add_contact(database, merchant_id=user.merchant_id or "", **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/revenue-sre/merchant/notifications")
    def revenue_sre_notification_outbox(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {"notifications": list_outbox(database, user.merchant_id or "")}

    @app.get("/revenue-sre/merchant/whatsapp-replies")
    def revenue_sre_whatsapp_replies(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        return {"replies": inbound_replies(database, user.merchant_id or "")}

    @app.post("/revenue-sre/merchant/customer-recovery/{finding_id}/prepare")
    def revenue_sre_prepare_customer_recovery(
        finding_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return prepare_batch(database, resolved, user.merchant_id or "", finding_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/revenue-sre/merchant/customer-recovery")
    def revenue_sre_customer_recovery(
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            run_follow_up_monitor(database, resolved, user.merchant_id or "")
            return {"batch": latest_batch(database, user.merchant_id or "")}
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/customer-recovery/{batch_id}/approve-and-send")
    def revenue_sre_approve_customer_recovery(
        batch_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return approve_and_send_batch(database, resolved, user.merchant_id or "", batch_id)
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/revenue-sre/merchant/customer-recovery/{batch_id}/delivery-status")
    def revenue_sre_customer_recovery_delivery_status(
        batch_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return {
                "recipients": customer_delivery_statuses(
                    database, resolved, user.merchant_id or "", batch_id
                )
            }
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post(
        "/revenue-sre/merchant/customer-recovery/{batch_id}/recipients/{recipient_id}/retry"
    )
    def revenue_sre_retry_customer_recovery_message(
        batch_id: str,
        recipient_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return retry_undelivered_customer_message(
                database, resolved, user.merchant_id or "", batch_id, recipient_id
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/customer-recovery/test-payment/{token}", response_class=HTMLResponse)
    def revenue_sre_test_payment_page(token: str) -> HTMLResponse:
        # WhatsApp fetches links to create a preview. A plain GET is not customer
        # intent and must not produce a false “link opened” event.
        with database.connect() as connection:
            recipient = connection.execute(
                "SELECT recipient_id FROM customer_recovery_recipients WHERE link_token = ?",
                (token,),
            ).fetchone()
        if recipient is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Test payment link not found"
            )
        return HTMLResponse(
            "<html><body style='font-family: sans-serif; max-width: 560px; margin: 64px auto'>"
            "<h1>Revenue SRE test payment</h1><p>This is a synthetic demo link. No money is collected.</p>"
            "<p id='tracking'>Opening test payment securely…</p>"
            f"<form method='post' action='/customer-recovery/test-payment/{token}/complete'>"
            "<button type='submit' style='padding:12px 20px'>Payment done (test)</button></form>"
            f"<script>fetch('/customer-recovery/test-payment/{token}/open', {{method: 'POST'}})"
            ".then(() => document.getElementById('tracking').textContent = 'Test payment page opened.')"
            ".catch(() => document.getElementById('tracking').textContent = 'Open tracking is unavailable.');</script>"
            "</body></html>"
        )

    @app.post(
        "/customer-recovery/test-payment/{token}/open", status_code=status.HTTP_204_NO_CONTENT
    )
    def revenue_sre_record_test_payment_open(token: str) -> None:
        if record_link_open(database, token) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Test payment link not found"
            )

    @app.post("/customer-recovery/test-payment/{token}/complete", response_class=HTMLResponse)
    def revenue_sre_complete_test_payment(token: str) -> HTMLResponse:
        if not record_payment_complete(database, token):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Test payment link not found"
            )
        return HTMLResponse(
            "<html><body style='font-family: sans-serif; max-width: 560px; margin: 64px auto'>"
            "<h1>Test payment recorded</h1><p>No real payment was collected. Revenue SRE has updated the dashboard.</p>"
            "</body></html>"
        )

    @app.post("/revenue-sre/merchant/notifications/{notification_id}/send-live-email")
    def revenue_sre_send_live_email(
        notification_id: str,
        body: LiveEmailSendRequest,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        owned_ids = {
            item["notification_id"] for item in list_outbox(database, user.merchant_id or "")
        }
        if notification_id not in owned_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
            )
        return deliver_controlled_email(
            database, resolved, notification_id, subject=body.subject, body=body.body
        )

    @app.post("/revenue-sre/merchant/notifications/{notification_id}/draft-email")
    def revenue_sre_draft_email(
        notification_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, str | None]:
        return draft_engineering_email(
            database,
            user.merchant_id or "",
            notification_id,
            gemini_api_key=(
                resolved.gemini_api_key.get_secret_value()
                if resolved.gemini_api_key is not None
                else None
            ),
            gemini_model=resolved.gemini_model,
        )

    @app.post("/revenue-sre/merchant/critical-findings/{finding_id}/prepare-urgent-email")
    def revenue_sre_prepare_urgent_email(
        finding_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return prepare_critical_engineering_email(database, user.merchant_id or "", finding_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/revenue-sre/merchant/critical-findings/{finding_id}/engineering-route")
    def revenue_sre_critical_engineering_route(
        finding_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        try:
            return critical_engineering_route(database, user.merchant_id or "", finding_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/critical-findings/{finding_id}/send-urgent-email")
    def revenue_sre_send_urgent_email(
        finding_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        """One-click, threshold-triggered engineering escalation from the sidebar."""

        try:
            notification = prepare_critical_engineering_email(
                database, user.merchant_id or "", finding_id
            )
            draft = draft_engineering_email(
                database,
                user.merchant_id or "",
                str(notification["notification_id"]),
                gemini_api_key=(
                    resolved.gemini_api_key.get_secret_value()
                    if resolved.gemini_api_key is not None
                    else None
                ),
                gemini_model=resolved.gemini_model,
            )
            if draft["model_used"] != "gemini":
                return {
                    "status": "not_sent",
                    "safe_error": "Gemini could not draft the critical engineering email.",
                }
            delivery = deliver_controlled_email(
                database,
                resolved,
                str(notification["notification_id"]),
                subject=str(draft["subject"]),
                body=str(draft["body"]),
            )
            return {**delivery, "route": notification["route"]}
        except (KeyError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/notifications/{notification_id}/send-live-whatsapp")
    def revenue_sre_send_live_whatsapp(
        notification_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        """Deliver to the one configured personal Sandbox number, never a demo contact."""

        owned_ids = {
            item["notification_id"] for item in list_outbox(database, user.merchant_id or "")
        }
        if notification_id not in owned_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
            )
        return deliver_controlled_whatsapp(database, resolved, notification_id)

    @app.post("/webhooks/twilio/whatsapp")
    async def twilio_whatsapp_webhook(
        request: Request,
        x_twilio_signature: str = Header(default=""),
    ) -> Response:
        """Receive a signed WhatsApp reply through a temporary public demo URL."""

        if not resolved.twilio_webhook_base_url or not resolved.twilio_auth_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Set TWILIO_WEBHOOK_BASE_URL before enabling inbound WhatsApp replies.",
            )
        raw_body = await request.body()
        parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
        form_fields = {key: values[-1] for key, values in parsed.items()}
        expected_url = f"{resolved.twilio_webhook_base_url.rstrip('/')}/webhooks/twilio/whatsapp"
        if not valid_webhook_signature(
            auth_token=resolved.twilio_auth_token.get_secret_value(),
            url=expected_url,
            form_fields=form_fields,
            signature=x_twilio_signature,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Twilio signature"
            )
        message_sid, sender, body = (
            form_fields.get("MessageSid"),
            form_fields.get("From"),
            form_fields.get("Body"),
        )
        if not message_sid or not sender or body is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Incomplete Twilio message"
            )
        record_inbound_reply(
            database,
            provider_message_id=message_sid,
            from_address=sender,
            body=body,
        )
        return Response("<Response></Response>", media_type="application/xml")

    @app.post("/revenue-sre/merchant/interventions/{intervention_id}/approval")
    def revenue_sre_approve_intervention(
        intervention_id: str,
        body: ApprovalRequest,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        allowed_ids = {
            item["intervention_id"]
            for item in interventions_for_merchant(database, user.merchant_id or "")
        }
        if intervention_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Intervention not found"
            )
        try:
            return approve_intervention(
                database, intervention_id, approved=body.approve, actor=user.login_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/interventions/{intervention_id}/execute")
    def revenue_sre_execute_intervention(
        intervention_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        allowed_ids = {
            item["intervention_id"]
            for item in interventions_for_merchant(database, user.merchant_id or "")
        }
        if intervention_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Intervention not found"
            )
        try:
            return execute_intervention(database, intervention_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/interventions/{intervention_id}/verify")
    def revenue_sre_verify_intervention(
        intervention_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        allowed_ids = {
            item["intervention_id"]
            for item in interventions_for_merchant(database, user.merchant_id or "")
        }
        if intervention_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Intervention not found"
            )
        try:
            return verify_intervention(database, intervention_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/revenue-sre/merchant/interventions/{intervention_id}/rollback")
    def revenue_sre_rollback_intervention(
        intervention_id: str,
        user: AuthenticatedUser = Depends(merchant_user),
    ) -> dict[str, object]:
        allowed_ids = {
            item["intervention_id"]
            for item in interventions_for_merchant(database, user.merchant_id or "")
        }
        if intervention_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Intervention not found"
            )
        try:
            return rollback_intervention(database, intervention_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/revenue-sre/ops/network-summary")
    def revenue_sre_network_summary(
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, object]:
        return revenue_sre.operations_summary()

    @app.get("/revenue-sre/ops/detected-clusters")
    def revenue_sre_detected_clusters(
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, object]:
        return {"clusters": operations_clusters(database)}

    @app.post("/revenue-sre/ops/detection/run")
    def revenue_sre_run_detection(
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, int]:
        return run_detection(database)

    @app.post("/revenue-sre/ops/investigations/run")
    def revenue_sre_run_all_investigations(
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, object]:
        investigations = run_all_investigations(database)
        return {"investigations_started": len(investigations), "investigations": investigations}

    @app.get("/revenue-sre/ops/investigations/{investigation_id}")
    def revenue_sre_get_investigation(
        investigation_id: str,
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, object]:
        try:
            return get_investigation(database, investigation_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get("/revenue-sre/demo/data-quality")
    def revenue_sre_data_quality(
        _: AuthenticatedUser = Depends(operations_user),
    ) -> dict[str, int | bool]:
        return validate_synthetic_data(database)

    @app.get("/demo/capabilities")
    def demo_capabilities() -> dict[str, str | bool]:
        return {
            "gemini_configured": demo.gemini_configured,
            "gemini_model": resolved.gemini_model,
            "razorpay_configured": bool(resolved.razorpay_key_id and resolved.razorpay_key_secret),
            "simulation_available": True,
        }

    @app.get("/demo/metrics")
    def demo_metrics() -> dict[str, int | float | str]:
        return demo.metrics()

    @app.get("/demo/cases")
    def demo_cases(
        query: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        bucket: str = "all",
    ) -> list[dict]:
        return [
            _case_summary(case)
            for case in demo.list_cases(
                query,
                date_from=_parse_date_start(date_from),
                date_to=_parse_date_end(date_to),
                bucket=bucket,
            )
        ]

    @app.get("/demo/portfolio/scan")
    def demo_portfolio_scan() -> dict[str, object]:
        return demo.scan_portfolio()

    @app.post("/demo/copilot")
    def demo_copilot(body: CopilotRequest) -> dict[str, object]:
        try:
            return demo.ask_copilot(body.question)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/demo/cases/{case_id}")
    def demo_case_detail(case_id: str) -> dict:
        try:
            return demo.detail(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc

    @app.post("/demo/reset")
    def demo_reset() -> dict[str, int | bool]:
        cases = demo.reset()
        return {"reset": True, "cases": len(cases)}

    @app.post("/demo/clock/advance")
    def demo_advance_clock(body: AdvanceTimeRequest) -> dict:
        return demo.advance_time(body.hours)

    @app.post("/demo/cases/{case_id}/run")
    def demo_run_case(case_id: str, body: RunCaseRequest) -> dict:
        try:
            return demo.run_case(case_id, live_ai=body.live_ai)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini agent request failed: {exc}",
            ) from exc

    @app.post("/demo/cases/{case_id}/reply")
    def demo_customer_reply(case_id: str, body: CustomerReplyRequest) -> dict:
        try:
            return demo.interpret_reply(
                case_id,
                body.message,
                live_ai=body.live_ai,
                channel=body.channel,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Gemini reply interpretation failed: {exc}",
            ) from exc

    @app.post("/demo/cases/{case_id}/payment")
    def demo_apply_payment(case_id: str, body: ApplyPaymentRequest) -> dict:
        try:
            return demo.apply_payment(case_id, body.amount_paise)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/demo/cases/{case_id}/human-review")
    def demo_human_review(case_id: str, body: HumanReviewRequest) -> dict:
        try:
            return demo.human_review(case_id, approve=body.approve)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/webhooks/razorpay", status_code=status.HTTP_202_ACCEPTED)
    async def razorpay_webhook(
        request: Request,
        x_razorpay_signature: str = Header(default=""),
        x_razorpay_event_id: str | None = Header(default=None),
    ) -> dict[str, str | bool]:
        if inbox is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Razorpay webhook integration is not configured",
            )
        raw_body = await request.body()
        try:
            event = inbox.ingest(
                raw_body=raw_body,
                signature=x_razorpay_signature,
                delivery_id=x_razorpay_event_id,
                received_at=WallClock().now(),
            )
        except InvalidWebhookSignature as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return {
            "accepted": True,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "duplicate": event.is_duplicate,
        }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("recovery_orchestrator.api:app", host="127.0.0.1", port=8010, reload=True)
