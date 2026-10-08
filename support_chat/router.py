"""Support Chat — FastAPI REST + WebSocket router.

H1 tenant/auth boundary:

  * tenant-facing routes (create/list/get/message/csat) depend on the canonical
    ``deps.verify_api_key`` and reduce the verified principal to a sanitised
    ``TenantContext`` (raw key dropped);
  * the tenant is never taken from a header or a body field;
  * the WebSocket is refused before ``accept()`` (close 4401).

H2a trusted-agent boundary:

  * ``assign``/``transition`` authenticate a dedicated support *session* — a
    hash-only credential exchanged for a one-hour opaque session cookie — and
    never accept a caller-supplied agent id, role or tenant;
  * a tenant API key on those routes stays a fail-closed 403, before any lookup;
  * the session cookie is Secure/HttpOnly/SameSite=Strict and every state-changing
    session call needs an allowlisted Origin plus a CSRF token;
  * the WebSocket stays unconditional 4401 until H2b.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
)

from deps import verify_api_key
from support_chat.identity import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    SESSION_TTL_SECONDS,
    SupportAuthError,
    SupportAuthService,
    SupportBrowserGuardError,
    SupportCredentialError,
    SupportPrincipal,
    SupportSessionError,
    origin_allowed,
)
from support_chat.models import (
    AssignSelfRequest,
    CreateMessageRequest,
    CreateTicketRequest,
    CreateTicketResponse,
    CsatSubmitRequest,
    SupportLoginRequest,
    SupportSessionResponse,
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
_auth = SupportAuthService()

# H1: an API key identifies a tenant, not a support role, so a tenant key on a
# privileged route stays closed. H2a keeps that rule; the privileged routes now
# accept a dedicated support *session* instead.
_PRIVILEGED_ROLE_REQUIRED = (
    "This operation requires a trusted support role; an API key identifies a "
    "tenant, not a support agent"
)


def _set_session_cookie(response: Response, raw_session: str) -> None:
    """Secure/HttpOnly/Strict cookie scoped to the support API only."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_session,
        max_age=SESSION_TTL_SECONDS,
        path=SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _require_allowed_origin(request: Request) -> None:
    """Fail-closed Origin allowlist for cookie-authenticated browser calls."""
    origin = (request.headers.get("origin") or "").strip()
    if not origin or not origin_allowed(origin):
        raise HTTPException(status_code=403, detail="Origin not allowed")


async def _require_csrf(request: Request, principal: SupportPrincipal) -> None:
    """State-changing session calls must echo the session's CSRF token."""
    presented = request.headers.get(CSRF_HEADER_NAME) or ""
    if not presented:
        raise HTTPException(status_code=403, detail="Missing CSRF token")
    try:
        matches = await _auth.csrf_matches(presented, principal.session_id)
    except SupportSessionError as exc:
        raise HTTPException(status_code=401, detail="Invalid support session") from exc
    if not matches:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


async def _tenant_context(principal: dict = Depends(verify_api_key)) -> TenantContext:
    """Reduce the canonical verified principal to a sanitised tenant context."""
    try:
        return TenantContext.from_principal(principal)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid API key") from exc


def _optional_tenant_principal(
    x_api_key: str | None = Header(None),
) -> dict | None:
    """Canonical tenant-key check, or ``None`` when no key is presented.

    Wrapper dependency used by the privileged ``_support_agent`` route gate. It
    calls the canonical synchronous verifier ``deps.verify_api_key`` directly, so
    the 401/403 behaviour is exactly the canonical one.

    Tests that need to substitute the tenant-key check override *this* wrapper;
    overriding ``verify_api_key`` does not propagate through FastAPI dependency
    resolution and would not affect this call site.

    The sync form is intentional: FastAPI runs synchronous dependencies in its
    threadpool, so the DB-backed verifier cannot block the event loop.
    """
    if not x_api_key:
        return None
    return verify_api_key(x_api_key)


async def _support_agent(
    request: Request,
    tenant_principal: dict | None = Depends(_optional_tenant_principal),
) -> SupportPrincipal:
    """H2a gate for privileged routes: a trusted support session, or nothing.

    Ordering is deliberate and preserves the H1 contract:

      * no credential at all            -> 401
      * both credential types presented -> 400, before any lookup
      * invalid/expired/revoked session -> 401
      * tenant API key                  -> canonical 401/403, then fail-closed 403
      * valid support session           -> Origin + CSRF, then the principal
    """
    raw_session = request.cookies.get(SESSION_COOKIE_NAME) or ""

    if raw_session and tenant_principal is not None:
        raise HTTPException(
            status_code=400,
            detail="Present either a support session or an API key, not both",
        )
    if raw_session:
        try:
            principal = await _auth.authenticate_session(raw_session)
        except SupportAuthError as exc:
            raise HTTPException(
                status_code=401, detail="Invalid support session"
            ) from exc
        _require_allowed_origin(request)
        await _require_csrf(request, principal)
        return principal
    if tenant_principal is not None:
        # The canonical key check already ran (401 missing/invalid, 403 unverified);
        # a tenant key still never grants a support role — refuse before any lookup.
        raise HTTPException(status_code=403, detail=_PRIVILEGED_ROLE_REQUIRED)
    raise HTTPException(
        status_code=401, detail="Support session or X-API-Key required"
    )


