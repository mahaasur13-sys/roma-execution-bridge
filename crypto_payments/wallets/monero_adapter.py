"""Crypto Payments — Monero Wallet Adapter (monero-wallet-rpc + Feather)."""
from __future__ import annotations

from typing import Any

import httpx
from structlog import get_logger

from crypto_payments.wallets.models import MoneroSubaddress, MoneroViewOnlyConfig

logger = get_logger(__name__)


class MoneroWalletAdapter:
    """Privacy-first Monero integration.

    SECURITY: Never stores spend keys. View-only mode exclusively.
    All interactions go through monero-wallet-rpc (JSON-RPC 2.0).
    """

    def __init__(self, config: MoneroViewOnlyConfig | None = None, wallet_rpc_url: str = "") -> None:
        self._config = config or MoneroViewOnlyConfig(primary_address="", view_key_private="")
        self._rpc_url = wallet_rpc_url or self._config.wallet_rpc_url
        self._http = httpx.AsyncClient(timeout=30)
        self._subaddress_counter: dict[str, int] = {}

    async def health_check(self) -> bool:
        try:
            result = await self._rpc_call("get_version", {})
            return result.get("result") is not None
        except Exception:
            logger.error("monero_rpc_health_check_failed")
            return False

    async def generate_subaddress(
        self,
        wallet_id: str,
        account_index: int = 0,
        label: str | None = None,
    ) -> MoneroSubaddress:
        key = f"{wallet_id}:{account_index}"
        index = self._subaddress_counter.get(key, 0)
        result = await self._rpc_call("create_address", {
            "account_index": account_index,
            "label": label or f"DecisionOS-{wallet_id[:8]}",
        })
        data = result.get("result", {})
        self._subaddress_counter[key] = index + 1
        logger.info("monero_subaddress_created", wallet_id=wallet_id, index=index)
        return MoneroSubaddress(
            wallet_id=wallet_id,
            account_index=account_index,
            subaddress_index=data.get("address_index", index),
            address=data.get("address", ""),
            label=label,
        )

    async def get_balance(self, account_index: int = 0) -> dict[str, Any]:
        result = await self._rpc_call("get_balance", {"account_index": account_index})
        return result.get("result", {})

    async def get_transfers(self, account_index: int = 0, min_confirmations: int | None = None) -> list[dict]:
        params: dict[str, Any] = {
            "account_index": account_index,
            "in": True,
        }
        if min_confirmations is not None:
            params["filter_by_height"] = True
        result = await self._rpc_call("get_transfers", params)
        transfers = result.get("result", {}).get("in", [])
        if min_confirmations:
            transfers = [t for t in transfers if t.get("confirmations", 0) >= min_confirmations]
        return transfers

    async def import_view_only(self, restore_height: int = 0) -> dict[str, Any]:
        result = await self._rpc_call("generate_from_keys", {
            "restore_height": restore_height,
            "address": self._config.primary_address,
            "viewkey": self._config.view_key_private,
        })
        logger.info("monero_view_only_imported", address=self._config.primary_address[:12])
        return result.get("result", {})

    async def close(self) -> None:
        await self._http.aclose()

    async def _rpc_call(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params}
        resp = await self._http.post(self._rpc_url, json=payload)
        resp.raise_for_status()
        return resp.json()
