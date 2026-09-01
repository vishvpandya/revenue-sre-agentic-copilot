"""Environment-backed application configuration."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_path: Path = Path("data/recovery_orchestrator.sqlite3")
    admin_api_token: SecretStr | None = None
    backend_url: str = "http://127.0.0.1:8010"

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_thinking_level: str = "low"

    razorpay_key_id: SecretStr | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    resend_api_key: SecretStr | None = None
    resend_from_email: str | None = None
    # A controlled address used for the hackathon proof. Synthetic contacts are never real targets.
    resend_test_recipient: str | None = None

    # Twilio WhatsApp Sandbox. The recipient is deliberately a single personal
    # test number, never one of the synthetic merchant contacts.
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_whatsapp_from: str | None = None
    twilio_test_recipient: str | None = None
    # Comma-separated, Sandbox-joined test recipients for the customer-recovery demo.
    # This is capped in code so a demo cannot fan out unexpectedly.
    twilio_customer_test_recipients: str | None = None
    # Required for validating a real Twilio webhook sent through a temporary tunnel.
    twilio_webhook_base_url: str | None = None
