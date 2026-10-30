"""HTTP adapter for the host agent pull API (gpu_worker/host_api.py).

Mounted in main.py ONLY when ROMA_HOSTS_ENABLED=1 at import time; every request
re-checks the flag (503 when it is turned off at runtime).

Auth: ``Authorization: Bearer <host token>`` + ``X-Roma-Host-Id``. Tokens are
never logged or echoed. Storage: ROMA_HOSTS_STORE=memory (default) | pg.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from gpu_worker.host_api import HostApi
from gpu_worker.host_registry import HostRegistry
from gpu_worker.host_store import store_from_env

logger = logging.getLogger("roma.hosts.http")

router = APIRouter(prefix="/hosts/agent", tags=["hosts-agent"])

_api: HostApi | None = None
_api_mu = threading.Lock()


def get_host_api() -> HostApi:
    global _api
    with _api_mu:
        if _api is None:
            store = store_from_env()
            reg = HostRegistry(store=store)
            reg.load_from_store()
            _api = HostApi(reg, store=store)
        return _api


def set_host_api(api: HostApi | None) -> None:
    """Test / operator hook."""
    global _api
    with _api_mu:
        _api = api


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def _call(action: str, request: Request, authorization: str | None, host_id: str | None) -> Response:
    body: dict = {}
    if action == "submit_result":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        body = {k: body.get(k) for k in ("job_id", "job_token", "output_sha256", "elapsed_s")}
    try:
        res = get_host_api().handle(
            {**body, "action": action, "host_id": host_id or "", "token": _bearer(authorization)}
        )
    except Exception as e:  # noqa: BLE001 - fail closed, never leak details
        logger.warning("hosts.http.error action=%s err=%s", action, type(e).__name__)
        return JSONResponse({"error": "unavailable"}, status_code=503)
    code = int(res.pop("code", 500))
    if code == 204:
        return Response(status_code=204)
    return JSONResponse(res, status_code=code)


@router.post("/heartbeat")
async def heartbeat(
    request: Request, authorization: str | None = Header(None), x_roma_host_id: str | None = Header(None)
):
    return await _call("heartbeat", request, authorization, x_roma_host_id)


@router.post("/job")
async def fetch_job(
    request: Request, authorization: str | None = Header(None), x_roma_host_id: str | None = Header(None)
):
    return await _call("fetch_job", request, authorization, x_roma_host_id)


@router.post("/result")
async def submit_result(
    request: Request, authorization: str | None = Header(None), x_roma_host_id: str | None = Header(None)
):
    return await _call("submit_result", request, authorization, x_roma_host_id)
