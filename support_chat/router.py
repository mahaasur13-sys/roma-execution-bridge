"""Support Chat — FastAPI REST + WebSocket router.

H1 tenant/auth boundary:

  * every HTTP route depends on the canonical ``deps.verify_api_key`` and reduces
    the verified principal to a sanitised ``TenantContext`` (raw key dropped);
  * the tenant is never taken from a header or a body field;
  * ``assign``/``transition`` are fail-closed 403 until H2 supplies a trusted
    role — the gate runs as a dependency, i.e. before any lookup or mutation;
  * the WebSocket is refused before ``accept()`` (close 4401).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket

from deps import verify_api_key
from support_chat.models import (
    AssignTicketRequest,
    CreateMessageRequest,
    CreateTicketRequest,
    CreateTicketResponse,
    CsatSubmitRequest,
    TicketListResponse,
    TicketDetailResponse,
    TicketStatus,
)
from support_chat.service import (
    SupportTicketService,
    TenantContext,
    TicketNotFoundError,
    TicketStatusConflictError,
)
from support_chat.chat_service import ChatService
from support_chat.settings import SupportSettings

router = APIRouter(prefix="/v1/support", tags=["support_chat"])
_settings = SupportSettings()
_chat_svc = ChatService(settings=_settings)
_service = SupportTicketService(chat_service=_chat_svc, settings=_settings)

# H1: an API key identifies a tenant, not a support role, so privileged
# operations stay closed until H2 derives a trusted role from a verified principal.
_PRIVILEGED_ROLE_REQUIRED = (
    "This operation requires a trusted support role, which is not yet derived "
    "from an API key"
)


async def _tenant_context(principal: dict = Depends(verify_api_key)) -> TenantContext:
    """Reduce the canonical verified principal to a sanitised tenant context."""
    try:
        return TenantContext.from_principal(principal)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid API key") from exc


async def _require_trusted_role(
    tenant: TenantContext = Depends(_tenant_context),
) -> TenantContext:
    """H1 fail-closed gate: refuse privileged operations before any lookup.

    Runs after the canonical key check, so a missing/invalid key is still a 401.
    """
    raise HTTPException(status_code=403, detail=_PRIVILEGED_ROLE_REQUIRED)


@router.post("/tickets", response_model=CreateTicketResponse, status_code=201)
async def create_ticket(
    request: CreateTicketRequest, tenant: TenantContext = Depends(_tenant_context)
):
    try:
        return await _service.create_ticket(request, tenant)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tickets", response_model=TicketListResponse)
async def list_tickets(
    tenant: TenantContext = Depends(_tenant_context),
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    ticket_status = TicketStatus(status) if status else None
    return await _service.list_tickets(
        tenant, status=ticket_status, page=page, page_size=page_size
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
async def get_ticket(ticket_id: str, tenant: TenantContext = Depends(_tenant_context)):
    detail = await _service.get_ticket_detail(ticket_id, tenant)
    if not detail:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return detail


@router.post("/tickets/{ticket_id}/messages")
async def add_message(
    ticket_id: str,
    request: CreateMessageRequest,
    tenant: TenantContext = Depends(_tenant_context),
):
    try:
        return await _service.add_message(ticket_id, request, tenant)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tickets/{ticket_id}/assign")
async def assign_agent(
    ticket_id: str,
    request: AssignTicketRequest,
    tenant: TenantContext = Depends(_require_trusted_role),
):
    # Unreachable in H1 (the gate above always refuses); kept intact so H2 only
    # has to replace the gate and the tenant-scoped E-15 contract is unchanged.
    try:
        return await _service.assign_agent(ticket_id, request, tenant)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TicketStatusConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tickets/{ticket_id}/transition")
async def transition_status(
    ticket_id: str,
    status: str,
    tenant: TenantContext = Depends(_require_trusted_role),
):
    try:
        new_status = TicketStatus(status)
        return await _service.transition_status(ticket_id, new_status, tenant)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tickets/{ticket_id}/csat")
async def submit_csat(
    ticket_id: str,
    request: CsatSubmitRequest,
    tenant: TenantContext = Depends(_tenant_context),
):
    try:
        return await _service.submit_csat(ticket_id, request, tenant)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.websocket("/ws/{ticket_id}")
async def support_websocket(ws: WebSocket, ticket_id: str):
    """H1: refuse the browser WebSocket before accepting it.

    There is no trusted browser credential transport yet, and the old handler
    trusted a payload-supplied ticket id and role. The handshake is therefore
    denied (application close code 4401) without any ticket lookup or write;
    H2 adds the authenticated ticket/session flow.
    """
    await ws.close(
        code=4401,
        reason="Support chat WebSocket requires an authenticated session",
    )
