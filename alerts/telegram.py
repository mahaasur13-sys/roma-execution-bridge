"""ROMA Alerts — Telegram channel via Bot API."""

import logging
import urllib.request
import json

logger = logging.getLogger("roma.alerts.telegram")


def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> bool:
    """Send alert message via Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        logger.warning("Telegram alert skipped: bot_token or chat_id not configured")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                logger.info("Telegram alert sent to chat %s", chat_id)
                return True
            else:
                logger.error(
                    "Telegram API error: %s", result.get("description", "unknown")
                )
                return False
    except Exception as exc:
        logger.error("Telegram alert failed: %s", exc)
        return False
