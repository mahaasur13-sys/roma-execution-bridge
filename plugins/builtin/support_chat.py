"""AI Support Chat Plugin — real-time support + ticket system."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("roma.plugin.support_chat")

TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")


class SupportChatPlugin:
    """Real-time AI support chat with ticket tracking."""

    name = "support-chat"
    display_name = "AI Support Chat"

    def __init__(self) -> None:
        self._tickets: dict[str, dict[str, Any]] = {}
        self._config: dict[str, Any] = {}

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config

    async def run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        if action == "send_message":
            return await self._handle_message(
                kwargs["tenant_id"], kwargs["message"], kwargs.get("user_name", "User")
            )
        elif action == "create_ticket":
            return await self._create_ticket(
                kwargs["tenant_id"], kwargs["subject"], kwargs["description"]
            )
        elif action == "get_ticket":
            return self._get_ticket(kwargs["ticket_id"])
        elif action == "resolve_ticket":
            return self._resolve_ticket(kwargs["ticket_id"])
        else:
            return {"error": f"Unknown action: {action}"}

    async def _handle_message(
        self, tenant_id: str, message: str, user_name: str
    ) -> dict[str, Any]:
        """Process a chat message with AI response."""
        logger.info("Chat from %s/%s: %s", tenant_id, user_name, message[:50])

        # Simple intent detection for demo
        intent = self._detect_intent(message)

        response = self._generate_response(intent, message, user_name)

        return {
            "user": user_name,
            "message": message,
            "response": response,
            "intent": intent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _create_ticket(
        self, tenant_id: str, subject: str, description: str
    ) -> dict[str, Any]:
        """Create a support ticket."""
        ticket_id = f"T-{len(self._tickets):06d}"
        ticket = {
            "ticket_id": ticket_id,
            "tenant_id": tenant_id,
            "subject": subject,
            "description": description,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def _get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return self._tickets.get(ticket_id, {"error": "Not found"})

    def _resolve_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            return {"error": "Not found"}
        ticket["status"] = "resolved"
        ticket["resolved_at"] = datetime.now(timezone.utc).isoformat()
        return ticket

    @staticmethod
    def _detect_intent(message: str) -> str:
        m = message.lower()
        if "submit" in m or "run" in m or "train" in m:
            return "job_submit"
        if "status" in m or "job" in m:
            return "job_status"
        if "billing" in m or "invoice" in m or "crypto" in m:
            return "billing"
        if "plugin" in m or "install" in m:
            return "plugin_help"
        if "error" in m or "fail" in m or "bug" in m:
            return "error_help"
        return "general"

    @staticmethod
    def _generate_response(intent: str, message: str, user_name: str) -> str:
        responses: dict[str, str] = {
            "job_submit": "Got it. I'll submit your job through the ROMA Execution Bridge. Use `/status` to track it.",
            "job_status": "You can check jobs at: GET /v1/jobs. I can fetch your recent ones — tell me your job ID.",
            "billing": "ROMA supports crypto (USDT/BTC/ETH/XMR via NOWPayments) and card payments. Pro plan unlocks crypto payments.",
            "plugin_help": "ROMA has a plugin marketplace! You can install new plugins with one command. Check `/v1/plugins/marketplace`.",
            "error_help": "I can help with errors. Share the error message and your tenant ID.",
            "general": f"Hi {user_name}! I'm the ROMA AI Assistant. I can help with job submission, billing, plugins, and support.",
        }
        return responses.get(intent, responses["general"])
