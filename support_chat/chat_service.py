"""Support Chat — Chat Service (message persistence + WebSocket manager)."""
from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

from structlog import get_logger

from support_chat.models import ChatMessage, MessageType, ParticipantRole
from support_chat.settings import SupportSettings

logger = get_logger(__name__)


class ConnectionManager:
    """WebSocket connection manager — per-ticket rooms."""

    def __init__(self) -> None:
        self._rooms: dict[str, dict[str, object]] = {}
        self._user_rooms: dict[str, set[str]] = {}

    async def connect(self, ticket_id: str, user_id: str, ws: object) -> None:
        self._rooms.setdefault(ticket_id, {})[user_id] = ws
        self._user_rooms.setdefault(user_id, set()).add(ticket_id)
        logger.info("ws_connected", ticket_id=ticket_id, user_id=user_id)

    async def disconnect(self, ticket_id: str, user_id: str) -> None:
        room = self._rooms.get(ticket_id, {})
        room.pop(user_id, None)
        if not room:
            self._rooms.pop(ticket_id, None)
        rooms = self._user_rooms.get(user_id, set())
        rooms.discard(ticket_id)
        logger.info("ws_disconnected", ticket_id=ticket_id, user_id=user_id)

    async def broadcast(self, ticket_id: str, message: dict, exclude_user: str | None = None) -> None:
        room = self._rooms.get(ticket_id, {})
        payload = json.dumps(message)
        for uid, ws in list(room.items()):
            if uid == exclude_user:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                logger.warning("ws_send_failed", ticket_id=ticket_id, user_id=uid)

    async def send_to_user(self, user_id: str, message: dict) -> None:
        for ticket_id in self._user_rooms.get(user_id, set()):
            room = self._rooms.get(ticket_id, {})
            ws = room.get(user_id)
            if ws:
                try:
                    await ws.send_text(json.dumps(message))
                except Exception:
                    pass


class ChatService:
    def __init__(self, settings: SupportSettings | None = None) -> None:
        self._settings = settings or SupportSettings()
        self._messages: dict[str, list[ChatMessage]] = {}
        self._ws_manager = ConnectionManager()

    @property
    def ws_manager(self) -> ConnectionManager:
        return self._ws_manager

    async def add_message(self, message: ChatMessage) -> ChatMessage:
        tid = str(message.ticket_id)
        self._messages.setdefault(tid, []).append(message)
        ws_msg = self._to_ws_event(message)
        await self._ws_manager.broadcast(tid, ws_msg)
        logger.info("chat_message_added", ticket_id=tid, sender_id=message.sender_id)
        return message

    def get_messages(self, ticket_id: str, limit: int = 100) -> list[ChatMessage]:
        msgs = self._messages.get(ticket_id, [])
        return msgs[-limit:]

    def get_visible_messages(self, ticket_id: str, user_role: str, limit: int = 100) -> list[ChatMessage]:
        msgs = self.get_messages(ticket_id, limit)
        if user_role in (ParticipantRole.SUPPORT_AGENT.value, ParticipantRole.SUPER_ADMIN.value):
            return msgs
        return [m for m in msgs if not m.is_internal and m.message_type != MessageType.INTERNAL_NOTE]

    async def add_system_message(self, ticket_id: UUID, body: str) -> ChatMessage:
        msg = ChatMessage(
            ticket_id=ticket_id,
            sender_id="system",
            sender_role=ParticipantRole.SUPER_ADMIN,
            body=body,
            message_type=MessageType.SYSTEM,
        )
        return await self.add_message(msg)

    async def add_internal_note(self, ticket_id: UUID, agent_id: str, note: str) -> ChatMessage:
        msg = ChatMessage(
            ticket_id=ticket_id,
            sender_id=agent_id,
            sender_role=ParticipantRole.SUPPORT_AGENT,
            body=note,
            message_type=MessageType.INTERNAL_NOTE,
            is_internal=True,
        )
        return await self.add_message(msg)

    def _to_ws_event(self, message: ChatMessage) -> dict:
        return {
            "type": "message",
            "message_id": str(message.message_id),
            "ticket_id": str(message.ticket_id),
            "sender_id": message.sender_id,
            "sender_role": message.sender_role.value,
            "body": message.body,
            "message_type": message.message_type.value,
            "is_internal": message.is_internal,
            "attachment_ids": [str(a) for a in message.attachment_ids],
            "created_at": message.created_at.isoformat(),
        }
