"""DecisionOS Support Chat — 12 smoke tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
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
from support_chat.service import (
    SupportTicketService,
    TicketNotFoundError,
    TicketStatusConflictError,
)
from support_chat.chat_service import ChatService
from support_chat.settings import SupportSettings
from support_chat.db import SupportDatabaseUnavailable, get_session_factory


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

        t1_tickets = await service.list_tickets("t1")
        t2_tickets = await service.list_tickets("t2")
        assert t1_tickets.total == 1
        assert t2_tickets.total == 1
        t1_ticket_id = t1_tickets.tickets[0]["ticket_id"]
        assert await service.get_ticket(t1_ticket_id, tenant_id="t2") is None


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
        ticket = await service2.get_ticket(str(tid), tenant_id="t1")

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

        assert (await service.get_ticket(tid)).status == TicketStatus.WAITING_CUSTOMER

    @pytest.mark.asyncio
    async def test_16_utc_roundtrip(self, service) -> None:
        """UTC round-trip: written timestamps read back as tz-aware UTC."""
        from datetime import timedelta

        req = CreateTicketRequest(tenant_id="t1", subject="UTC")
        ticket = await service.create_ticket(req, "user1")
        got = await service.get_ticket(str(ticket.ticket_id), tenant_id="t1")
        assert got is not None
        assert got.created_at.tzinfo is not None
        assert got.created_at.utcoffset() == timedelta(0)
        assert got.updated_at.tzinfo is not None
        assert got.updated_at.utcoffset() == timedelta(0)


class _RecordingFactory:
    """Session-factory wrapper recording worker thread ids, opens and closes."""

    def __init__(self, factory, on_call=None) -> None:
        self._factory = factory
        self._on_call = on_call
        self.thread_ids: list[int] = []
        self.opens = 0
        self.closes = 0

    def __call__(self):
        self.thread_ids.append(threading.get_ident())
        self.opens += 1
        if self._on_call is not None:
            self._on_call()
        session = self._factory()
        real_close = session.close

        def _close() -> None:
            self.closes += 1
            real_close()

        session.close = _close
        return session


class TestRuntimeBlockers:
    """BLOCKER-1 (sync I/O on the loop) and BLOCKER-2 (silent SQLite fallback)."""

    @pytest.mark.asyncio
    async def test_17_db_io_runs_off_the_event_loop_thread(self, chat_service) -> None:
        """Sync SQLAlchemy work must happen in a worker, never on the loop thread."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        await service.create_ticket(
            CreateTicketRequest(tenant_id="t1", subject="offload"), "user1"
        )

        assert sf.thread_ids, "session factory was never used"
        assert all(tid != loop_thread for tid in sf.thread_ids), sf.thread_ids

    @pytest.mark.asyncio
    async def test_18_loop_stays_responsive_while_db_call_is_parked(
        self, chat_service
    ) -> None:
        """Deterministic responsiveness proof (no wall-clock-only assertion)."""
        entered = threading.Event()
        release = threading.Event()

        def _park() -> None:
            entered.set()
            release.wait(5)

        sf = _RecordingFactory(
            get_session_factory("sqlite:///:memory:"), on_call=_park
        )
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)
        req = CreateTicketRequest(tenant_id="t1", subject="parked")

        async def other_coroutine() -> str:
            return "loop-alive"

        task = asyncio.create_task(service.create_ticket(req, "user1"))
        try:
            for _ in range(10000):
                if entered.is_set():
                    break
                await asyncio.sleep(0)
            assert entered.is_set(), "worker never reached the DB helper"

            # Loop is free: another coroutine completes while the DB call is parked.
            assert await other_coroutine() == "loop-alive"
            assert not task.done()
        finally:
            release.set()

        created = await task
        assert created.ticket_id is not None

    @pytest.mark.asyncio
    async def test_19_every_async_mutation_is_offloaded(self, chat_service) -> None:
        """create / add_message / assign_agent / transition_status all offload."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        ticket = await service.create_ticket(
            CreateTicketRequest(tenant_id="t1", subject="mutations"), "user1"
        )
        tid = str(ticket.ticket_id)

        sf.thread_ids.clear()
        await service.create_ticket(
            CreateTicketRequest(tenant_id="t1", subject="second"), "user1"
        )
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "create"

        sf.thread_ids.clear()
        await service.add_message(
            CreateMessageRequest(
                ticket_id=tid,
                body="hello",
                sender_id="user1",
                sender_role=ParticipantRole.TENANT_USER,
            )
        )
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "add_message"

        sf.thread_ids.clear()
        await service.assign_agent(tid, AssignTicketRequest(agent_id="agent1"))
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "assign_agent"

        sf.thread_ids.clear()
        await service.transition_status(tid, TicketStatus.WAITING_CUSTOMER)
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "transition"

    @pytest.mark.asyncio
    async def test_20_session_is_opened_and_closed_inside_worker(self, chat_service) -> None:
        """Every Session is created and closed in the worker thread."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        await service.create_ticket(
            CreateTicketRequest(tenant_id="t1", subject="ownership"), "user1"
        )

        assert sf.opens == sf.closes >= 1
        assert all(tid != loop_thread for tid in sf.thread_ids), sf.thread_ids

    @pytest.mark.asyncio
    async def test_21_worker_error_propagates_fail_closed(self, service) -> None:
        """A failure raised inside the worker must reach the caller."""
        with pytest.raises(ValueError, match="not found"):
            await service.add_message(
                CreateMessageRequest(
                    ticket_id=str(uuid4()),
                    body="orphan",
                    sender_id="user1",
                    sender_role=ParticipantRole.TENANT_USER,
                )
            )

    @pytest.mark.asyncio
    async def test_22_production_without_pg_or_injection_fails_closed(
        self, chat_service, monkeypatch
    ) -> None:
        """Production + no PG_DSN + no injection → explicit refusal, no DB file."""
        monkeypatch.delenv("PG_DSN", raising=False)
        monkeypatch.delenv("ROMA_ENV", raising=False)
        monkeypatch.setenv("ENV", "production")

        db_file = Path("support_chat.db")
        existed_before = db_file.exists()

        service = SupportTicketService(chat_service=chat_service)
        assert service._session_factory is None, "constructor must stay lazy"

        with pytest.raises(SupportDatabaseUnavailable) as excinfo:
            await service.create_ticket(
                CreateTicketRequest(tenant_id="t1", subject="nope"), "user1"
            )

        message = str(excinfo.value)
        assert "PG_DSN" in message
        assert "postgresql" not in message.lower()
        assert "sqlite" not in message.lower()
        assert db_file.exists() is existed_before

    @pytest.mark.asyncio
    async def test_23_explicit_sqlite_injection_allowed_under_production(
        self, chat_service, tmp_path, monkeypatch
    ) -> None:
        """Explicit test/local injection stays allowed even with the prod marker."""
        monkeypatch.delenv("PG_DSN", raising=False)
        monkeypatch.setenv("ENV", "production")

        sf = get_session_factory(f"sqlite:///{tmp_path}/explicit.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        created = await service.create_ticket(
            CreateTicketRequest(tenant_id="t1", subject="explicit"), "user1"
        )
        assert await service.get_ticket(str(created.ticket_id), tenant_id="t1") is not None

    def test_24_construction_without_pg_never_touches_the_db(
        self, monkeypatch
    ) -> None:
        """No DB resolution, no connection and no file until the first DB call."""
        monkeypatch.delenv("PG_DSN", raising=False)
        monkeypatch.setenv("ENV", "production")

        db_file = Path("support_chat.db")
        existed_before = db_file.exists()

        service = SupportTicketService()

        assert service._session_factory is None
        assert db_file.exists() is existed_before

    @pytest.mark.asyncio
    async def test_25_concurrent_schema_init_runs_once(
        self, chat_service, tmp_path, monkeypatch
    ) -> None:
        """Lock + double check: concurrent workers initialise SQLite DDL once."""
        from support_chat.db_models import Base

        calls: list[int] = []
        real_create_all = Base.metadata.create_all

        def _counting_create_all(bind, *args, **kwargs):
            calls.append(1)
            return real_create_all(bind, *args, **kwargs)

        monkeypatch.setattr(Base.metadata, "create_all", _counting_create_all)

        sf = get_session_factory(f"sqlite:///{tmp_path}/lock.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        await asyncio.gather(
            asyncio.to_thread(service._ensure_tables_sync),
            asyncio.to_thread(service._ensure_tables_sync),
        )

        assert len(calls) == 1


