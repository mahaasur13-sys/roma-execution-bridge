"""Support Chat — SupportTicketService."""
from __future__ import annotations

from datetime import datetime, timezone

from structlog import get_logger

from support_chat.chat_service import ChatService
from support_chat.models import (
    AssignTicketRequest, ChatMessage, ChatParticipant, CreateMessageRequest,
    CreateTicketRequest, CreateTicketResponse, CsatRating, CsatSubmitRequest,
    ParticipantRole, SupportTicket, TicketAttachment, TicketDetailResponse,
    TicketListResponse, TicketStatus,
)
from support_chat.settings import SupportSettings

logger = get_logger(__name__)

VALID_STATUS_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    TicketStatus.OPEN: [TicketStatus.IN_PROGRESS, TicketStatus.CLOSED],
    TicketStatus.IN_PROGRESS: [TicketStatus.WAITING_CUSTOMER, TicketStatus.RESOLVED, TicketStatus.CLOSED],
    TicketStatus.WAITING_CUSTOMER: [TicketStatus.IN_PROGRESS, TicketStatus.CLOSED],
    TicketStatus.RESOLVED: [TicketStatus.CLOSED, TicketStatus.OPEN],
    TicketStatus.CLOSED: [TicketStatus.OPEN],
}


class SupportTicketService:
    def __init__(self, chat_service: ChatService | None = None, settings: SupportSettings | None = None) -> None:
        self._chat = chat_service or ChatService()
        self._settings = settings or SupportSettings()
        self._tickets: dict[str, SupportTicket] = {}
        self._participants: dict[str, list[ChatParticipant]] = {}
        self._attachments: dict[str, list[TicketAttachment]] = {}
        self._csat: dict[str, CsatRating] = {}

    async def create_ticket(self, request: CreateTicketRequest, user_id: str) -> CreateTicketResponse:
        ticket = SupportTicket(
            tenant_id=request.tenant_id,
            subject=request.subject,
            body=request.body,
            priority=request.priority,
            context_type=request.context_type,
            context_id=request.context_id,
            context_data=request.context_data,
            created_by=user_id,
        )
        tid = str(ticket.ticket_id)
        self._tickets[tid] = ticket

        participant = ChatParticipant(
            ticket_id=ticket.ticket_id,
            user_id=user_id,
            role=ParticipantRole.TENANT_USER,
            tenant_id=request.tenant_id,
        )
        self._participants.setdefault(tid, []).append(participant)

        if request.body:
            msg = ChatMessage(
                ticket_id=ticket.ticket_id,
                sender_id=user_id,
                sender_role=ParticipantRole.TENANT_USER,
                body=request.body,
            )
            await self._chat.add_message(msg)

        await self._chat.add_system_message(ticket.ticket_id, f"Ticket created by {user_id}")
        logger.info("support_ticket_created", ticket_id=tid, tenant_id=request.tenant_id, context_type=request.context_type)
        return CreateTicketResponse(
            ticket_id=ticket.ticket_id,
            tenant_id=ticket.tenant_id,
            subject=ticket.subject,
            status=ticket.status,
            priority=ticket.priority,
            created_at=ticket.created_at,
            context_type=ticket.context_type,
            context_id=ticket.context_id,
        )

    async def add_message(self, request: CreateMessageRequest) -> ChatMessage:
        ticket = self._tickets.get(str(request.ticket_id))
        if not ticket:
            raise ValueError(f"Ticket {request.ticket_id} not found")
        msg = ChatMessage(
            ticket_id=request.ticket_id,
            sender_id=request.sender_id,
            sender_role=request.sender_role,
            body=request.body,
            message_type=request.message_type,
            is_internal=request.is_internal,
            attachment_ids=request.attachment_ids,
        )
        if ticket.status == TicketStatus.WAITING_CUSTOMER and request.sender_role == ParticipantRole.TENANT_USER:
            ticket.status = TicketStatus.IN_PROGRESS
            await self._chat.add_system_message(request.ticket_id, "Customer replied — status → in_progress")
        ticket.updated_at = datetime.now(timezone.utc)
        return await self._chat.add_message(msg)

    async def assign_agent(self, ticket_id: str, request: AssignTicketRequest) -> SupportTicket:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")
        ticket.assigned_agent_id = request.agent_id
        ticket.status = TicketStatus.IN_PROGRESS
        ticket.updated_at = datetime.now(timezone.utc)

        participant = ChatParticipant(
            ticket_id=ticket.ticket_id,
            user_id=request.agent_id,
            role=ParticipantRole.SUPPORT_AGENT,
            tenant_id=ticket.tenant_id,
        )
        self._participants.setdefault(ticket_id, []).append(participant)
        await self._chat.add_system_message(ticket.ticket_id, f"Agent {request.agent_id} assigned")
        logger.info("support_agent_assigned", ticket_id=ticket_id, agent_id=request.agent_id)
        return ticket

    def get_ticket(self, ticket_id: str, tenant_id: str | None = None) -> SupportTicket | None:
        ticket = self._tickets.get(ticket_id)
        if ticket and tenant_id and ticket.tenant_id != tenant_id:
            return None
        return ticket

    def list_tickets(self, tenant_id: str, status: TicketStatus | None = None, page: int = 1, page_size: int = 20) -> TicketListResponse:
        tickets = [t for t in self._tickets.values() if t.tenant_id == tenant_id]
        if status:
            tickets = [t for t in tickets if t.status == status]
        total = len(tickets)
        start = (page - 1) * page_size
        return TicketListResponse(
            tickets=[t.model_dump(mode="json") for t in tickets[start:start + page_size]],
            total=total, page=page, page_size=page_size,
        )

    def get_ticket_detail(self, ticket_id: str, user_role: str, tenant_id: str) -> TicketDetailResponse | None:
        ticket = self.get_ticket(ticket_id, tenant_id)
        if not ticket:
            return None
        messages = self._chat.get_visible_messages(ticket_id, user_role)
        participants = self._participants.get(ticket_id, [])
        attachments = self._attachments.get(ticket_id, [])
        csat = self._csat.get(ticket_id)
        return TicketDetailResponse(
            ticket=ticket.model_dump(mode="json"),
            messages=[m.model_dump(mode="json") for m in messages],
            participants=[p.model_dump(mode="json") for p in participants],
            attachments=[a.model_dump(mode="json") for a in attachments],
            csat=csat.model_dump(mode="json") if csat else None,
        )

    async def transition_status(self, ticket_id: str, new_status: TicketStatus) -> SupportTicket:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")
        allowed = VALID_STATUS_TRANSITIONS.get(ticket.status, [])
        if new_status not in allowed:
            raise ValueError(f"Cannot transition from {ticket.status} to {new_status}")
        ticket.status = new_status
        ticket.updated_at = datetime.now(timezone.utc)
        if new_status == TicketStatus.RESOLVED:
            ticket.resolved_at = datetime.now(timezone.utc)
        await self._chat.add_system_message(ticket.ticket_id, f"Status → {new_status.value}")
        logger.info("support_ticket_transition", ticket_id=ticket_id, old_status=ticket.status.value, new_status=new_status.value)
        return ticket

    async def submit_csat(self, ticket_id: str, request: CsatSubmitRequest, user_id: str) -> CsatRating:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")
        rating = CsatRating(
            ticket_id=ticket.ticket_id,
            tenant_id=ticket.tenant_id,
            score=request.score,
            comment=request.comment,
            rated_by=user_id,
        )
        self._csat[ticket_id] = rating
        await self._chat.add_system_message(ticket.ticket_id, f"CSAT: {request.score}/5")
        logger.info("support_csat_submitted", ticket_id=ticket_id, score=request.score)
        return rating

    def add_attachment(self, attachment: TicketAttachment) -> TicketAttachment:
        tid = str(attachment.ticket_id)
        self._attachments.setdefault(tid, []).append(attachment)
        return attachment

    def add_participant(self, participant: ChatParticipant) -> ChatParticipant:
        tid = str(participant.ticket_id)
        self._participants.setdefault(tid, []).append(participant)
        return participant

    @property
    def chat_service(self) -> ChatService:
        return self._chat
