"""Support Chat — SupportTicketService (DB-backed ticket store)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from structlog import get_logger
from sqlalchemy import update

from support_chat.chat_service import ChatService
from support_chat.db import get_session_factory
from support_chat.db_models import SupportTicketModel
from support_chat.models import (
    AssignTicketRequest,
    ChatMessage,
    ChatParticipant,
    CreateMessageRequest,
    CreateTicketRequest,
    CreateTicketResponse,
    CsatRating,
    CsatSubmitRequest,
    ParticipantRole,
    SupportTicket,
    TicketAttachment,
    TicketDetailResponse,
    TicketListResponse,
    TicketPriority,
    TicketStatus,
)
from support_chat.settings import SupportSettings

logger = get_logger(__name__)

VALID_STATUS_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    TicketStatus.OPEN: [TicketStatus.IN_PROGRESS, TicketStatus.CLOSED],
    TicketStatus.IN_PROGRESS: [
        TicketStatus.WAITING_CUSTOMER,
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
    ],
    TicketStatus.WAITING_CUSTOMER: [TicketStatus.IN_PROGRESS, TicketStatus.CLOSED],
    TicketStatus.RESOLVED: [TicketStatus.CLOSED, TicketStatus.OPEN],
    TicketStatus.CLOSED: [TicketStatus.OPEN],
}


def _coerce_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a model datetime to tz-aware UTC (SQLite stores naive datetimes)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ticket_to_model(ticket: SupportTicket) -> SupportTicketModel:
    return SupportTicketModel(
        ticket_id=ticket.ticket_id,
        tenant_id=ticket.tenant_id,
        subject=ticket.subject,
        body=ticket.body,
        status=ticket.status.value,
        priority=ticket.priority.value,
        assigned_agent_id=ticket.assigned_agent_id,
        context_type=ticket.context_type,
        context_id=ticket.context_id,
        context_data=ticket.context_data,
        created_by=ticket.created_by,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        resolved_at=ticket.resolved_at,
        tags=ticket.tags,
    )


def _model_to_ticket(model: SupportTicketModel) -> SupportTicket:
    return SupportTicket(
        ticket_id=model.ticket_id,
        tenant_id=model.tenant_id,
        subject=model.subject,
        body=model.body,
        status=TicketStatus(model.status),
        priority=TicketPriority(model.priority),
        assigned_agent_id=model.assigned_agent_id,
        context_type=model.context_type,
        context_id=model.context_id,
        context_data=model.context_data or {},
        created_by=model.created_by,
        created_at=_as_utc(model.created_at),
        updated_at=_as_utc(model.updated_at),
        resolved_at=_as_utc(model.resolved_at),
        tags=model.tags or [],
    )


class SupportTicketService:
    def __init__(
        self,
        chat_service: ChatService | None = None,
        settings: SupportSettings | None = None,
        session_factory=None,
    ) -> None:
        self._chat = chat_service or ChatService()
        self._settings = settings or SupportSettings()
        self._session_factory = session_factory or get_session_factory()
        self._tables_ready = False
        # Secondary collections remain in-memory (out of scope for P-1 БЛОКЕР-4):
        # participants / attachments / csat are auxiliary to the ticket record.
        self._participants: dict[str, list[ChatParticipant]] = {}
        self._attachments: dict[str, list[TicketAttachment]] = {}
        self._csat: dict[str, CsatRating] = {}

    def _ensure_tables(self) -> None:
        if self._tables_ready:
            return
        engine = self._session_factory.kw.get("bind")
        if engine is not None:
            from support_chat.db_models import Base

            Base.metadata.create_all(engine)
        self._tables_ready = True

    def _persist_ticket(self, ticket: SupportTicket) -> None:
        with self._session_factory() as session:
            session.add(_ticket_to_model(ticket))
            session.commit()

    async def create_ticket(
        self, request: CreateTicketRequest, user_id: str
    ) -> CreateTicketResponse:
        self._ensure_tables()
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
        self._persist_ticket(ticket)

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

        await self._chat.add_system_message(
            ticket.ticket_id, f"Ticket created by {user_id}"
        )
        logger.info(
            "support_ticket_created",
            ticket_id=tid,
            tenant_id=request.tenant_id,
            context_type=request.context_type,
        )
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
        self._ensure_tables()
        tid = _coerce_uuid(request.ticket_id)
        status_changed = False
        with self._session_factory() as session:
            model = session.get(SupportTicketModel, tid)
            if model is None:
                raise ValueError(f"Ticket {request.ticket_id} not found")
            if (
                TicketStatus(model.status) == TicketStatus.WAITING_CUSTOMER
                and request.sender_role == ParticipantRole.TENANT_USER
            ):
                model.status = TicketStatus.IN_PROGRESS.value
                status_changed = True
            model.updated_at = datetime.now(timezone.utc)
            session.commit()

        if status_changed:
            await self._chat.add_system_message(
                request.ticket_id, "Customer replied — status → in_progress"
            )

        msg = ChatMessage(
            ticket_id=request.ticket_id,
            sender_id=request.sender_id,
            sender_role=request.sender_role,
            body=request.body,
            message_type=request.message_type,
            is_internal=request.is_internal,
            attachment_ids=request.attachment_ids,
        )
        return await self._chat.add_message(msg)

    async def assign_agent(
        self, ticket_id: str, request: AssignTicketRequest
    ) -> SupportTicket:
        self._ensure_tables()
        tid = _coerce_uuid(ticket_id)
        with self._session_factory() as session:
            model = session.get(SupportTicketModel, tid)
            if model is None:
                raise ValueError(f"Ticket {ticket_id} not found")
            model.assigned_agent_id = request.agent_id
            model.status = TicketStatus.IN_PROGRESS.value
            model.updated_at = datetime.now(timezone.utc)
            session.commit()
            ticket = _model_to_ticket(model)

        participant = ChatParticipant(
            ticket_id=ticket.ticket_id,
            user_id=request.agent_id,
            role=ParticipantRole.SUPPORT_AGENT,
            tenant_id=ticket.tenant_id,
        )
        self._participants.setdefault(ticket_id, []).append(participant)
        await self._chat.add_system_message(
            ticket.ticket_id, f"Agent {request.agent_id} assigned"
        )
        logger.info(
            "support_agent_assigned", ticket_id=ticket_id, agent_id=request.agent_id
        )
        return ticket

    def get_ticket(
        self, ticket_id: str, tenant_id: str | None = None
    ) -> SupportTicket | None:
        self._ensure_tables()
        tid = _coerce_uuid(ticket_id)
        if tid is None:
            return None
        with self._session_factory() as session:
            model = session.get(SupportTicketModel, tid)
            if model is None:
                return None
            ticket = _model_to_ticket(model)
        if tenant_id and ticket.tenant_id != tenant_id:
            return None
        return ticket

    def list_tickets(
        self,
        tenant_id: str,
        status: TicketStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TicketListResponse:
        self._ensure_tables()
        with self._session_factory() as session:
            q = session.query(SupportTicketModel).filter(
                SupportTicketModel.tenant_id == tenant_id
            )
            if status is not None:
                q = q.filter(SupportTicketModel.status == status.value)
            total = q.count()
            rows = (
                q.order_by(SupportTicketModel.created_at.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            tickets = [_model_to_ticket(m) for m in rows]
        return TicketListResponse(
            tickets=[t.model_dump(mode="json") for t in tickets],
            total=total,
            page=page,
            page_size=page_size,
        )

    def get_ticket_detail(
        self, ticket_id: str, user_role: str, tenant_id: str
    ) -> TicketDetailResponse | None:
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

    async def transition_status(
        self, ticket_id: str, new_status: TicketStatus
    ) -> SupportTicket:
        self._ensure_tables()
        tid = _coerce_uuid(ticket_id)
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            model = session.get(SupportTicketModel, tid)
            if model is None:
                raise ValueError(f"Ticket {ticket_id} not found")
            current = TicketStatus(model.status)
            allowed = VALID_STATUS_TRANSITIONS.get(current, [])
            if new_status not in allowed:
                raise ValueError(
                    f"Cannot transition from {current.value} to {new_status.value}"
                )
            values: dict = {"status": new_status.value, "updated_at": now}
            if new_status == TicketStatus.RESOLVED:
                values["resolved_at"] = now
            result = session.execute(
                update(SupportTicketModel)
                .where(
                    SupportTicketModel.ticket_id == tid,
                    SupportTicketModel.status == current.value,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                raise ValueError(
                    f"Cannot transition from {current.value} to {new_status.value} "
                    "(status changed concurrently)"
                )
            session.commit()
            session.refresh(model)
            ticket = _model_to_ticket(model)

        await self._chat.add_system_message(
            ticket.ticket_id, f"Status → {new_status.value}"
        )
        logger.info(
            "support_ticket_transition",
            ticket_id=ticket_id,
            old_status=current.value,
            new_status=new_status.value,
        )
        return ticket

    async def submit_csat(
        self, ticket_id: str, request: CsatSubmitRequest, user_id: str
    ) -> CsatRating:
        ticket = self.get_ticket(ticket_id)
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
        await self._chat.add_system_message(
            ticket.ticket_id, f"CSAT: {request.score}/5"
        )
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