class TestAssignStatusRace:
    """E-15: assignment must not overwrite terminal or concurrently changed status."""

    @pytest.mark.asyncio
    async def test_26_assign_refused_for_terminal_status_without_side_effects(
        self, service, chat_service
    ) -> None:
        """RESOLVED and CLOSED both refuse assignment and leave the row untouched."""
        for terminal in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            req = CreateTicketRequest(tenant_id="t1", subject=f"Terminal {terminal.value}")
            ticket = await service.create_ticket(req, "user1")
            tid = str(ticket.ticket_id)
            await service.transition_status(tid, TicketStatus.IN_PROGRESS)
            await service.transition_status(tid, terminal)

            participants_before = len(service._participants.get(tid, []))
            messages_before = len(chat_service.get_messages(tid))

            with pytest.raises(TicketStatusConflictError):
                await service.assign_agent(tid, AssignTicketRequest(agent_id="a1"))

            stored = await service.get_ticket(tid)
            assert stored is not None
            assert stored.status == terminal
            assert stored.assigned_agent_id is None
            # create_ticket already registered the tenant participant; the failed
            # assignment must not add a support-agent participant or a system note.
            assert len(service._participants.get(tid, [])) == participants_before
            assert all(
                participant.role != ParticipantRole.SUPPORT_AGENT
                for participant in service._participants.get(tid, [])
            )
            assert len(chat_service.get_messages(tid)) == messages_before

    @pytest.mark.asyncio
    async def test_27_assign_reassigns_within_in_progress(self, service) -> None:
        """IN_PROGRESS -> IN_PROGRESS reassignment stays allowed and keeps the status."""
        req = CreateTicketRequest(tenant_id="t1", subject="Reassign")
        ticket = await service.create_ticket(req, "user1")
        tid = str(ticket.ticket_id)
        await service.transition_status(tid, TicketStatus.IN_PROGRESS)

        first = await service.assign_agent(tid, AssignTicketRequest(agent_id="a1"))
        assert first.assigned_agent_id == "a1"
        assert first.status == TicketStatus.IN_PROGRESS

        second = await service.assign_agent(tid, AssignTicketRequest(agent_id="a2"))
        assert second.assigned_agent_id == "a2"
        assert second.status == TicketStatus.IN_PROGRESS

        stored = await service.get_ticket(tid)
        assert stored is not None
        assert stored.assigned_agent_id == "a2"
        assert stored.status == TicketStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_28_stale_read_assignment_loses_to_concurrent_transition(
        self, chat_service, tmp_path
    ) -> None:
        """CAS: a status committed after the read makes the assignment UPDATE match 0 rows."""
        from sqlalchemy import update as sa_update

        from support_chat.db_models import SupportTicketModel

        factory = get_session_factory(f"sqlite:///{tmp_path}/assign-race.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)

        req = CreateTicketRequest(tenant_id="t1", subject="Assign race")
        ticket = await service.create_ticket(req, "user1")
        tid = str(ticket.ticket_id)
        await service.transition_status(tid, TicketStatus.IN_PROGRESS)
        await service.assign_agent(tid, AssignTicketRequest(agent_id="a1"))

        read_seen = threading.Event()
        winner_done = threading.Event()
        observed: list[str] = []

        class _StaleReadFactory:
            """Snapshot the row in a short-lived Session, then pause before the UPDATE."""

            def __call__(self):
                session = factory()

                def _stale_get(entity, ident, **kwargs):
                    with factory() as snapshot:
                        model = snapshot.get(entity, ident)
                        observed.append(model.status)
                        snapshot.expunge(model)
                    read_seen.set()
                    assert winner_done.wait(timeout=10), "winner transition never committed"
                    return model

                session.get = _stale_get
                return session

        service._session_factory = _StaleReadFactory()

        def _winner_transition() -> None:
            with factory() as session:
                session.execute(
                    sa_update(SupportTicketModel)
                    .where(SupportTicketModel.ticket_id == ticket.ticket_id)
                    .values(status=TicketStatus.CLOSED.value)
                )
                session.commit()

        task = asyncio.create_task(
            service.assign_agent(tid, AssignTicketRequest(agent_id="a2"))
        )
        try:
            assert await asyncio.to_thread(read_seen.wait, 10), "assignment never read the row"
            await asyncio.to_thread(_winner_transition)
            winner_done.set()
            with pytest.raises(TicketStatusConflictError):
                await task
        finally:
            winner_done.set()
            if not task.done():
                task.cancel()

        assert observed == [TicketStatus.IN_PROGRESS.value]
        with factory() as session:
            final = session.get(SupportTicketModel, ticket.ticket_id)
            assert final.status == TicketStatus.CLOSED.value
            assert final.assigned_agent_id == "a1"

    @pytest.mark.asyncio
    async def test_29_missing_ticket_raises_not_found(self, service) -> None:
        """A missing ticket is a typed not-found, distinct from a status conflict."""
        missing = str(uuid4())
        with pytest.raises(TicketNotFoundError) as excinfo:
            await service.assign_agent(missing, AssignTicketRequest(agent_id="a1"))
        assert not isinstance(excinfo.value, TicketStatusConflictError)
        assert missing in str(excinfo.value)

    def test_30_assign_http_contract_404_409_200(
        self, chat_service, monkeypatch, tmp_path
    ) -> None:
        """HTTP mapping: missing ticket -> 404, terminal status -> 409, normal -> 200."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import support_chat.router as support_router

        factory = get_session_factory(f"sqlite:///{tmp_path}/assign-http.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)
        monkeypatch.setattr(support_router, "_service", service)

        app = FastAPI()
        app.include_router(support_router.router)
        client = TestClient(app)

        missing = client.post(
            f"/v1/support/tickets/{uuid4()}/assign", json={"agent_id": "a1"}
        )
        assert missing.status_code == 404

        created = client.post(
            "/v1/support/tickets", json={"tenant_id": "t1", "subject": "HTTP"}
        )
        assert created.status_code == 201
        tid = created.json()["ticket_id"]

        happy = client.post(f"/v1/support/tickets/{tid}/assign", json={"agent_id": "a1"})
        assert happy.status_code == 200
        assert happy.json()["assigned_agent_id"] == "a1"

        closed = client.post(f"/v1/support/tickets/{tid}/transition?status=closed")
        assert closed.status_code == 200

        conflict = client.post(
            f"/v1/support/tickets/{tid}/assign", json={"agent_id": "a2"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]
