"""Support Chat — FastAPI REST + WebSocket router."""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from support_chat.models import (
    AssignTicketRequest,
    CreateMessageRequest,
    CreateTicketRequest,
    CreateTicketResponse,
    CsatSubmitRequest,
    MessageType,
    ParticipantRole,
    TicketListResponse,
    TicketDetailResponse,
    TicketStatus,
)
from support_chat.service import SupportTicketService
from support_chat.chat_service import ChatService
from support_chat.settings import SupportSettings

router = APIRouter(prefix="/v1/support", tags=["support_chat"])
_settings = SupportSettings()
_chat_svc = ChatService(settings=_settings)
_service = SupportTicketService(chat_service=_chat_svc, settings=_settings)


async def _get_tenant_id(request: Request) -> str:
    tenant = request.headers.get("x-tenant-id") or request.headers.get("x-api-key")
    if not tenant:
        raise HTTPException(status_code=401, detail="Missing tenant/auth header")
    return tenant


async def _get_user_id(request: Request) -> str:
    return request.headers.get("x-user-id", "anonymous")


async def _get_user_role(request: Request) -> str:
    return request.headers.get("x-user-role", ParticipantRole.TENANT_USER.value)


@router.post("/tickets", response_model=CreateTicketResponse, status_code=201)
async def create_ticket(
    request: CreateTicketRequest, user_id: str = Depends(_get_user_id)
):
    try:
        return await _service.create_ticket(request, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tickets", response_model=TicketListResponse)
async def list_tickets(
    tenant_id: str = Depends(_get_tenant_id),
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    ticket_status = TicketStatus(status) if status else None
    return _service.list_tickets(
        tenant_id, status=ticket_status, page=page, page_size=page_size
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
async def get_ticket(
    ticket_id: str,
    tenant_id: str = Depends(_get_tenant_id),
    user_role: str = Depends(_get_user_role),
):
    detail = _service.get_ticket_detail(ticket_id, user_role, tenant_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return detail


@router.post("/tickets/{ticket_id}/messages")
async def add_message(ticket_id: str, request: CreateMessageRequest):
    try:
        return await _service.add_message(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tickets/{ticket_id}/assign")
async def assign_agent(ticket_id: str, request: AssignTicketRequest):
    try:
        return await _service.assign_agent(ticket_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tickets/{ticket_id}/transition")
async def transition_status(ticket_id: str, status: str):
    try:
        new_status = TicketStatus(status)
        return await _service.transition_status(ticket_id, new_status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tickets/{ticket_id}/csat")
async def submit_csat(
    ticket_id: str, request: CsatSubmitRequest, user_id: str = Depends(_get_user_id)
):
    try:
        return await _service.submit_csat(ticket_id, request, user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.websocket("/ws/{ticket_id}")
async def support_websocket(ws: WebSocket, ticket_id: str):
    await ws.accept()
    user_id = ws.headers.get("x-user-id", f"ws-{uuid4().hex[:8]}")
    await _chat_svc.ws_manager.connect(ticket_id, user_id, ws)

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg = await _service.add_message(
                CreateMessageRequest(
                    ticket_id=data.get("ticket_id", ticket_id),
                    body=data.get("body", ""),
                    sender_id=user_id,
                    sender_role=ParticipantRole(
                        data.get("role", ParticipantRole.TENANT_USER.value)
                    ),
                    message_type=MessageType(data.get("type", "text")),
                )
            )
            await ws.send_json({"type": "ack", "message_id": str(msg.message_id)})
    except WebSocketDisconnect:
        await _chat_svc.ws_manager.disconnect(ticket_id, user_id)
    except Exception:
        await _chat_svc.ws_manager.disconnect(ticket_id, user_id)
