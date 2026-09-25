"""DecisionOS Support Chat — 12 smoke tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from support_chat.models import (
    CsatRating,
    TicketStatus,
    TicketPriority,
    ParticipantRole,
    CreateTicketRequest,
    CreateMessageRequest,
    AssignTicketRequest,
    CsatSubmitRequest,
)
from support_chat.service import SupportTicketService
from support_chat.chat_service import ChatService
from support_chat.settings import SupportSettings
from support_chat.db import get_session_factory


@pytest.fixture
def settings() -> SupportSettings:
    return SupportSettings()


@pytest.fixture
def chat_service() -> ChatService:
    return ChatService()


@pytest.fixture
def service(chat_service: ChatService) -> SupportTicketService:
    sf = get_session_factory("sqlite:///:memory:")
    return SupportTicketService(chat_service=chat_service, session_factory=sf)


class TestTicketCreation:

    @pytest.mark.asyncio
    async def test_01_create_basic_ticket(self, service: SupportTicketService) -> None:
        """Smoke 1: Create basic support ticket."""
        req = CreateTicketRequest(
            tenant_id="t1",
            subject="Payment issue",
            body="My USDT payment didn't arrive",
            priority=TicketPriority.HIGH,
        )
        result = await service.create_ticket(req, "user1")
        assert result.ticket_id is not None
        assert result.status == TicketStatus.OPEN
        assert result.subject == "Payment issue"
        assert result.tenant_id == "t1"

    @pytest.mark.asyncio
    async def test_02_create_ticket_with_crypto_context(
        self, service: SupportTicketService
    ) -> None:
        """Smoke 2: Create ticket linked to crypto invoice."""
        invoice_id = uuid4()
        req = CreateTicketRequest(
            tenant_id="t1",
            subject="Invoice not confirmed",
            body="Invoice abc123 is stuck on pending",
            priority=TicketPriority.CRITICAL,
            context_type="crypto_invoice",
            context_id=invoice_id,
            context_data={
                "invoice_status": "pending",
                "network": "TRC20",
                "amount": "49.00",
            },
        )
        result = await service.create_ticket(req, "user1")
        assert result.context_type == "crypto_invoice"
        assert result.context_id == invoice_id

    @pytest.mark.asyncio
    async def test_03_create_ticket_with_monero_context(
        self, service: SupportTicketService
    ) -> None:
        """Smoke 3: Create ticket linked to Monero wallet."""
        wallet_id = uuid4()
        req = CreateTicketRequest(
            tenant_id="t1",
            subject="Monero subaddress issue",
            body="Subaddress not showing balance",
            context_type="crypto_wallet",
            context_id=wallet_id,
            context_data={
                "wallet_type": "monero",
                "subaddress": "8Abc...monero_sub",
                "subaddress_index": 5,
                "view_key_hash": "abc...",
            },
        )
        result = await service.create_ticket(req, "user1")
        assert result.context_type == "crypto_wallet"

    @pytest.mark.asyncio
    async def test_04_ticket_tenant_isolation(
        self, service: SupportTicketService
    ) -> None:
        """Smoke 4: Tenant isolation — tenant A can't see tenant B tickets."""
        req1 = CreateTicketRequest(tenant_id="t1", subject="T1 ticket")
        req2 = CreateTicketRequest(tenant_id="t2", subject="T2 ticket")
        await service.create_ticket(req1, "user1")
        await service.create_ticket(req2, "user2")

        t1_tickets = service.list_tickets("t1")
        t2_tickets = service.list_tickets("t2")
        assert t1_tickets.total == 1
        assert t2_tickets.total == 1
        t1_ticket_id = t1_tickets.tickets[0]["ticket_id"]
        assert service.get_ticket(t1_ticket_id, tenant_id="t2") is None


class TestMessages:

    @pytest.mark.asyncio
    async def test_05_add_message(self, service: SupportTicketService) -> None:
        """Smoke 5: Add message to ticket."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")

        msg_req = CreateMessageRequest(
            ticket_id=ticket.ticket_id,
            body="Hello, I need help",
            sender_id="user1",
            sender_role=ParticipantRole.TENANT_USER,
        )
        msg = await service.add_message(msg_req)
        assert msg.ticket_id == ticket.ticket_id
        assert msg.body == "Hello, I need help"

    @pytest.mark.asyncio
    async def test_06_internal_note_hidden_from_tenant(
        self, service: SupportTicketService, chat_service: ChatService
    ) -> None:
        """Smoke 6: Internal notes visible only to agents."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")

        await service.assign_agent(
            str(ticket.ticket_id), AssignTicketRequest(agent_id="agent1")
        )
        await chat_service.add_internal_note(
            ticket.ticket_id, "agent1", "Customer might be fraud"
        )

        visible_msgs = chat_service.get_visible_messages(
            str(ticket.ticket_id), ParticipantRole.TENANT_USER.value
        )
        assert not any(m.is_internal for m in visible_msgs)

        agent_msgs = chat_service.get_visible_messages(
            str(ticket.ticket_id), ParticipantRole.SUPPORT_AGENT.value
        )
        assert any(m.is_internal for m in agent_msgs)


class TestStatusTransitions:

    @pytest.mark.asyncio
    async def test_07_valid_transition(self, service: SupportTicketService) -> None:
        """Smoke 7: Valid status transition open → in_progress."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")
        updated = await service.transition_status(
            str(ticket.ticket_id), TicketStatus.IN_PROGRESS
        )
        assert updated.status == TicketStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_08_invalid_transition_blocked(
        self, service: SupportTicketService
    ) -> None:
        """Smoke 8: Invalid transition open → resolved blocked."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")
        with pytest.raises(ValueError, match="Cannot transition from open"):
            await service.transition_status(
                str(ticket.ticket_id), TicketStatus.RESOLVED
            )


