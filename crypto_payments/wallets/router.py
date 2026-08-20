"""Crypto Payments — Wallet Management API Router."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from crypto_payments.settings import CryptoSettings
from crypto_payments.wallets.models import (
    CreateWalletRequest,
    CreateWalletResponse,
    GenerateAddressRequest,
    GenerateAddressResponse,
    GenerateMoneroSubaddressRequest,
    GenerateMoneroSubaddressResponse,
    RotateWalletRequest,
    RotateWalletResponse,
)
from crypto_payments.wallets.service import CryptoWalletService

router = APIRouter(prefix="/v1/crypto/wallets", tags=["crypto_wallets"])
_settings = CryptoSettings()
_service = CryptoWalletService(settings=_settings)


async def _get_tenant_id(request: Request) -> str:
    tenant = request.headers.get("x-tenant-id") or request.headers.get("x-api-key")
    if not tenant:
        raise HTTPException(status_code=401, detail="Missing tenant/auth header")
    return tenant


@router.post("", response_model=CreateWalletResponse, status_code=201)
async def create_wallet(request: CreateWalletRequest):
    try:
        return await _service.create_wallet(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{wallet_id}/addresses", response_model=GenerateAddressResponse)
async def generate_address(wallet_id: str, request: GenerateAddressRequest):
    try:
        return await _service.generate_address(wallet_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{wallet_id}/monero/subaddress", response_model=GenerateMoneroSubaddressResponse)
async def generate_monero_subaddress(wallet_id: str, request: GenerateMoneroSubaddressRequest):
    try:
        return await _service.generate_monero_subaddress(wallet_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{wallet_id}/rotate", response_model=RotateWalletResponse)
async def rotate_wallet(wallet_id: str, request: RotateWalletRequest):
    try:
        return await _service.rotate_wallet(wallet_id, request)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
