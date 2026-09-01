"""Fact-bounded merchant Copilot and morning briefing.

Gemini is used only to explain the merchant facts already retrieved by this module.
It never receives another merchant's data and it never makes an operational decision.
"""

# ruff: noqa: E501

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from recovery_orchestrator.db.connection import Database
from recovery_orchestrator.revenue_sre.customer_recovery import latest_batch
from recovery_orchestrator.revenue_sre.detection import merchant_findings
from recovery_orchestrator.revenue_sre.intervention import interventions_for_merchant
from recovery_orchestrator.revenue_sre.investigation import investigations_for_merchant
from recovery_orchestrator.revenue_sre.live_monitor import live_payment_summary
from recovery_orchestrator.revenue_sre.live_whatsapp import inbound_replies


def _pct(value: float) -> str:
    return f"{value:.0%}"


def _finding_reference(finding: dict[str, object]) -> dict[str, str]:
    return {"type": "payment_health_finding", "id": str(finding["finding_id"])}


def _investigation_reference(investigation: dict[str, object]) -> dict[str, str]:
    return {"type": "agent_investigation", "id": str(investigation["investigation_id"])}


def copilot_history(database: Database, merchant_id: str) -> list[dict[str, str]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT role, content FROM sre_copilot_messages WHERE merchant_id = ? ORDER BY created_at DESC LIMIT 20",
            (merchant_id,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _remember(database: Database, merchant_id: str, role: str, content: str) -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO sre_copilot_messages(message_id, merchant_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), merchant_id, role, content, datetime.now(UTC).isoformat()),
        )


