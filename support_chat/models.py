"""Support Chat — domain models (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ParticipantRole(StrEnum):
    TENANT_USER = "tenant_user"
    TENANT_ADMIN = "tenant_admin"
    SUPPORT_AGENT = "support_agent"
    SUPER_ADMIN = "super_admin"


class MessageType(StrEnum):
    TEXT = "text"
    SYSTEM = "system"
    INTERNAL_NOTE = "internal_note"
    FILE = "file"


class SupportTicket(BaseModel):
    ticket_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    subject: str
    body: str = ""
    status: TicketStatus = TicketStatus.OPEN
    priority: TicketPriority = TicketPriority.MEDIUM
    assigned_agent_id: str | None = None
    context_type: str | None = None
    context_id: UUID | None = None
    context_data: dict = Field(default_factory=dict)
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    message_id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    sender_id: str
    sender_role: ParticipantRole
    body: str
    message_type: MessageType = MessageType.TEXT
    is_internal: bool = False
    attachment_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatParticipant(BaseModel):
    participant_id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    user_id: str
    role: ParticipantRole
    tenant_id: str | None = None
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_online: bool = False


class TicketAttachment(BaseModel):
    attachment_id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    message_id: UUID | None = None
    filename: str
    content_type: str
    size_bytes: int = 0
    url: str
    uploaded_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CsatRating(BaseModel):
    rating_id: UUID = Field(default_factory=uuid4)
    ticket_id: UUID
    tenant_id: str
    score: int = Field(ge=1, le=5)
    comment: str | None = None
    rated_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CreateTicketRequest(BaseModel):
    tenant_id: str
    subject: str
    body: str = ""
    priority: TicketPriority = TicketPriority.MEDIUM
    context_type: str | None = None
    context_id: UUID | None = None
    context_data: dict = Field(default_factory=dict)


class CreateTicketResponse(BaseModel):
    ticket_id: UUID
    tenant_id: str
    subject: str
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    context_type: str | None
    context_id: UUID | None


class CreateMessageRequest(BaseModel):
    ticket_id: UUID
    body: str
    sender_id: str
    sender_role: ParticipantRole
    message_type: MessageType = MessageType.TEXT
    is_internal: bool = False
    attachment_ids: list[UUID] = Field(default_factory=list)


class TicketListResponse(BaseModel):
    tickets: list[dict]
    total: int
    page: int
    page_size: int


class TicketDetailResponse(BaseModel):
    ticket: dict
    messages: list[dict]
    participants: list[dict]
    attachments: list[dict]
    csat: dict | None


class AssignTicketRequest(BaseModel):
    agent_id: str


class CsatSubmitRequest(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = None


class TicketSearchRequest(BaseModel):
    tenant_id: str | None = None
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_agent_id: str | None = None
    context_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    from_date: datetime | None = None
    to_date: datetime | None = None
