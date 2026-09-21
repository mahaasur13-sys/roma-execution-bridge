"""DecisionOS — Customer Support Chat Module."""

from __future__ import annotations

from support_chat.models import (
    SupportTicket,
    ChatMessage,
    ChatParticipant,
    TicketAttachment,
    CsatRating,
    TicketStatus,
    TicketPriority,
    ParticipantRole,
    MessageType,
    CreateTicketRequest,
    CreateTicketResponse,
    CreateMessageRequest,
    TicketListResponse,
    TicketDetailResponse,
    AssignTicketRequest,
    CsatSubmitRequest,
    TicketSearchRequest,
)
from support_chat.service import SupportTicketService
from support_chat.chat_service import ChatService
from support_chat.router import router as support_router

__all__ = [
    "SupportTicket",
    "ChatMessage",
    "ChatParticipant",
    "TicketAttachment",
    "CsatRating",
    "TicketStatus",
    "TicketPriority",
    "ParticipantRole",
    "MessageType",
    "CreateTicketRequest",
    "CreateTicketResponse",
    "CreateMessageRequest",
    "TicketListResponse",
    "TicketDetailResponse",
    "AssignTicketRequest",
    "CsatSubmitRequest",
    "TicketSearchRequest",
    "SupportTicketService",
    "ChatService",
    "support_router",
]
