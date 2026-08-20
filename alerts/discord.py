"""ROMA Alerts — Discord channel via Webhook."""

import logging
import urllib.request
import json

logger = logging.getLogger("roma.alerts.discord")


def send_discord_alert(webhook_url: str, message: str) -> bool:
    """Send alert message via Discord webhook. Returns True on success."""
    if not webhook_url:
        logger.warning("Discord alert skipped: webhook_url not configured")
        return False

    # Discord webhooks support 2000 char limit
    content = message[:2000] if len(message) > 2000 else message

    payload = {
        "content": content,
        "username": "ROMA Execution Bridge",
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                logger.info("Discord alert sent")
                return True
            else:
                logger.error("Discord webhook returned %s: %s", resp.status, resp.read().decode()[:200])
                return False
    except Exception as exc:
        logger.error("Discord alert failed: %s", exc)
        return False
