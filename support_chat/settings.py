"""Support Chat — configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings


class SupportSettings(BaseSettings):
    model_config = {"env_prefix": "SUPPORT_", "env_file": ".env", "extra": "ignore"}

    max_messages_per_ticket: int = 1000
    max_attachments_per_message: int = 10
    max_attachment_size_mb: int = 25
    auto_close_days: int = 7
    csat_reminder_hours: int = 24
    ws_heartbeat_seconds: int = 30
    ws_max_connections_per_tenant: int = 50
    notification_email_enabled: bool = True
    notification_from_email: str = "support@decisionos.io"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    enterprise_white_label_enabled: bool = False
