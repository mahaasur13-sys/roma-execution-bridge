"""DecisionOS Support Chat — smoke + runtime-blocker + E-15 race + H1 tenant/auth.

H1 hardening contract asserted here:

  * every support HTTP route depends on the canonical ``deps.verify_api_key``;
  * the tenant is derived from the verified principal only — a body ``tenant_id``
    is a 422 and ``x-tenant-id``/``x-user-id``/``x-user-role`` change nothing;
  * every read and mutation is constrained by ``ticket_id AND tenant_id``;
    a foreign ticket is indistinguishable from a missing one;
  * author/role of a message are server-derived (``tenant:{tenant_id}`` /
    ``tenant_user``); ``sender_id``/``sender_role``/``is_internal`` are rejected;
  * assign/transition are fail-closed 403 until H2 supplies a trusted role,
    while the E-15 CAS contract stays intact at the service layer;
  * the WebSocket is refused before ``accept()`` (close 4401).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

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
    TenantContext,
    TicketNotFoundError,
    TicketStatusConflictError,
)
from support_chat.chat_service import ChatService
from support_chat.settings import SupportSettings
from support_chat.db import SupportDatabaseUnavailable, get_session_factory

# Test-only fake credential value: proves the raw key never leaves the boundary.
RAW_KEY = "tenant-a-key"


def _tenant(tenant_id: str) -> TenantContext:
    """Tenant context exactly as the HTTP boundary derives it from a principal."""
    return TenantContext.from_principal(
        {"tenant_id": tenant_id, "name": tenant_id, "plan": "free"}
    )


class _ForbiddenService:
    """Any use of the service from a route fails the test loudly."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"service.{name} must not be reached")

        return _boom


def _http_client(monkeypatch, service, principal: dict | None = None) -> TestClient:
    """Narrow app: support router + optional canonical-principal override."""
    import support_chat.router as support_router

    monkeypatch.setattr(support_router, "_service", service)
    app = FastAPI()
    app.include_router(support_router.router)
    if principal is not None:
        app.dependency_overrides[support_router.verify_api_key] = lambda: principal
    return TestClient(app)


def _principal(tenant_id: str = "t1") -> dict:
    """A verified principal as ``deps.verify_api_key`` returns it (key included)."""
    return {"tenant_id": tenant_id, "name": tenant_id, "plan": "pro", "api_key": RAW_KEY}


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


@pytest.fixture
def tenant_a() -> TenantContext:
    return _tenant("t1")


@pytest.fixture
def tenant_b() -> TenantContext:
    return _tenant("t2")


