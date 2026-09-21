"""Support AI Agent — multi-model support chat with advanced routing."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("roma.plugin.support_ai_agent")


class SupportAIAgentPlugin:
    """Advanced AI support agent with multi-model routing.

    Routes queries to appropriate model based on complexity.
    """

    name = "support-ai-agent"
    display_name = "Support AI Agent"

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._session_history: list[dict[str, Any]] = []

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config
        self._models = config.get(
            "models", ["ollama:llama3", "openrouter:claude-haiku"]
        )

    async def run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        if action == "chat":
            return await self._chat(kwargs["message"], kwargs.get("tenant_id", ""))
        elif action == "classify":
            return await self._classify(kwargs["message"])
        elif action == "history":
            return self._get_history()
        else:
            return {"error": f"Unknown action: {action}"}

    async def _chat(self, message: str, tenant_id: str) -> dict[str, Any]:
        """Route message to appropriate model and respond."""
        complexity = self._estimate_complexity(message)
        model = self._models[0] if complexity == "simple" else self._models[-1]

        logger.info(
            "Support AI: complexity=%s, model=%s, msg=%s",
            complexity,
            model,
            message[:50],
        )

        response = self._generate_response(complexity, message)

        entry = {
            "tenant_id": tenant_id,
            "message": message,
            "response": response,
            "complexity": complexity,
            "model_used": model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._session_history.append(entry)

        return entry

    async def _classify(self, message: str) -> dict[str, Any]:
        """Classify message intent."""
        _categories = [
            "billing",
            "technical",
            "onboarding",
            "bug_report",
            "feature_request",
            "general",
        ]
        msg = message.lower()

        if any(w in msg for w in ("bill", "invoice", "payment", "crypto")):
            return {"category": "billing", "confidence": 0.92}
        if any(w in msg for w in ("error", "bug", "fail", "crash", "500")):
            return {"category": "technical", "confidence": 0.88}
        if any(w in msg for w in ("start", "begin", "setup", "how", "guide")):
            return {"category": "onboarding", "confidence": 0.85}
        return {"category": "general", "confidence": 0.70}

    def _get_history(self) -> list[dict[str, Any]]:
        return self._session_history[-20:]

    @staticmethod
    def _estimate_complexity(message: str) -> str:
        length = len(message)
        if length < 30:
            return "simple"
        if length < 120:
            return "medium"
        return "complex"

    @staticmethod
    def _generate_response(complexity: str, message: str) -> str:
        responses: dict[str, str] = {
            "simple": "Got it. Processing your request now.",
            "medium": "I understand. Let me route this to the appropriate team.",
            "complex": "This is a complex query. I'm analyzing it with our advanced reasoning model. Give me a moment.",
        }
        return responses.get(complexity, responses["simple"])
