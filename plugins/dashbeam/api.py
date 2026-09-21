"""DashBeam Plugin API — FastAPI routes + WebSocket events."""

from __future__ import annotations


from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

# ── Pydantic request/response schemas ──


class CreateTicketRequest(BaseModel):
    file_name: str
    file_size: int
    file_hash: str
    mime_type: str = "application/octet-stream"
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class TicketResponse(BaseModel):
    ticket_id: str
    ticket_token: str
    file_name: str
    file_size_bytes: int
    file_hash: str
    relay_url: str
    status: str
    expires_at: str


class SessionRequest(BaseModel):
    device_name: str


class SessionResponse(BaseModel):
    session_id: str
    peer_node_id: str
    device_name: str
    relay_url: str
    status: str


class RelayConfigRequest(BaseModel):
    relay_url: str
    tier: str = "free"
    discovery_nodes: list[str] | None = None
    region: str = "auto"


class RelayConfigResponse(BaseModel):
    relay_url: str
    is_custom: bool
    relay_region: str
    max_bandwidth_mbps: int
    discovery_nodes: list[str]


class MoneroShareRequest(BaseModel):
    address: str
    view_key: str
    label: str = ""


class MoneroShareResponse(BaseModel):
    type: str
    address: str
    subaddresses: list[str]
    label: str
    warning: str


# ── Router factory ──


