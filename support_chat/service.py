"""Support Chat — SupportTicketService (DB-backed ticket store)."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
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
    MessageType,
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


class TicketNotFoundError(ValueError):
    """Raised when a support ticket does not exist."""


class TicketStatusConflictError(ValueError):
    """Raised when ticket status prevents or races with an assignment."""


@dataclass(frozen=True)
class TenantContext:
    """Sanitised tenant identity derived from a verified API key.

    H1 boundary: the raw key is dropped here and is never stored, logged,
    returned or used as an actor id. H1 has no trusted per-user role yet, so the
    only role a tenant key can act with is enforced at the call sites as
    ``TENANT_USER``; privileged operations stay fail-closed until H2.
    """

    tenant_id: str
    actor_id: str

    @classmethod
    def from_principal(cls, principal: dict) -> TenantContext:
        """Reduce a verified principal to tenant_id + a synthetic actor id."""
        tenant_id = str(principal.get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError("verified principal carries no tenant_id")
        return cls(tenant_id=tenant_id, actor_id=f"tenant:{tenant_id}")


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
        # Lazy: the default factory is built on the first DB operation, never at
        # import time — importing the router/app without PG_DSN must neither open
        # a connection nor create a file.
        self._session_factory = session_factory
        self._factory_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._tables_ready = False
        # Secondary collections remain in-memory (out of scope for P-1 БЛОКЕР-4):
        # participants / attachments / csat are auxiliary to the ticket record.
        self._participants: dict[str, list[ChatParticipant]] = {}
        self._attachments: dict[str, list[TicketAttachment]] = {}
        self._csat: dict[str, CsatRating] = {}

    def _factory(self):
        """Session factory for the resolved URL (built lazily, exactly once)."""
        factory = self._session_factory
        if factory is None:
            with self._factory_lock:
                if self._session_factory is None:
                    self._session_factory = get_session_factory()
                factory = self._session_factory
        return factory

    def _ensure_tables_sync(self) -> None:
        """Worker-only schema init.

        PostgreSQL schema is owned by migration 013, so nothing is created on
        that path; SQLite (explicit test/local URL) still needs lazy DDL. The
        lock + double check keep concurrent worker threads from racing DDL.
        """
        if self._tables_ready:
            return
        with self._schema_lock:
            if self._tables_ready:
                return
            with self._factory()() as session:
                bind = session.get_bind()
                if bind.dialect.name == "sqlite":
                    from support_chat.db_models import Base

                    Base.metadata.create_all(bind)
            self._tables_ready = True

    def _insert_ticket_sync(self, ticket: SupportTicket) -> None:
        self._ensure_tables_sync()
        with self._factory()() as session:
            session.add(_ticket_to_model(ticket))
            session.commit()

    def _add_message_sync(
        self, ticket_id: str, tenant_id: str
    ) -> tuple[UUID, bool]:
        self._ensure_tables_sync()
        tid = _coerce_uuid(ticket_id)
        status_changed = False
        with self._factory()() as session:
            # Tenant-scoped read: a ticket of another tenant is indistinguishable
            # from a missing one, so nothing is written and nothing is disclosed.
            model = (
                session.query(SupportTicketModel)
                .filter(
                    SupportTicketModel.ticket_id == tid,
                    SupportTicketModel.tenant_id == tenant_id,
                )
                .one_or_none()
            )
            if model is None:
                raise ValueError(f"Ticket {ticket_id} not found")
            # A tenant-level actor is always a tenant user, so a reply on a ticket
            # waiting on the customer still flips it back to in_progress.
            if TicketStatus(model.status) == TicketStatus.WAITING_CUSTOMER:
                model.status = TicketStatus.IN_PROGRESS.value
                status_changed = True
            model.updated_at = datetime.now(timezone.utc)
            session.commit()
            return model.ticket_id, status_changed

    def _assign_agent_sync(
        self, ticket_id: str, agent_id: str, tenant_id: str
    ) -> SupportTicket:
        self._ensure_tables_sync()
        tid = _coerce_uuid(ticket_id)
        with self._factory()() as session:
            # ``session.get`` stays the read path so the accepted E-15 stale-read
            # seam keeps working; the tenant is enforced on the loaded row and on
            # the CAS UPDATE below, so a foreign ticket is a not-found, not a write.
            model = session.get(SupportTicketModel, tid)
            if model is None or model.tenant_id != tenant_id:
                raise TicketNotFoundError(f"Ticket {ticket_id} not found")
            current = TicketStatus(model.status)
            allowed = VALID_STATUS_TRANSITIONS.get(current, [])
            if current is not TicketStatus.IN_PROGRESS and (
                TicketStatus.IN_PROGRESS not in allowed
            ):
                raise TicketStatusConflictError(
                    f"Ticket {ticket_id} is {current.value}; "
                    "assignment is not allowed from this status"
                )
            now = datetime.now(timezone.utc)
            # Optimistic status predicate: the read status is re-checked in the
            # UPDATE, so a concurrent transition cannot be overwritten.
            result = session.execute(
                update(SupportTicketModel)
                .where(
                    SupportTicketModel.ticket_id == tid,
                    SupportTicketModel.tenant_id == tenant_id,
                    SupportTicketModel.status == current.value,
                )
                .values(
                    assigned_agent_id=agent_id,
                    status=TicketStatus.IN_PROGRESS.value,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                raise TicketStatusConflictError(
                    f"Ticket {ticket_id} status changed concurrently; "
                    "assignment was not applied"
                )
            session.commit()
            session.refresh(model)
            return _model_to_ticket(model)

    def _get_ticket_sync(self, ticket_id: str, tenant_id: str) -> SupportTicket | None:
        self._ensure_tables_sync()
        tid = _coerce_uuid(ticket_id)
        if tid is None:
            return None
        with self._factory()() as session:
            model = (
                session.query(SupportTicketModel)
                .filter(
                    SupportTicketModel.ticket_id == tid,
                    SupportTicketModel.tenant_id == tenant_id,
                )
                .one_or_none()
            )
            if model is None:
                return None
            return _model_to_ticket(model)

    def _list_tickets_sync(
        self,
        tenant_id: str,
        status: TicketStatus | None,
        page: int,
        page_size: int,
    ) -> TicketListResponse:
        self._ensure_tables_sync()
        with self._factory()() as session:
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

    def _transition_sync(
        self, ticket_id: str, new_status: TicketStatus, tenant_id: str
    ) -> tuple[SupportTicket, str]:
        self._ensure_tables_sync()
        tid = _coerce_uuid(ticket_id)
        now = datetime.now(timezone.utc)
        with self._factory()() as session:
            model = (
                session.query(SupportTicketModel)
                .filter(
                    SupportTicketModel.ticket_id == tid,
                    SupportTicketModel.tenant_id == tenant_id,
                )
                .one_or_none()
            )
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
                    SupportTicketModel.tenant_id == tenant_id,
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
            return _model_to_ticket(model), current.value

    async def create_ticket(
        self, request: CreateTicketRequest, tenant: TenantContext
    ) -> CreateTicketResponse:
        # The persisted tenant is the verified principal's tenant only: the
        # request schema no longer carries a tenant_id at all.
        ticket = SupportTicket(
            tenant_id=tenant.tenant_id,
            subject=request.subject,
            body=request.body,
            priority=request.priority,
            context_type=request.context_type,
            context_id=request.context_id,
            context_data=request.context_data,
            created_by=tenant.actor_id,
        )
        tid = str(ticket.ticket_id)
        await asyncio.to_thread(self._insert_ticket_sync, ticket)

        participant = ChatParticipant(
            ticket_id=ticket.ticket_id,
            user_id=tenant.actor_id,
            role=ParticipantRole.TENANT_USER,
            tenant_id=tenant.tenant_id,
        )
        self._participants.setdefault(tid, []).append(participant)

        if request.body:
            msg = ChatMessage(
                ticket_id=ticket.ticket_id,
                sender_id=tenant.actor_id,
                sender_role=ParticipantRole.TENANT_USER,
                body=request.body,
            )
            await self._chat.add_message(msg)

        await self._chat.add_system_message(
            ticket.ticket_id, f"Ticket created by {tenant.actor_id}"
        )
        logger.info(
            "support_ticket_created",
            ticket_id=tid,
            tenant_id=tenant.tenant_id,
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

    async def add_message(
        self, ticket_id: str, request: CreateMessageRequest, tenant: TenantContext
    ) -> ChatMessage:
        # The path ticket id is authoritative; the tenant-scoped helper is the only
        # lookup, so a foreign or missing ticket fails before anything is written.
        msg_ticket_id, status_changed = await asyncio.to_thread(
            self._add_message_sync, ticket_id, tenant.tenant_id
        )

        if status_changed:
            await self._chat.add_system_message(
                msg_ticket_id, "Customer replied — status → in_progress"
            )

        msg = ChatMessage(
            ticket_id=msg_ticket_id,
            sender_id=tenant.actor_id,
            sender_role=ParticipantRole.TENANT_USER,
            body=request.body,
            message_type=MessageType.TEXT,
            is_internal=False,
            attachment_ids=request.attachment_ids,
        )
        return await self._chat.add_message(msg)

    async def assign_agent(
        self, ticket_id: str, request: AssignTicketRequest, tenant: TenantContext
    ) -> SupportTicket:
        ticket = await asyncio.to_thread(
            self._assign_agent_sync, ticket_id, request.agent_id, tenant.tenant_id
        )

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

    async def get_ticket(
        self, ticket_id: str, tenant: TenantContext
    ) -> SupportTicket | None:
        return await asyncio.to_thread(
            self._get_ticket_sync, ticket_id, tenant.tenant_id
        )

    async def list_tickets(
        self,
        tenant: TenantContext,
        status: TicketStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TicketListResponse:
        return await asyncio.to_thread(
            self._list_tickets_sync, tenant.tenant_id, status, page, page_size
        )

    async def get_ticket_detail(
        self, ticket_id: str, tenant: TenantContext
    ) -> TicketDetailResponse | None:
        ticket = await self.get_ticket(ticket_id, tenant)
        if not ticket:
            return None
        # H1: a tenant key carries no trusted role, so visibility is always the
        # tenant-user view — internal notes and internal-note messages stay hidden.
        messages = self._chat.get_visible_messages(
            ticket_id, ParticipantRole.TENANT_USER.value
        )
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
        self, ticket_id: str, new_status: TicketStatus, tenant: TenantContext
    ) -> SupportTicket:
        ticket, current = await asyncio.to_thread(
            self._transition_sync, ticket_id, new_status, tenant.tenant_id
        )

        await self._chat.add_system_message(
            ticket.ticket_id, f"Status → {new_status.value}"
        )
        logger.info(
            "support_ticket_transition",
            ticket_id=ticket_id,
            old_status=current,
            new_status=new_status.value,
        )
        return ticket

    async def submit_csat(
        self, ticket_id: str, request: CsatSubmitRequest, tenant: TenantContext
    ) -> CsatRating:
        ticket = await self.get_ticket(ticket_id, tenant)
        if not ticket:
            raise ValueError(f"Ticket {ticket_id} not found")
        rating = CsatRating(
            ticket_id=ticket.ticket_id,
            tenant_id=tenant.tenant_id,
            score=request.score,
            comment=request.comment,
            rated_by=tenant.actor_id,
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
