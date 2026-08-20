"""ROMA Alerts — dispatcher: routes alerts to configured channels."""

import os
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone

from alerts.telegram import send_telegram_alert
from alerts.discord import send_discord_alert
from alerts.email import send_email_alert

logger = logging.getLogger("roma.alerts.dispatcher")


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Structured alert event."""
    level: AlertLevel
    title: str
    body: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def format_markdown(self) -> str:
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(self.level.value, "📢")
        return f"{emoji} *{self.title}*\n\n{self.body}\n\n`{self.timestamp}` — ROMA v2.1.0"

    def format_plain(self) -> str:
        prefix = {"info": "[INFO]", "warning": "[WARNING]", "critical": "[CRITICAL]"}.get(self.level.value, "[ALERT]")
        return f"{prefix} {self.title}\n\n{self.body}\n\n{self.timestamp} — ROMA v2.1.0"


class AlertDispatcher:
    """Singleton dispatcher. Reads channel config from env vars. Non-blocking fire-and-forget."""

    def __init__(self):
        self._telegram_enabled = os.getenv("ALERT_TELEGRAM_ENABLED", "").lower() in ("1", "true", "yes")
        self._discord_enabled = os.getenv("ALERT_DISCORD_ENABLED", "").lower() in ("1", "true", "yes")
        self._email_enabled = os.getenv("ALERT_EMAIL_ENABLED", "").lower() in ("1", "true", "yes")

        # Telegram
        self._tg_bot_token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")

        # Discord
        self._discord_webhook_url = os.getenv("ALERT_DISCORD_WEBHOOK_URL", "")

        # Email
        self._smtp_host = os.getenv("ALERT_EMAIL_SMTP_HOST", "")
        self._smtp_port = int(os.getenv("ALERT_EMAIL_SMTP_PORT", "587"))
        self._smtp_user = os.getenv("ALERT_EMAIL_SMTP_USER", "")
        self._smtp_password = os.getenv("ALERT_EMAIL_SMTP_PASSWORD", "")
        self._email_from = os.getenv("ALERT_EMAIL_FROM", "alerts@roma-execution-bridge.io")
        self._email_to = os.getenv("ALERT_EMAIL_TO", "")

        channels = []
        if self._telegram_enabled:
            channels.append("telegram")
        if self._discord_enabled:
            channels.append("discord")
        if self._email_enabled:
            channels.append("email")

        if channels:
            logger.info("AlertDispatcher initialized: channels=%s", ", ".join(channels))
        else:
            logger.info("AlertDispatcher initialized: no channels enabled (alerts are silent)")

    def send(self, alert: Alert) -> None:
        """Fire-and-forget: dispatch alert to all enabled channels in background thread."""
        if not any([self._telegram_enabled, self._discord_enabled, self._email_enabled]):
            return

        def _dispatch():
            md = alert.format_markdown()
            plain = alert.format_plain()

            if self._telegram_enabled:
                send_telegram_alert(self._tg_bot_token, self._tg_chat_id, md)

            if self._discord_enabled:
                send_discord_alert(self._discord_webhook_url, md)

            if self._email_enabled:
                send_email_alert(
                    smtp_host=self._smtp_host,
                    smtp_port=self._smtp_port,
                    smtp_user=self._smtp_user,
                    smtp_password=self._smtp_password,
                    from_addr=self._email_from,
                    to_addr=self._email_to,
                    subject=alert.title,
                    body=plain,
                )

        threading.Thread(target=_dispatch, daemon=True).start()

    @property
    def channels_status(self) -> dict:
        """Return enabled/disabled status for all channels."""
        return {
            "telegram": {"enabled": self._telegram_enabled, "chat_id": self._tg_chat_id[:8] + "..." if self._tg_chat_id else None},
            "discord": {"enabled": self._discord_enabled, "webhook_configured": bool(self._discord_webhook_url)},
            "email": {"enabled": self._email_enabled, "to": self._email_to},
        }