def _copilot_facts(
    database: Database,
    merchant_id: str,
    findings: list[dict[str, Any]],
    investigations: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the deliberately small, tenant-scoped evidence bundle for Gemini."""

    return {
        "morning_brief": morning_brief(database, merchant_id),
        "payment_findings": findings[:3],
        "investigations": investigations[:3],
        "interventions": interventions[:3],
        "live_payment_summary": live_payment_summary(database, merchant_id),
        "whatsapp_replies": inbound_replies(database, merchant_id)[:5],
        "customer_recovery_batch": latest_batch(database, merchant_id),
        "recorded_capabilities": [
            "payment reliability findings",
            "agent investigations",
            "approval-gated interventions",
            "notification records",
            "signed WhatsApp replies",
            "approval-gated customer recovery links and their open/completion status",
        ],
    }


def _gemini_answer(
    *,
    api_key: str,
    model: str,
    question: str,
    history: list[dict[str, str]],
    facts: dict[str, Any],
) -> str:
    """Use Gemini as a language layer, constrained to the supplied merchant facts."""

    import json

    from google import genai

    instructions = """You are Revenue SRE Copilot, helping a non-technical business owner.
Answer the user's question using ONLY the FACTS JSON below. Be direct, warm, and concise.
Explain technical words in plain business language. Never invent a payment, message, link,
customer action, email, or result. If the requested information is absent, say exactly that
it is not recorded in this merchant's data and state what the system does track. Do not give
instructions to deploy code or contact a customer. Do not expose other merchants' data.

Conversation context and FACTS JSON follow. The FACTS JSON is the source of truth;
ignore any older conversation statement that conflicts with it."""
    prompt = (
        f"{instructions}\n\n"
        f"RECENT CONVERSATION:\n{json.dumps(history[-8:], default=str)}\n\n"
        f"CURRENT USER QUESTION:\n{question}\n\n"
        f"FACTS JSON:\n{json.dumps(facts, default=str)}"
    )
    try:
        # Keep the client in a local variable. Chaining Client(...).models can let the
        # temporary client be closed by Python before the SDK sends its HTTP request.
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
    except Exception as exc:
        safe_message = _safe_error_detail(exc)
        raise RuntimeError(f"Gemini request failed: {safe_message}") from exc
    answer = getattr(response, "text", None)
    if not answer or not answer.strip():
        raise RuntimeError("Gemini returned an empty response")
    return answer.strip()


def _safe_error_detail(exc: Exception, *, limit: int = 240) -> str:
    """Keep a useful provider diagnostic while never returning an API key to the UI."""

    message = " ".join(str(exc).split())
    message = re.sub(r"AIza[0-9A-Za-z_-]+", "[redacted Gemini API key]", message)
    return message[:limit]


def draft_engineering_email(
    database: Database,
    merchant_id: str,
    notification_id: str,
    *,
    gemini_api_key: str | None,
    gemini_model: str,
) -> dict[str, str | None]:
    """Prepare a reviewable internal-email draft from only this merchant's evidence."""

    with database.connect() as connection:
        notification = connection.execute(
            """
            SELECT subject, body FROM notification_outbox
            WHERE notification_id = ? AND merchant_id = ? AND channel = 'email'
            """,
            (notification_id, merchant_id),
        ).fetchone()
    if notification is None:
        raise KeyError("Email notification not found")
    if not gemini_api_key:
        return {
            "subject": str(notification["subject"]),
            "body": str(notification["body"]),
            "model_used": "not_configured",
            "model_error": "Set GEMINI_API_KEY to generate an email draft.",
        }
    findings = merchant_findings(database, merchant_id)
    investigations = investigations_for_merchant(database, merchant_id)
    interventions = interventions_for_merchant(database, merchant_id)
    try:
        body = _gemini_answer(
            api_key=gemini_api_key,
            model=gemini_model,
            question=(
                "Draft the body of a concise internal email to the engineering team about this "
                "payment issue. Use only recorded facts. State the observed impact, the safe next "
                "step, and whether merchant approval is still needed. Do not use a greeting subject "
                "line or placeholders; write the email body only."
            ),
            history=[],
            facts={
                **_copilot_facts(database, merchant_id, findings, investigations, interventions),
                "email_notification": dict(notification),
            },
        )
    except Exception as exc:
        return {
            "subject": str(notification["subject"]),
            "body": str(notification["body"]),
            "model_used": "gemini_unavailable",
            "model_error": _safe_error_detail(exc),
        }
    return {
        "subject": str(notification["subject"]),
        "body": body,
        "model_used": "gemini",
        "model_error": None,
    }


def morning_brief(database: Database, merchant_id: str) -> dict[str, Any]:
    findings = merchant_findings(database, merchant_id)
    investigations = investigations_for_merchant(database, merchant_id)
    interventions = interventions_for_merchant(database, merchant_id)
    pending = [item for item in interventions if item["status"] == "awaiting_approval"]
    rollback = [item for item in interventions if item["status"] == "rollback_required"]
    verified = [
        item
        for item in interventions
        if item["status"] == "verified" and item.get("measurement") is not None
    ]
    references: list[dict[str, str]] = []
    if not findings:
        headline = "Payment health is normal this morning."
        detail = "No payment pattern is outside this merchant's normal baseline, so the agents have not taken action."
    elif rollback:
        headline = "One payment action needs rollback."
        detail = "The verifier found that the treated payment group performed worse than the comparison group."
        references.extend(_investigation_reference(item) for item in investigations)
    elif pending:
        headline = "One safe payment action needs your approval."
        detail = pending[0]["action_summary"]
        references.append(_finding_reference(findings[0]))
    elif verified:
        measurement = verified[0]["measurement"]
        treated = _pct(float(measurement["treated_success_rate"]))
        holdout = _pct(float(measurement["holdout_success_rate"]))
        baseline = _pct(float(measurement["baseline_success_rate"]))
        headline = "A safe recovery action improved the affected payment group."
        detail = (
            f"Success reached {treated}, compared with {holdout} in the unchanged group. "
            f"This is still below the normal {baseline} baseline, so the agents are continuing to monitor it."
        )
        references.append({"type": "intervention", "id": str(verified[0]["intervention_id"])})
    else:
        headline = f"{len(findings)} payment issue(s) are being monitored."
        top = findings[0]
        detail = (
            f"The largest change is in {top['payment_method'].upper()}: success moved from "
            f"{_pct(float(top['baseline_success_rate']))} to {_pct(float(top['observed_success_rate']))}."
        )
        references.append(_finding_reference(top))
    return {
        "headline": headline,
        "detail": detail,
        "agent_status": f"{len(investigations)} investigation(s) completed; {len(interventions)} decision(s) recorded.",
        "references": references,
    }


def ask_copilot(
    database: Database,
    merchant_id: str,
    question: str,
    *,
    gemini_api_key: str | None = None,
    gemini_model: str = "gemini-3.5-flash",
) -> dict[str, Any]:
    """Answer from retrieved merchant facts, never from cross-tenant data."""

    query = question.lower().strip()
    findings = merchant_findings(database, merchant_id)
    investigations = investigations_for_merchant(database, merchant_id)
    interventions = interventions_for_merchant(database, merchant_id)
    replies = inbound_replies(database, merchant_id)
    customer_recovery = latest_batch(database, merchant_id)
    references: list[dict[str, str]] = []
    if any(
        term in query
        for term in ("payment link", "payment_link", "checkout link", "clicked", "click")
    ):
        if customer_recovery is None:
            answer = "No approved customer-recovery payment-link batch is recorded for this merchant yet."
        else:
            recipients = customer_recovery["recipients"]
            opened = sum(
                item["status"] in {"opened", "completed", "follow_up_sent"} for item in recipients
            )
            completed = sum(item["status"] == "completed" for item in recipients)
            follow_ups = sum(item["status"] == "follow_up_sent" for item in recipients)
            answer = (
                f"The customer-recovery batch is {customer_recovery['status']}. "
                f"{opened} of {len(recipients)} test links have been opened and {completed} test payments "
                f"have been marked complete. {follow_ups} five-minute Gemini follow-up message(s) have been sent."
            )
    elif any(term in query for term in ("whatsapp", "reply", "replied", "respond", "response")):
        if replies:
            latest_reply = replies[0]
            answer = (
                f"The latest signed WhatsApp reply says: ‘{latest_reply['body']}’ "
                f"{latest_reply['interpretation']}"
            )
        else:
            answer = "No WhatsApp reply has been received from the configured test number yet."
    elif any(term in query for term in ("approval", "approve", "decision")):
        pending = [item for item in interventions if item["status"] == "awaiting_approval"]
        if pending:
            answer = f"You need to approve this safe action: {pending[0]['action_summary']}"
            references.append({"type": "intervention", "id": str(pending[0]["intervention_id"])})
        else:
            answer = "There is no payment action waiting for your approval right now."
    elif any(
        term in query for term in ("how do", "how can", "fix this", "next step", "what should")
    ):
        pending = [item for item in interventions if item["status"] == "awaiting_approval"]
        if pending:
            answer = (
                f"The next safe step is to approve this action: {pending[0]['action_summary']} "
                "After approval, run it from the Decisions tab and the verifier will compare results."
            )
            references.append({"type": "intervention", "id": str(pending[0]["intervention_id"])})
        elif investigations:
            item = investigations[0]
            answer = (
                f"The agents recommend: {item['proposed_action']}. "
                "Open Agent activity to review the evidence, then request an approval-gated action."
            )
            references.append(_investigation_reference(item))
        elif findings:
            answer = "Start an investigation from Payment health first. The agents need evidence before they can propose a safe action."
            references.append(_finding_reference(findings[0]))
        else:
            answer = "There is no detected payment issue to fix right now."
    elif any(term in query for term in ("work", "result", "fix", "recover", "improve")):
        measured = [item for item in interventions if item.get("measurement")]
        if measured:
            measurement = measured[0]["measurement"]
            answer = (
                f"The latest verified action is {measurement['outcome'].replace('_', ' ')}. "
                f"Treated payment success was {_pct(float(measurement['treated_success_rate']))}, "
                f"compared with {_pct(float(measurement['holdout_success_rate']))} in the untreated comparison group."
            )
            references.append({"type": "intervention", "id": str(measured[0]["intervention_id"])})
        else:
            answer = "No action has been verified yet, so I cannot claim that a fix worked."
    elif any(term in query for term in ("agent", "check", "investigat", "why")):
        if investigations:
            item = investigations[0]
            answer = (
                f"The agents concluded this is a {item['scope']}-level issue with "
                f"{_pct(float(item['confidence']))} confidence. {item['root_cause_summary']}"
            )
            references.append(_investigation_reference(item))
        elif findings:
            top = findings[0]
            answer = (
                f"We detected a {top['severity']} payment-health change: {top['payment_method'].upper()} "
                f"success moved from {_pct(float(top['baseline_success_rate']))} to "
                f"{_pct(float(top['observed_success_rate']))}. Start an investigation to learn the likely scope."
            )
            references.append(_finding_reference(top))
        else:
            answer = "Payment health is within the merchant's normal range, so there is nothing to investigate."
    else:
        brief = morning_brief(database, merchant_id)
        answer, references = f"{brief['headline']} {brief['detail']}", brief["references"]
        live = live_payment_summary(database, merchant_id)
        if live["attempts"]:
            live_rate = (
                _pct(float(live["success_rate"]))
                if live["success_rate"] is not None
                else "not available"
            )
            answer += f" In the last hour, {live['attempts']} live demo event(s) were received at {live_rate} success."
    model_used = "deterministic_fallback"
    model_error: str | None = None
    if gemini_api_key:
        try:
            history = copilot_history(database, merchant_id)
            answer = _gemini_answer(
                api_key=gemini_api_key,
                model=gemini_model,
                question=question,
                history=history,
                facts=_copilot_facts(
                    database, merchant_id, findings, investigations, interventions
                ),
            )
            model_used = "gemini"
        except Exception as exc:
            safe_message = _safe_error_detail(exc)
            model_error = f"Copilot language layer was unavailable: {safe_message}"
            # When a Gemini key is configured, never present a deterministic answer
            # as though Gemini had written it. Make the outage visible instead.
            answer = (
                "Gemini could not answer right now, so I have not substituted a template response. "
                "Check this computer's connection to Google Gemini and try again."
            )
            model_used = "gemini_unavailable"
    try:
        _remember(database, merchant_id, "user", question)
        _remember(database, merchant_id, "assistant", answer)
    except Exception as exc:
        safe_message = _safe_error_detail(exc, limit=160)
        model_error = model_error or f"Copilot answer was not saved to history: {safe_message}"
    return {
        "answer": answer,
        "references": references,
        "fact_bounded": True,
        "model_used": model_used,
        "model_error": model_error,
    }
