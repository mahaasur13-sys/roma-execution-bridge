"""Crypto Payments — Wallet Management Service."""
from __future__ import annotations

from datetime import datetime, timezone

from structlog import get_logger

from crypto_payments.settings import CryptoSettings
from crypto_payments.wallets.models import (
    CreateWalletRequest,
    CreateWalletResponse,
    CryptoWallet,
    DepositAddress,
    GenerateAddressRequest,
    GenerateAddressResponse,
    GenerateMoneroSubaddressRequest,
    GenerateMoneroSubaddressResponse,
    RotateWalletRequest,
    RotateWalletResponse,
    WalletMode,
    WalletRotationEvent,
    WalletStatus,
    WalletType,
)
from crypto_payments.wallets.monero_adapter import MoneroWalletAdapter
from crypto_payments.wallets.provider_wallet_adapter import ProviderWalletAdapter

logger = get_logger(__name__)


class CryptoWalletService:
    def __init__(self, settings: CryptoSettings | None = None) -> None:
        self._settings = settings or CryptoSettings()
        self._wallets: dict[str, CryptoWallet] = {}
        self._addresses: dict[str, DepositAddress] = {}
        self._monero_adapters: dict[str, MoneroWalletAdapter] = {}
        self._provider_adapters: dict[str, ProviderWalletAdapter] = {}

    async def create_wallet(self, request: CreateWalletRequest) -> CreateWalletResponse:
        if request.wallet_type == WalletType.MONERO and not request.monero_config:
            raise ValueError("Monero wallet requires monero_config")
        if request.mode == WalletMode.HOT:
            raise ValueError("HOT mode not allowed — DecisionOS never stores private keys")

        wallet = CryptoWallet(
            tenant_id=request.tenant_id,
            label=request.label,
            wallet_type=request.wallet_type,
            mode=request.mode,
            provider=request.provider,
            public_address=request.public_address,
            rpc_endpoint=request.rpc_endpoint,
            view_key=request.view_key,
            metadata=request.monero_config.model_dump() if request.monero_config else {},
        )
        wallet_id = str(wallet.wallet_id)
        self._wallets[wallet_id] = wallet

        if request.wallet_type == WalletType.MONERO and request.monero_config:
            adapter = MoneroWalletAdapter(config=request.monero_config)
            if not await adapter.health_check():
                raise RuntimeError("Monero wallet RPC health check failed")
            self._monero_adapters[wallet_id] = adapter
        elif request.wallet_type == WalletType.PROVIDER and request.provider:
            adapter = ProviderWalletAdapter(self._settings, request.provider)
            if not await adapter.health_check():
                raise RuntimeError(f"Provider {request.provider} health check failed")
            self._provider_adapters[wallet_id] = adapter

        logger.info("crypto_wallet_created", wallet_id=wallet_id, tenant_id=request.tenant_id, wallet_type=request.wallet_type.value)
        return CreateWalletResponse(
            wallet_id=wallet.wallet_id,
            tenant_id=wallet.tenant_id,
            label=wallet.label,
            wallet_type=wallet.wallet_type,
            mode=wallet.mode,
            status=wallet.status,
            created_at=wallet.created_at,
        )

    async def generate_address(self, wallet_id: str, request: GenerateAddressRequest) -> GenerateAddressResponse:
        wallet = self._wallets.get(wallet_id)
        if not wallet:
            raise ValueError(f"Wallet {wallet_id} not found")

        if wallet.wallet_type == WalletType.MONERO:
            return await self._generate_monero_address(wallet, request)
        elif wallet.wallet_type == WalletType.PROVIDER:
            return await self._generate_provider_address(wallet, request)
        else:
            return await self._generate_static_address(wallet, request)

    async def generate_monero_subaddress(
        self, wallet_id: str, request: GenerateMoneroSubaddressRequest
    ) -> GenerateMoneroSubaddressResponse:
        wallet = self._wallets.get(wallet_id)
        if not wallet or wallet.wallet_type != WalletType.MONERO:
            raise ValueError(f"Monero wallet {wallet_id} not found")
        adapter = self._monero_adapters.get(wallet_id)
        if not adapter:
            raise RuntimeError(f"No Monero adapter for wallet {wallet_id}")

        subaddress = await adapter.generate_subaddress(
            wallet_id=wallet_id,
            account_index=request.account_index,
            label=request.label,
        )

        deposit = DepositAddress(
            wallet_id=wallet.wallet_id,
            invoice_id=request.invoice_id,
            currency="XMR",
            network="MONERO",
            address=subaddress.address,
            subaddress_index=subaddress.subaddress_index,
            is_monero_subaddress=True,
            created_at=subaddress.created_at,
        )
        self._addresses[str(deposit.address_id)] = deposit
        logger.info("monero_subaddress_generated", wallet_id=wallet_id, index=subaddress.subaddress_index)
        return GenerateMoneroSubaddressResponse(
            subaddress_index=subaddress.subaddress_index,
            address=subaddress.address,
            account_index=subaddress.account_index,
            address_id=deposit.address_id,
            wallet_id=wallet.wallet_id,
        )

    async def rotate_wallet(self, wallet_id: str, request: RotateWalletRequest) -> RotateWalletResponse:
        wallet = self._wallets.get(wallet_id)
        if not wallet:
            raise ValueError(f"Wallet {wallet_id} not found")

        old_status = wallet.status
        wallet.status = WalletStatus.ROTATING
        event = WalletRotationEvent(
            wallet_id=wallet.wallet_id,
            tenant_id=wallet.tenant_id,
            old_address=wallet.public_address,
            new_address=request.new_public_address,
            reason=request.reason,
        )
        if request.new_public_address:
            wallet.public_address = request.new_public_address
        wallet.status = WalletStatus.ACTIVE
        wallet.rotated_at = datetime.now(timezone.utc)
        logger.info("crypto_wallet_rotated", wallet_id=wallet_id, reason=request.reason)
        return RotateWalletResponse(
            wallet_id=wallet.wallet_id,
            old_status=old_status,
            new_wallet_id=None,
            rotation_id=event.rotation_id,
            message=f"Wallet {wallet_id} rotated: {request.reason}",
        )

    def get_wallet(self, wallet_id: str) -> CryptoWallet | None:
        return self._wallets.get(wallet_id)

    async def close(self) -> None:
        for adapter in self._monero_adapters.values():
            await adapter.close()
        for adapter in self._provider_adapters.values():
            await adapter.close()

    async def _generate_monero_address(
        self, wallet: CryptoWallet, request: GenerateAddressRequest
    ) -> GenerateAddressResponse:
        adapter = self._monero_adapters.get(str(wallet.wallet_id))
        if not adapter:
            raise RuntimeError(f"No Monero adapter for wallet {wallet.wallet_id}")
        subaddress = await adapter.generate_subaddress(wallet_id=str(wallet.wallet_id))
        deposit = DepositAddress(
            wallet_id=wallet.wallet_id,
            invoice_id=request.invoice_id,
            currency="XMR",
            network="MONERO",
            address=subaddress.address,
            subaddress_index=subaddress.subaddress_index,
            is_monero_subaddress=True,
            created_at=subaddress.created_at,
        )
        self._addresses[str(deposit.address_id)] = deposit
        return GenerateAddressResponse(
            address_id=deposit.address_id,
            wallet_id=wallet.wallet_id,
            address=deposit.address,
            currency=deposit.currency,
            network=deposit.network,
            is_monero_subaddress=True,
            subaddress_index=deposit.subaddress_index,
            created_at=deposit.created_at,
        )

    async def _generate_provider_address(
        self, wallet: CryptoWallet, request: GenerateAddressRequest
    ) -> GenerateAddressResponse:
        adapter = self._provider_adapters.get(str(wallet.wallet_id))
        if not adapter:
            raise RuntimeError(f"No provider adapter for wallet {wallet.wallet_id}")
        result = await adapter.generate_address(request.currency, request.network)
        deposit = DepositAddress(
            wallet_id=wallet.wallet_id,
            invoice_id=request.invoice_id,
            currency=result["currency"],
            network=result["network"],
            address=result["address"],
            is_monero_subaddress=False,
            created_at=datetime.now(timezone.utc),
        )
        self._addresses[str(deposit.address_id)] = deposit
        return GenerateAddressResponse(
            address_id=deposit.address_id,
            wallet_id=wallet.wallet_id,
            address=deposit.address,
            currency=deposit.currency,
            network=deposit.network,
            is_monero_subaddress=False,
            subaddress_index=None,
            created_at=deposit.created_at,
        )

    async def _generate_static_address(
        self, wallet: CryptoWallet, request: GenerateAddressRequest
    ) -> GenerateAddressResponse:
        deposit = DepositAddress(
            wallet_id=wallet.wallet_id,
            invoice_id=request.invoice_id,
            currency=request.currency,
            network=request.network,
            address=wallet.public_address or f"static_{wallet.wallet_id}",
            is_monero_subaddress=False,
            created_at=datetime.now(timezone.utc),
        )
        self._addresses[str(deposit.address_id)] = deposit
        return GenerateAddressResponse(
            address_id=deposit.address_id,
            wallet_id=wallet.wallet_id,
            address=deposit.address,
            currency=deposit.currency,
            network=deposit.network,
            is_monero_subaddress=False,
            subaddress_index=None,
            created_at=deposit.created_at,
        )