class TestAgentAssignment:

    @pytest.mark.asyncio
    async def test_09_assign_agent(self, service: SupportTicketService) -> None:
        """Smoke 9: Assign agent to ticket."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")
        updated = await service.assign_agent(
            str(ticket.ticket_id), AssignTicketRequest(agent_id="agent42")
        )
        assert updated.assigned_agent_id == "agent42"
        assert updated.status == TicketStatus.IN_PROGRESS


class TestCSAT:

    @pytest.mark.asyncio
    async def test_10_submit_csat(self, service: SupportTicketService) -> None:
        """Smoke 10: Submit CSAT rating after resolution."""
        req = CreateTicketRequest(tenant_id="t1", subject="Test")
        ticket = await service.create_ticket(req, "user1")
        await service.transition_status(str(ticket.ticket_id), TicketStatus.IN_PROGRESS)
        await service.transition_status(str(ticket.ticket_id), TicketStatus.RESOLVED)

        csat = await service.submit_csat(
            str(ticket.ticket_id), CsatSubmitRequest(score=5, comment="Great!"), "user1"
        )
        assert csat.score == 5
        assert csat.comment == "Great!"


class TestModels:

    def test_11_ticket_status_enum_values(self) -> None:
        """TicketStatus enum has all 5 values."""
        assert len(TicketStatus) == 5
        assert TicketStatus.OPEN.value == "open"
        assert TicketStatus.CLOSED.value == "closed"

    def test_12_csat_rating_validation(self) -> None:
        """CsatRating score must be 1-5."""
        rating = CsatRating(ticket_id=uuid4(), tenant_id="t1", score=5, rated_by="u1")
        assert rating.score == 5
        with pytest.raises(Exception):
            CsatRating(ticket_id=uuid4(), tenant_id="t1", score=0, rated_by="u1")
        with pytest.raises(Exception):
            CsatRating(ticket_id=uuid4(), tenant_id="t1", score=6, rated_by="u1")


class TestPersistence:

    @pytest.mark.asyncio
    async def test_13_restart_persistence(self, tmp_path) -> None:
        """P-1 БЛОКЕР-4: ticket survives a service restart (new engine, same DB)."""
        db_url = f"sqlite:///{tmp_path}/support.db"
        sf1 = get_session_factory(db_url)
        service1 = SupportTicketService(session_factory=sf1)

        req = CreateTicketRequest(
            tenant_id="t1",
            subject="Persists across restart",
            body="created before restart",
            priority=TicketPriority.HIGH,
        )
        result = await service1.create_ticket(req, "user1")
        tid = result.ticket_id

        # "restart": brand-new engine/session factory over the same DB file.
        sf2 = get_session_factory(db_url)
        service2 = SupportTicketService(session_factory=sf2)
        ticket = service2.get_ticket(str(tid), tenant_id="t1")

        assert ticket is not None
        assert ticket.ticket_id == tid
        assert ticket.subject == "Persists across restart"
        assert ticket.status == TicketStatus.OPEN
        assert ticket.priority == TicketPriority.HIGH

    @pytest.mark.asyncio
    async def test_14_fail_closed_db_unavailable(self, tmp_path) -> None:
        """Fail-closed: unreachable DB raises — no silent in-memory fallback."""
        db_url = f"sqlite:///{tmp_path}/no_such_dir/support.db"
        sf = get_session_factory(db_url)
        service = SupportTicketService(session_factory=sf)

        req = CreateTicketRequest(tenant_id="t1", subject="should fail")
        with pytest.raises(Exception):
            await service.create_ticket(req, "user1")

    @pytest.mark.asyncio
    async def test_15_status_transition_race(self, service) -> None:
        """Race: stale expected status → refusal (0 rows), status unchanged; success → exactly one."""
        from sqlalchemy import update as sa_update

        from support_chat.db_models import SupportTicketModel

        req = CreateTicketRequest(tenant_id="t1", subject="Race")
        ticket = await service.create_ticket(req, "user1")
        tid = str(ticket.ticket_id)

        # 1) successful transition → exactly one winner
        updated = await service.transition_status(tid, TicketStatus.IN_PROGRESS)
        assert updated.status == TicketStatus.IN_PROGRESS

        # 2) concurrent winner moves in_progress → waiting_customer
        with service._session_factory() as session:
            session.execute(
                sa_update(SupportTicketModel)
                .where(
                    SupportTicketModel.ticket_id == ticket.ticket_id,
                    SupportTicketModel.status == "in_progress",
                )
                .values(status="waiting_customer")
            )
            session.commit()

        # 3) stale reader believes in_progress → optimistic update matches 0 rows
        with service._session_factory() as session:
            result = session.execute(
                sa_update(SupportTicketModel)
                .where(
                    SupportTicketModel.ticket_id == ticket.ticket_id,
                    SupportTicketModel.status == "in_progress",
                )
                .values(status="resolved")
            )
            session.commit()
            assert result.rowcount == 0

        assert service.get_ticket(tid).status == TicketStatus.WAITING_CUSTOMER

    @pytest.mark.asyncio
    async def test_16_utc_roundtrip(self, service) -> None:
        """UTC round-trip: written timestamps read back as tz-aware UTC."""
        from datetime import timedelta

        req = CreateTicketRequest(tenant_id="t1", subject="UTC")
        ticket = await service.create_ticket(req, "user1")
        got = service.get_ticket(str(ticket.ticket_id), tenant_id="t1")
        assert got is not None
        assert got.created_at.tzinfo is not None
        assert got.created_at.utcoffset() == timedelta(0)
        assert got.updated_at.tzinfo is not None
        assert got.updated_at.utcoffset() == timedelta(0)