class TestTicketCreation:

    @pytest.mark.asyncio
    async def test_01_create_basic_ticket(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 1: Create basic support ticket."""
        req = CreateTicketRequest(
            subject="Payment issue",
            body="My USDT payment didn't arrive",
            priority=TicketPriority.HIGH,
        )
        result = await service.create_ticket(req, tenant_a)
        assert result.ticket_id is not None
        assert result.status == TicketStatus.OPEN
        assert result.subject == "Payment issue"
        assert result.tenant_id == "t1"

    @pytest.mark.asyncio
    async def test_02_create_ticket_with_crypto_context(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 2: Create ticket linked to crypto invoice."""
        invoice_id = uuid4()
        req = CreateTicketRequest(
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
        result = await service.create_ticket(req, tenant_a)
        assert result.context_type == "crypto_invoice"
        assert result.context_id == invoice_id

    @pytest.mark.asyncio
    async def test_03_create_ticket_with_monero_context(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 3: Create ticket linked to Monero wallet."""
        wallet_id = uuid4()
        req = CreateTicketRequest(
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
        result = await service.create_ticket(req, tenant_a)
        assert result.context_type == "crypto_wallet"

    @pytest.mark.asyncio
    async def test_04_ticket_tenant_isolation(
        self,
        service: SupportTicketService,
        tenant_a: TenantContext,
        tenant_b: TenantContext,
    ) -> None:
        """Smoke 4: Tenant isolation — tenant A can't see tenant B tickets."""
        req1 = CreateTicketRequest(subject="T1 ticket")
        req2 = CreateTicketRequest(subject="T2 ticket")
        await service.create_ticket(req1, tenant_a)
        await service.create_ticket(req2, tenant_b)

        t1_tickets = await service.list_tickets(tenant_a)
        t2_tickets = await service.list_tickets(tenant_b)
        assert t1_tickets.total == 1
        assert t2_tickets.total == 1
        t1_ticket_id = t1_tickets.tickets[0]["ticket_id"]
        assert await service.get_ticket(t1_ticket_id, tenant_b) is None


class TestMessages:

    @pytest.mark.asyncio
    async def test_05_add_message(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 5: Add message to ticket."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)

        msg_req = CreateMessageRequest(body="Hello, I need help")
        msg = await service.add_message(str(ticket.ticket_id), msg_req, tenant_a)
        assert msg.ticket_id == ticket.ticket_id
        assert msg.body == "Hello, I need help"

    @pytest.mark.asyncio
    async def test_06_internal_note_hidden_from_tenant(
        self,
        service: SupportTicketService,
        chat_service: ChatService,
        tenant_a: TenantContext,
    ) -> None:
        """Smoke 6: Internal notes visible only to agents."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)

        await service.assign_agent(
            str(ticket.ticket_id), AssignTicketRequest(agent_id="agent1"), tenant_a
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
    async def test_07_valid_transition(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 7: Valid status transition open → in_progress."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)
        updated = await service.transition_status(
            str(ticket.ticket_id), TicketStatus.IN_PROGRESS, tenant_a
        )
        assert updated.status == TicketStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_08_invalid_transition_blocked(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 8: Invalid transition open → resolved blocked."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)
        with pytest.raises(ValueError, match="Cannot transition from open"):
            await service.transition_status(
                str(ticket.ticket_id), TicketStatus.RESOLVED, tenant_a
            )


class TestAgentAssignment:

    @pytest.mark.asyncio
    async def test_09_assign_agent(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 9: Assign agent to ticket."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)
        updated = await service.assign_agent(
            str(ticket.ticket_id), AssignTicketRequest(agent_id="agent42"), tenant_a
        )
        assert updated.assigned_agent_id == "agent42"
        assert updated.status == TicketStatus.IN_PROGRESS


class TestCSAT:

    @pytest.mark.asyncio
    async def test_10_submit_csat(
        self, service: SupportTicketService, tenant_a: TenantContext
    ) -> None:
        """Smoke 10: Submit CSAT rating after resolution."""
        req = CreateTicketRequest(subject="Test")
        ticket = await service.create_ticket(req, tenant_a)
        await service.transition_status(
            str(ticket.ticket_id), TicketStatus.IN_PROGRESS, tenant_a
        )
        await service.transition_status(
            str(ticket.ticket_id), TicketStatus.RESOLVED, tenant_a
        )

        csat = await service.submit_csat(
            str(ticket.ticket_id), CsatSubmitRequest(score=5, comment="Great!"), tenant_a
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
    async def test_13_restart_persistence(self, tmp_path, tenant_a) -> None:
        """P-1 БЛОКЕР-4: ticket survives a service restart (new engine, same DB)."""
        db_url = f"sqlite:///{tmp_path}/support.db"
        sf1 = get_session_factory(db_url)
        service1 = SupportTicketService(session_factory=sf1)

        req = CreateTicketRequest(
            subject="Persists across restart",
            body="created before restart",
            priority=TicketPriority.HIGH,
        )
        result = await service1.create_ticket(req, tenant_a)
        tid = result.ticket_id

        # "restart": brand-new engine/session factory over the same DB file.
        sf2 = get_session_factory(db_url)
        service2 = SupportTicketService(session_factory=sf2)
        ticket = await service2.get_ticket(str(tid), tenant_a)

        assert ticket is not None
        assert ticket.ticket_id == tid
        assert ticket.subject == "Persists across restart"
        assert ticket.status == TicketStatus.OPEN
        assert ticket.priority == TicketPriority.HIGH

    @pytest.mark.asyncio
    async def test_14_fail_closed_db_unavailable(self, tmp_path, tenant_a) -> None:
        """Fail-closed: unreachable DB raises — no silent in-memory fallback."""
        db_url = f"sqlite:///{tmp_path}/no_such_dir/support.db"
        sf = get_session_factory(db_url)
        service = SupportTicketService(session_factory=sf)

        req = CreateTicketRequest(subject="should fail")
        with pytest.raises(Exception):
            await service.create_ticket(req, tenant_a)

    @pytest.mark.asyncio
    async def test_15_status_transition_race(self, service, tenant_a) -> None:
        """Race: stale expected status → refusal (0 rows), status unchanged; success → exactly one."""
        from sqlalchemy import update as sa_update

        from support_chat.db_models import SupportTicketModel

        req = CreateTicketRequest(subject="Race")
        ticket = await service.create_ticket(req, tenant_a)
        tid = str(ticket.ticket_id)

        # 1) successful transition → exactly one winner
        updated = await service.transition_status(
            tid, TicketStatus.IN_PROGRESS, tenant_a
        )
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

        assert (await service.get_ticket(tid, tenant_a)).status == (
            TicketStatus.WAITING_CUSTOMER
        )

    @pytest.mark.asyncio
    async def test_16_utc_roundtrip(self, service, tenant_a) -> None:
        """UTC round-trip: written timestamps read back as tz-aware UTC."""
        from datetime import timedelta

        req = CreateTicketRequest(subject="UTC")
        ticket = await service.create_ticket(req, tenant_a)
        got = await service.get_ticket(str(ticket.ticket_id), tenant_a)
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
    async def test_17_db_io_runs_off_the_event_loop_thread(
        self, chat_service, tenant_a
    ) -> None:
        """Sync SQLAlchemy work must happen in a worker, never on the loop thread."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        await service.create_ticket(CreateTicketRequest(subject="offload"), tenant_a)

        assert sf.thread_ids, "session factory was never used"
        assert all(tid != loop_thread for tid in sf.thread_ids), sf.thread_ids

    @pytest.mark.asyncio
    async def test_18_loop_stays_responsive_while_db_call_is_parked(
        self, chat_service, tenant_a
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
        req = CreateTicketRequest(subject="parked")

        async def other_coroutine() -> str:
            return "loop-alive"

        task = asyncio.create_task(service.create_ticket(req, tenant_a))
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
    async def test_19_every_async_mutation_is_offloaded(
        self, chat_service, tenant_a
    ) -> None:
        """create / add_message / assign_agent / transition_status all offload."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        ticket = await service.create_ticket(
            CreateTicketRequest(subject="mutations"), tenant_a
        )
        tid = str(ticket.ticket_id)

        sf.thread_ids.clear()
        await service.create_ticket(CreateTicketRequest(subject="second"), tenant_a)
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "create"

        sf.thread_ids.clear()
        await service.add_message(tid, CreateMessageRequest(body="hello"), tenant_a)
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "add_message"

        sf.thread_ids.clear()
        await service.assign_agent(tid, AssignTicketRequest(agent_id="agent1"), tenant_a)
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "assign_agent"

        sf.thread_ids.clear()
        await service.transition_status(tid, TicketStatus.WAITING_CUSTOMER, tenant_a)
        assert sf.thread_ids and all(t != loop_thread for t in sf.thread_ids), "transition"

    @pytest.mark.asyncio
    async def test_20_session_is_opened_and_closed_inside_worker(
        self, chat_service, tenant_a
    ) -> None:
        """Every Session is created and closed in the worker thread."""
        loop_thread = threading.get_ident()
        sf = _RecordingFactory(get_session_factory("sqlite:///:memory:"))
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        await service.create_ticket(CreateTicketRequest(subject="ownership"), tenant_a)

        assert sf.opens == sf.closes >= 1
        assert all(tid != loop_thread for tid in sf.thread_ids), sf.thread_ids

    @pytest.mark.asyncio
    async def test_21_worker_error_propagates_fail_closed(self, service, tenant_a) -> None:
        """A failure raised inside the worker must reach the caller."""
        orphan = str(uuid4())
        with pytest.raises(ValueError, match="not found"):
            await service.add_message(
                orphan, CreateMessageRequest(body="orphan"), tenant_a
            )

    @pytest.mark.asyncio
    async def test_22_production_without_pg_or_injection_fails_closed(
        self, chat_service, monkeypatch, tenant_a
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
            await service.create_ticket(CreateTicketRequest(subject="nope"), tenant_a)

        message = str(excinfo.value)
        assert "PG_DSN" in message
        assert "postgresql" not in message.lower()
        assert "sqlite" not in message.lower()
        assert db_file.exists() is existed_before

    @pytest.mark.asyncio
    async def test_23_explicit_sqlite_injection_allowed_under_production(
        self, chat_service, tmp_path, monkeypatch, tenant_a
    ) -> None:
        """Explicit test/local injection stays allowed even with the prod marker."""
        monkeypatch.delenv("PG_DSN", raising=False)
        monkeypatch.setenv("ENV", "production")

        sf = get_session_factory(f"sqlite:///{tmp_path}/explicit.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=sf)

        created = await service.create_ticket(
            CreateTicketRequest(subject="explicit"), tenant_a
        )
        assert await service.get_ticket(str(created.ticket_id), tenant_a) is not None

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
        self, service, chat_service, tenant_a
    ) -> None:
        """RESOLVED and CLOSED both refuse assignment and leave the row untouched."""
        for terminal in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            req = CreateTicketRequest(subject=f"Terminal {terminal.value}")
            ticket = await service.create_ticket(req, tenant_a)
            tid = str(ticket.ticket_id)
            await service.transition_status(tid, TicketStatus.IN_PROGRESS, tenant_a)
            await service.transition_status(tid, terminal, tenant_a)

            participants_before = len(service._participants.get(tid, []))
            messages_before = len(chat_service.get_messages(tid))

            with pytest.raises(TicketStatusConflictError):
                await service.assign_agent(
                    tid, AssignTicketRequest(agent_id="a1"), tenant_a
                )

            stored = await service.get_ticket(tid, tenant_a)
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
    async def test_27_assign_reassigns_within_in_progress(self, service, tenant_a) -> None:
        """IN_PROGRESS -> IN_PROGRESS reassignment stays allowed and keeps the status."""
        req = CreateTicketRequest(subject="Reassign")
        ticket = await service.create_ticket(req, tenant_a)
        tid = str(ticket.ticket_id)
        await service.transition_status(tid, TicketStatus.IN_PROGRESS, tenant_a)

        first = await service.assign_agent(
            tid, AssignTicketRequest(agent_id="a1"), tenant_a
        )
        assert first.assigned_agent_id == "a1"
        assert first.status == TicketStatus.IN_PROGRESS

        second = await service.assign_agent(
            tid, AssignTicketRequest(agent_id="a2"), tenant_a
        )
        assert second.assigned_agent_id == "a2"
        assert second.status == TicketStatus.IN_PROGRESS

        stored = await service.get_ticket(tid, tenant_a)
        assert stored is not None
        assert stored.assigned_agent_id == "a2"
        assert stored.status == TicketStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_28_stale_read_assignment_loses_to_concurrent_transition(
        self, chat_service, tmp_path, tenant_a
    ) -> None:
        """CAS: a status committed after the read makes the assignment UPDATE match 0 rows."""
        from sqlalchemy import update as sa_update

        from support_chat.db_models import SupportTicketModel

        factory = get_session_factory(f"sqlite:///{tmp_path}/assign-race.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)

        req = CreateTicketRequest(subject="Assign race")
        ticket = await service.create_ticket(req, tenant_a)
        tid = str(ticket.ticket_id)
        await service.transition_status(tid, TicketStatus.IN_PROGRESS, tenant_a)
        await service.assign_agent(tid, AssignTicketRequest(agent_id="a1"), tenant_a)

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
            service.assign_agent(tid, AssignTicketRequest(agent_id="a2"), tenant_a)
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
    async def test_29_missing_ticket_raises_not_found(self, service, tenant_a) -> None:
        """A missing ticket is a typed not-found, distinct from a status conflict."""
        missing = str(uuid4())
        with pytest.raises(TicketNotFoundError) as excinfo:
            await service.assign_agent(
                missing, AssignTicketRequest(agent_id="a1"), tenant_a
            )
        assert not isinstance(excinfo.value, TicketStatusConflictError)
        assert missing in str(excinfo.value)


class TestTenantAuthBoundary:
    """H1: canonical auth, trusted tenant, server-derived actor, fail-closed roles."""

    def test_30_tenant_context_is_derived_from_the_principal_without_the_key(
        self,
    ) -> None:
        """The sanitised context keeps the tenant and drops the credential."""
        ctx = TenantContext.from_principal(
            {"tenant_id": "  t1  ", "name": "T", "plan": "pro", "api_key": RAW_KEY}
        )
        assert ctx.tenant_id == "t1"
        assert ctx.actor_id == "tenant:t1"
        assert RAW_KEY not in ctx.actor_id
        assert RAW_KEY not in repr(ctx)
        assert not hasattr(ctx, "api_key")

        with pytest.raises(ValueError):
            TenantContext.from_principal({"api_key": RAW_KEY})
        with pytest.raises(ValueError):
            TenantContext.from_principal({"tenant_id": "   "})

    def test_31_every_support_http_route_requires_canonical_api_key(self) -> None:
        """Tenant-facing routes keep the canonical key; support-auth routes do not.

        H1 invariant (unchanged for every tenant-facing route): the canonical
        ``deps.verify_api_key`` is attached. H2a adds three support-auth endpoints
        that must NOT depend on a tenant API key — they authenticate a dedicated
        support session instead. Both halves are asserted, so the rule stays
        enforceable in either direction.
        """
        from fastapi.routing import APIRoute

        import support_chat.router as support_router

        def _calls(dependant) -> list:
            found = []
            for sub in dependant.dependencies:
                found.append(sub.call)
                found.extend(_calls(sub))
            return found

        http_routes = [
            r for r in support_router.router.routes if isinstance(r, APIRoute)
        ]
        assert http_routes, "no HTTP routes found on the support router"

        auth_paths = {
            "/v1/support/auth/login",
            "/v1/support/auth/logout",
            "/v1/support/auth/session",
        }
        # The canonical check is reached either directly or through the H2a
        # wrapper that calls it for a present key, so both forms satisfy the rule.
        canonical = (
            support_router.verify_api_key,
            support_router._optional_tenant_principal,
        )
        session_only = {
            "/v1/support/auth/logout": support_router._support_session_principal,
            "/v1/support/auth/session": support_router._support_session_principal,
            "/v1/support/auth/login": support_router._require_allowed_origin,
        }
        assert set(session_only) == auth_paths, sorted(auth_paths)

        seen_auth: set[str] = set()
        for route in http_routes:
            calls = _calls(route.dependant)
            if route.path in session_only:
                seen_auth.add(route.path)
                assert support_router.verify_api_key not in calls, route.path
                assert session_only[route.path] in calls, route.path
                continue
            assert any(call in calls for call in canonical), route.path
        assert seen_auth == auth_paths, sorted(auth_paths - seen_auth)

    def test_32_missing_api_key_is_rejected_before_any_service_call(
        self, monkeypatch
    ) -> None:
        """Canonical missing-key behaviour: 401 and no service work."""
        client = _http_client(monkeypatch, _ForbiddenService())
        resp = client.get("/v1/support/tickets")
        assert resp.status_code == 401
        assert "X-API-Key" in resp.json()["detail"]

    def test_33_invalid_api_key_is_rejected_before_any_service_call(
        self, monkeypatch
    ) -> None:
        """Canonical invalid-key behaviour: 401 and no service work."""
        import db_adapter

        monkeypatch.setattr(db_adapter, "find_tenant_by_key", lambda key: None)
        client = _http_client(monkeypatch, _ForbiddenService())
        resp = client.get(
            "/v1/support/tickets", headers={"X-API-Key": "not-a-real-key"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid API key"

    def test_34_create_uses_principal_tenant_and_rejects_caller_tenant(
        self, monkeypatch, chat_service, tmp_path
    ) -> None:
        """Body tenant_id → 422; header tenant/user/role → no effect at all."""
        factory = get_session_factory(f"sqlite:///{tmp_path}/h1-create.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)
        client = _http_client(monkeypatch, service, principal=_principal("t1"))

        spoof = client.post(
            "/v1/support/tickets", json={"tenant_id": "t2", "subject": "spoof"}
        )
        assert spoof.status_code == 422

        created = client.post(
            "/v1/support/tickets",
            json={"subject": "honest"},
            headers={
                "x-tenant-id": "t2",
                "x-user-id": "admin",
                "x-user-role": "super_admin",
            },
        )
        assert created.status_code == 201
        assert created.json()["tenant_id"] == "t1"
        assert RAW_KEY not in created.text

        tid = created.json()["ticket_id"]
        fetched = client.get(
            f"/v1/support/tickets/{tid}", headers={"x-user-role": "super_admin"}
        )
        assert fetched.status_code == 200
        assert fetched.json()["ticket"]["tenant_id"] == "t1"
        assert all(not m["is_internal"] for m in fetched.json()["messages"])
        assert RAW_KEY not in fetched.text

    @pytest.mark.asyncio
    async def test_35_cross_tenant_ticket_is_not_found_over_http(
        self, monkeypatch, chat_service, tmp_path, tenant_b
    ) -> None:
        """A tenant-B ticket answers 404 for tenant A and never appears in its list."""
        factory = get_session_factory(f"sqlite:///{tmp_path}/h1-cross.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)
        foreign = await service.create_ticket(CreateTicketRequest(subject="B"), tenant_b)

        client = _http_client(monkeypatch, service, principal=_principal("t1"))

        detail = client.get(f"/v1/support/tickets/{foreign.ticket_id}")
        assert detail.status_code == 404

        listing = client.get("/v1/support/tickets")
        assert listing.status_code == 200
        assert listing.json()["total"] == 0

        message = client.post(
            f"/v1/support/tickets/{foreign.ticket_id}/messages", json={"body": "cross"}
        )
        assert message.status_code == 404

        csat = client.post(
            f"/v1/support/tickets/{foreign.ticket_id}/csat", json={"score": 5}
        )
        assert csat.status_code == 404

    @pytest.mark.asyncio
    async def test_36_privileged_routes_fail_closed_before_lookup(
        self, monkeypatch, chat_service, tmp_path, tenant_a
    ) -> None:
        """assign/transition are 403 for a tenant key, before any lookup or write."""
        factory = get_session_factory(f"sqlite:///{tmp_path}/h1-priv.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)
        ticket = await service.create_ticket(CreateTicketRequest(subject="priv"), tenant_a)
        tid = str(ticket.ticket_id)
        messages_before = len(chat_service.get_messages(tid))

        # A service that would explode if a privileged route reached it.
        client = _http_client(monkeypatch, _ForbiddenService(), principal=_principal("t1"))
        tenant_key = {"X-API-Key": RAW_KEY}

        # H2a reads the tenant key through its own dependency, so the canonical
        # check is stubbed here exactly as ``verify_api_key`` would answer: a
        # present key resolves to a tenant principal, an absent one is ``None``.
        import support_chat.router as support_router

        def _tenant_key_or_none(request: Request):
            return _principal("t1") if request.headers.get("x-api-key") else None

        client.app.dependency_overrides[
            support_router._optional_tenant_principal
        ] = _tenant_key_or_none

        # No credential at all: still a 401 before any service call.
        assert (
            client.post(f"/v1/support/tickets/{tid}/assign", json={}).status_code
            == 401
        )

        assign = client.post(
            f"/v1/support/tickets/{tid}/assign", json={}, headers=tenant_key
        )
        assert assign.status_code == 403

        transition = client.post(
            f"/v1/support/tickets/{tid}/transition?status=closed", headers=tenant_key
        )
        assert transition.status_code == 403

        # An unknown ticket answers identically: no existence/status disclosure.
        unknown = client.post(
            f"/v1/support/tickets/{uuid4()}/assign", json={}, headers=tenant_key
        )
        assert unknown.status_code == 403

        stored = await service.get_ticket(tid, tenant_a)
        assert stored is not None
        assert stored.status == TicketStatus.OPEN
        assert stored.assigned_agent_id is None
        assert len(chat_service.get_messages(tid)) == messages_before

    @pytest.mark.asyncio
    async def test_37_message_author_is_server_derived_and_spoofing_is_rejected(
        self, monkeypatch, chat_service, tmp_path, tenant_a
    ) -> None:
        """Author/role come from the principal; sender/role/visibility fields are 422."""
        factory = get_session_factory(f"sqlite:///{tmp_path}/h1-msg.db")
        service = SupportTicketService(chat_service=chat_service, session_factory=factory)
        ticket = await service.create_ticket(
            CreateTicketRequest(subject="msg", body="first"), tenant_a
        )
        tid = str(ticket.ticket_id)

        client = _http_client(monkeypatch, service, principal=_principal("t1"))

        spoof_author = client.post(
            f"/v1/support/tickets/{tid}/messages",
            json={"body": "x", "sender_id": "admin", "sender_role": "super_admin"},
        )
        assert spoof_author.status_code == 422

        spoof_visibility = client.post(
            f"/v1/support/tickets/{tid}/messages",
            json={"body": "x", "is_internal": True},
        )
        assert spoof_visibility.status_code == 422

        ok = client.post(
            f"/v1/support/tickets/{tid}/messages",
            json={"body": "hello"},
            headers={"x-user-id": "admin", "x-user-role": "super_admin"},
        )
        assert ok.status_code == 200
        payload = ok.json()
        assert payload["sender_id"] == "tenant:t1"
        assert payload["sender_role"] == ParticipantRole.TENANT_USER.value
        assert payload["is_internal"] is False
        assert payload["message_type"] == "text"
        assert RAW_KEY not in ok.text

        stored = chat_service.get_messages(tid)[-1]
        assert stored.sender_id == "tenant:t1"
        assert stored.sender_role == ParticipantRole.TENANT_USER

    def test_38_websocket_is_refused_before_accept(self, monkeypatch) -> None:
        """The WS endpoint closes before accept: no lookup, no message, no role."""
        import support_chat.router as support_router

        monkeypatch.setattr(support_router, "_service", _ForbiddenService())
        monkeypatch.setattr(support_router, "_chat_svc", _ForbiddenService())

        app = FastAPI()
        app.include_router(support_router.router)
        client = TestClient(app)

        tid = str(uuid4())
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/v1/support/ws/{tid}"):
                pass
        assert excinfo.value.code == 4401

    @pytest.mark.asyncio
    async def test_39_tenant_scope_enforced_on_every_service_read_and_mutation(
        self, service, chat_service, tenant_a, tenant_b
    ) -> None:
        """Every read/mutation is constrained by ticket_id AND tenant_id."""
        b_ticket = await service.create_ticket(
            CreateTicketRequest(subject="B ticket", body="b"), tenant_b
        )
        b_tid = str(b_ticket.ticket_id)
        a_ticket = await service.create_ticket(
            CreateTicketRequest(subject="A ticket"), tenant_a
        )
        a_tid = str(a_ticket.ticket_id)

        # reads
        assert await service.get_ticket(b_tid, tenant_a) is None
        assert await service.get_ticket_detail(b_tid, tenant_a) is None
        listing = await service.list_tickets(tenant_a)
        assert listing.total == 1
        assert [t["ticket_id"] for t in listing.tickets] == [a_tid]

        # message: foreign ticket is not found and nothing is stored
        messages_before = len(chat_service.get_messages(b_tid))
        with pytest.raises(ValueError, match="not found"):
            await service.add_message(
                b_tid, CreateMessageRequest(body="cross"), tenant_a
            )
        assert len(chat_service.get_messages(b_tid)) == messages_before

        # assign: typed not-found, row untouched
        with pytest.raises(TicketNotFoundError):
            await service.assign_agent(
                b_tid, AssignTicketRequest(agent_id="a1"), tenant_a
            )
        foreign = await service.get_ticket(b_tid, tenant_b)
        assert foreign is not None
        assert foreign.status == TicketStatus.OPEN
        assert foreign.assigned_agent_id is None

        # transition: not found, status untouched
        with pytest.raises(ValueError, match="not found"):
            await service.transition_status(b_tid, TicketStatus.IN_PROGRESS, tenant_a)
        assert (await service.get_ticket(b_tid, tenant_b)).status == TicketStatus.OPEN

        # CSAT: foreign ticket is not found and nothing is recorded
        with pytest.raises(ValueError, match="not found"):
            await service.submit_csat(b_tid, CsatSubmitRequest(score=5), tenant_a)
        assert service._csat.get(b_tid) is None

        # same-tenant CSAT still succeeds and is attributed to the tenant actor
        await service.transition_status(a_tid, TicketStatus.IN_PROGRESS, tenant_a)
        await service.transition_status(a_tid, TicketStatus.RESOLVED, tenant_a)
        rating = await service.submit_csat(
            a_tid, CsatSubmitRequest(score=4, comment="ok"), tenant_a
        )
        assert rating.tenant_id == "t1"
        assert rating.rated_by == "tenant:t1"

    @pytest.mark.asyncio
    async def test_40_internal_notes_are_never_visible_to_a_tenant_key(
        self, service, chat_service, tenant_a
    ) -> None:
        """A tenant key sees only the customer-visible thread and cannot forge notes."""
        ticket = await service.create_ticket(
            CreateTicketRequest(subject="notes", body="hello"), tenant_a
        )
        tid = str(ticket.ticket_id)
        await service.assign_agent(tid, AssignTicketRequest(agent_id="agent1"), tenant_a)
        await chat_service.add_internal_note(ticket.ticket_id, "agent1", "internal only")

        detail = await service.get_ticket_detail(tid, tenant_a)
        assert detail is not None
        assert detail.messages, "the customer-visible thread must still be returned"
        assert all(not m["is_internal"] for m in detail.messages)
        assert all(m["message_type"] != "internal_note" for m in detail.messages)
        assert all(m["body"] != "internal only" for m in detail.messages)

        # The caller contract no longer has a visibility switch at all.
        with pytest.raises(ValidationError):
            CreateMessageRequest(body="x", is_internal=True)
        with pytest.raises(ValidationError):
            CreateMessageRequest(body="x", message_type="system")
        with pytest.raises(ValidationError):
            CreateMessageRequest(body="x", ticket_id=ticket.ticket_id)