def create_router(service) -> APIRouter:
    """Build the FastAPI router for dashbeam-transfer plugin."""
    router = APIRouter(prefix="/api/v1/dashbeam", tags=["dashbeam-transfer"])

    # ── Tickets ──

    @router.post("/tickets", response_model=TicketResponse, status_code=201)
    async def create_ticket(
        req: CreateTicketRequest, tenant_id: str = "default"
    ) -> TicketResponse:
        """Create a one-time P2P transfer ticket."""
        ticket = await service.create_ticket(
            tenant_id=tenant_id,
            file_name=req.file_name,
            file_size=req.file_size,
            file_hash=req.file_hash,
            mime_type=req.mime_type,
        )
        return TicketResponse(
            ticket_id=ticket.ticket_id,
            ticket_token=ticket.ticket_token,
            file_name=ticket.file_name,
            file_size_bytes=ticket.file_size_bytes,
            file_hash=ticket.file_hash,
            relay_url=ticket.relay_url,
            status=ticket.status.value,
            expires_at=ticket.expires_at.isoformat(),
        )

    @router.get("/tickets/{ticket_id}", response_model=TicketResponse)
    async def get_ticket(ticket_id: str) -> TicketResponse:
        ticket = service.get_ticket(ticket_id)
        if not ticket:
            raise HTTPException(404, f"Ticket {ticket_id} not found")
        return TicketResponse(
            ticket_id=ticket.ticket_id,
            ticket_token=ticket.ticket_token,
            file_name=ticket.file_name,
            file_size_bytes=ticket.file_size_bytes,
            file_hash=ticket.file_hash,
            relay_url=ticket.relay_url,
            status=ticket.status.value,
            expires_at=ticket.expires_at.isoformat(),
        )

    @router.get("/tickets")
    async def list_tickets(tenant_id: str = "default") -> list[TicketResponse]:
        tickets = service.list_tickets(tenant_id)
        return [
            TicketResponse(
                ticket_id=t.ticket_id,
                ticket_token=t.ticket_token,
                file_name=t.file_name,
                file_size_bytes=t.file_size_bytes,
                file_hash=t.file_hash,
                relay_url=t.relay_url,
                status=t.status.value,
                expires_at=t.expires_at.isoformat(),
            )
            for t in tickets
        ]

    @router.post("/tickets/{ticket_id}/complete")
    async def complete_ticket(ticket_id: str) -> TicketResponse:
        ticket = await service.complete_ticket(ticket_id)
        return TicketResponse(
            ticket_id=ticket.ticket_id,
            ticket_token=ticket.ticket_token,
            file_name=ticket.file_name,
            file_size_bytes=ticket.file_size_bytes,
            file_hash=ticket.file_hash,
            relay_url=ticket.relay_url,
            status=ticket.status.value,
            expires_at=ticket.expires_at.isoformat(),
        )

    # ── Sessions ──

    @router.post("/sessions", response_model=SessionResponse, status_code=201)
    async def start_session(
        req: SessionRequest, tenant_id: str = "default"
    ) -> SessionResponse:
        session = await service.start_session(tenant_id, req.device_name)
        return SessionResponse(
            session_id=session.session_id,
            peer_node_id=session.peer_node_id,
            device_name=session.device_name,
            relay_url=session.relay_url,
            status=session.status.value,
        )

    @router.get("/sessions")
    async def list_sessions(tenant_id: str = "default") -> list[SessionResponse]:
        sessions = service.list_sessions(tenant_id)
        return [
            SessionResponse(
                session_id=s.session_id,
                peer_node_id=s.peer_node_id,
                device_name=s.device_name,
                relay_url=s.relay_url,
                status=s.status.value,
            )
            for s in sessions
        ]

    # ── Relay Config ──

    @router.get("/relay", response_model=RelayConfigResponse)
    async def get_relay_config(tenant_id: str = "default") -> RelayConfigResponse:
        config = service.get_relay_config(tenant_id)
        return RelayConfigResponse(
            relay_url=config.relay_url,
            is_custom=config.is_custom,
            relay_region=config.relay_region,
            max_bandwidth_mbps=config.max_bandwidth_mbps,
            discovery_nodes=config.discovery_nodes,
        )

    @router.put("/relay", response_model=RelayConfigResponse)
    async def set_relay_config(
        req: RelayConfigRequest, tenant_id: str = "default"
    ) -> RelayConfigResponse:
        try:
            config = service.set_relay_config(
                tenant_id,
                req.relay_url,
                tier=req.tier,
                discovery_nodes=req.discovery_nodes,
            )
        except PermissionError as e:
            raise HTTPException(403, str(e))
        return RelayConfigResponse(
            relay_url=config.relay_url,
            is_custom=config.is_custom,
            relay_region=config.relay_region,
            max_bandwidth_mbps=config.max_bandwidth_mbps,
            discovery_nodes=config.discovery_nodes,
        )

    # ── Monero Share ──

    @router.post("/monero/share", response_model=MoneroShareResponse)
    async def share_monero(
        req: MoneroShareRequest, tenant_id: str = "default"
    ) -> MoneroShareResponse:
        from plugins.dashbeam.integrations.crypto_wallets import MoneroSharingValidator

        validator = MoneroSharingValidator()
        payload = validator.build_monero_share_payload(
            address=req.address,
            view_key=req.view_key,
            label=req.label,
        )
        validator.validate_share_data(payload)

        # Also validate via the service
        service.validate_monero_view_only(payload)

        return MoneroShareResponse(
            type=payload["type"],
            address=payload["address"],
            subaddresses=payload["subaddresses"],
            label=payload["label"],
            warning=payload["warning"],
        )

    # ── WebSocket for real-time transfer events ──

    @router.websocket("/ws/transfer")
    async def ws_transfer(ws: WebSocket, tenant_id: str = "default"):
        await ws.accept()
        await ws.send_json({"type": "dashbeam:connected", "tenant_id": tenant_id})
        try:
            while True:
                data = await ws.receive_json()
                event_type = data.get("type", "")
                if event_type == "ping":
                    await ws.send_json({"type": "pong"})
                elif event_type == "subscribe:transfer":
                    ticket_id = data.get("ticket_id", "")
                    ticket = service.get_ticket(ticket_id)
                    if ticket:
                        await ws.send_json(
                            {
                                "type": "dashbeam:transfer:status",
                                "ticket_id": ticket_id,
                                "status": ticket.status.value,
                            }
                        )
        except WebSocketDisconnect:
            pass

    return router