async def _support_agent_read(
    request: Request,
    tenant_principal: dict | None = Depends(_optional_tenant_principal),
) -> SupportPrincipal:
    """Read-only H2a-C1 gate: a trusted support session, and nothing else.

    Same credential ordering as :func:`_support_agent` — no credential 401,
    both credential types 400, invalid session 401 (never a tenant-key
    fallback), tenant API key 403 before any lookup — but without the browser
    write guards: these endpoints are side-effect-free GETs, so no Origin and
    no CSRF token is required.
    """
    raw_session = request.cookies.get(SESSION_COOKIE_NAME) or ""

    if raw_session and tenant_principal is not None:
        raise HTTPException(
            status_code=400,
            detail="Present either a support session or an API key, not both",
        )
    if raw_session:
        try:
            return await _auth.authenticate_session(raw_session)
        except SupportAuthError as exc:
            raise HTTPException(
                status_code=401, detail="Invalid support session"
            ) from exc
    if tenant_principal is not None:
        # A tenant key identifies a tenant, never a support agent.
        raise HTTPException(status_code=403, detail=_PRIVILEGED_ROLE_REQUIRED)
    raise HTTPException(
        status_code=401, detail="Support session or X-API-Key required"
    )


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


@router.get("/agent/tickets", response_model=TicketListResponse)
async def agent_list_tickets(
    principal: SupportPrincipal = Depends(_support_agent_read),
    tenant_id: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """H2a-C1: read the tickets of every explicit membership of the principal.

    ``tenant_id`` may only *narrow* the scope to one already-authorized
    membership; anything else is a non-enumerating 404 raised here, before any
    service or database work. The status filter follows the existing
    ``TicketStatus`` contract.
    """
    if tenant_id is not None and tenant_id not in principal.tenant_ids:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket_status: TicketStatus | None = None
    if status is not None:
        try:
            ticket_status = TicketStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid ticket status") from exc
    return await _service.agent_list_tickets(
        principal.tenant_ids,
        tenant_id=tenant_id,
        status=ticket_status,
        page=page,
        page_size=page_size,
    )


@router.get("/agent/tickets/{ticket_id}", response_model=TicketDetailResponse)
async def agent_get_ticket(
    ticket_id: str,
    principal: SupportPrincipal = Depends(_support_agent_read),
):
    """H2a-C1: membership-scoped detail, tenant-visible view only.

    Missing, malformed, foreign and out-of-scope ticket ids are all a uniform
    404. Reading does not require the ticket to be assigned to the caller, and
    internal notes stay hidden.
    """
    detail = await _service.agent_get_ticket_detail(ticket_id, principal.tenant_ids)
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
    request: AssignSelfRequest | None = Body(default=None),
    principal: SupportPrincipal = Depends(_support_agent),
):
    """H2a self-claim: the agent is always the authenticated principal.

    The route takes no body (``{}`` is tolerated) and rejects any extra field,
    so a caller cannot choose the assignee. The ticket must fall inside the
    principal's explicit tenant scope and the E-15 status/CAS contract holds.
    """
    try:
        return await _service.assign_agent_to_self(
            ticket_id, principal.actor_id, principal.tenant_ids
        )
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
    principal: SupportPrincipal = Depends(_support_agent),
):
    """H2a: only the agent the ticket is currently assigned to may transition it."""
    try:
        new_status = TicketStatus(status)
        return await _service.transition_status_as_assignee(
            ticket_id, new_status, principal.actor_id, principal.tenant_ids
        )
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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


@router.post("/auth/login", response_model=SupportSessionResponse)
async def support_login(
    request: Request,
    response: Response,
    payload: SupportLoginRequest,
    _origin_ok: None = Depends(_require_allowed_origin),
):
    """Exchange an owner-provisioned credential for a one-hour support session.

    The raw credential is verified by hash, never stored, echoed or logged; only
    the CSRF token is returned in the body, the session itself travels in a
    Secure/HttpOnly cookie.
    """
    try:
        principal, raw_session, csrf_token = await _auth.login(payload.credential)
    except SupportCredentialError as exc:
        raise HTTPException(status_code=401, detail="Invalid support credential") from exc
    except SupportBrowserGuardError as exc:
        raise HTTPException(status_code=403, detail="Origin not allowed") from exc
    except SupportAuthError as exc:
        raise HTTPException(status_code=401, detail="Invalid support credential") from exc
    _set_session_cookie(response, raw_session)
    return SupportSessionResponse(
        actor_id=principal.actor_id,
        roles=sorted(principal.roles),
        tenant_ids=sorted(principal.tenant_ids),
        csrf_token=csrf_token,
        expires_at=principal.expires_at,
    )


async def _support_session_principal(request: Request) -> SupportPrincipal:
    """Support-session-only dependency (no tenant API key on these routes)."""
    raw_session = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not raw_session:
        raise HTTPException(status_code=401, detail="Support session required")
    try:
        return await _auth.authenticate_session(raw_session)
    except SupportAuthError as exc:
        raise HTTPException(status_code=401, detail="Invalid support session") from exc


@router.post("/auth/logout", status_code=204)
async def support_logout(
    request: Request,
    response: Response,
    principal: SupportPrincipal = Depends(_support_session_principal),
) -> Response:
    """Revoke the current support session and clear the cookie."""
    raw_session = request.cookies.get(SESSION_COOKIE_NAME) or ""
    _require_allowed_origin(request)
    await _require_csrf(request, principal)
    await _auth.logout(raw_session)
    _clear_session_cookie(response)
    response.status_code = 204
    return response


@router.get("/auth/session")
async def support_session(
    principal: SupportPrincipal = Depends(_support_session_principal),
) -> dict:
    """Describe the current support session (no tokens, no credentials)."""
    return {
        "actor_id": principal.actor_id,
        "roles": sorted(principal.roles),
        "tenant_ids": sorted(principal.tenant_ids),
        "auth_method": principal.auth_method,
        "issued_at": principal.issued_at,
        "expires_at": principal.expires_at,
    }


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
