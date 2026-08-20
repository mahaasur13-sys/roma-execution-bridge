"""DecisionOS Crypto Wallets — 8 smoke tests (3+ Monero)."""
from __future__ import annotations

from uuid import UUID
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from crypto_payments.wallets.models import (
    CreateWalletRequest,
    WalletType,
    WalletMode,
    WalletStatus,
    GenerateAddressRequest,
    GenerateMoneroSubaddressRequest,
    RotateWalletRequest,
    MoneroViewOnlyConfig,
    MoneroSubaddress,
    WalletRotationEvent,
)
from crypto_payments.wallets.service import CryptoWalletService
from crypto_payments.wallets.monero_adapter import MoneroWalletAdapter
from crypto_payments.settings import CryptoSettings


@pytest.fixture
def settings() -> CryptoSettings:
    return CryptoSettings(
        provider="nowpayments",
        nowpayments_api_key="test-key",
        nowpayments_ipn_secret="test-secret",
    )


@pytest.fixture
def monero_config() -> MoneroViewOnlyConfig:
    return MoneroViewOnlyConfig(
        primary_address="48fp8gQfd...monero_address",
        view_key_private="abcdef...viewkey",
        rpc_url="http://127.0.0.1:18082/json_rpc",
        wallet_rpc_url="http://127.0.0.1:18084/json_rpc",
    )


class TestWalletCreation:

    @pytest.mark.asyncio
    async def test_01_create_provider_wallet(self, settings: CryptoSettings) -> None:
        """Smoke 1: Create provider-type wallet."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="NOWPayments USDT Wallet",
            wallet_type=WalletType.PROVIDER,
            mode=WalletMode.VIEW_ONLY,
            provider="nowpayments",
        )
        with patch.object(svc, "_provider_adapters", {}):
            with patch("crypto_payments.wallets.service.ProviderWalletAdapter") as mock:
                mock_instance = MagicMock()
                mock_instance.health_check = AsyncMock(return_value=True)
                mock.return_value = mock_instance
                result = await svc.create_wallet(req)
                assert result.wallet_id is not None
                assert result.label == "NOWPayments USDT Wallet"
                assert result.wallet_type == WalletType.PROVIDER

    @pytest.mark.asyncio
    async def test_02_create_monero_view_only_wallet(self, settings: CryptoSettings, monero_config: MoneroViewOnlyConfig) -> None:
        """Smoke 2: Create Monero view-only wallet (privacy-first)."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Monero View-Only Wallet",
            wallet_type=WalletType.MONERO,
            mode=WalletMode.VIEW_ONLY,
            monero_config=monero_config,
        )
        with patch("crypto_payments.wallets.service.MoneroWalletAdapter") as mock:
            mock_instance = MagicMock()
            mock_instance.health_check = AsyncMock(return_value=True)
            mock.return_value = mock_instance
            result = await svc.create_wallet(req)
            assert result.wallet_id is not None
            assert result.wallet_type == WalletType.MONERO
            assert result.mode == WalletMode.VIEW_ONLY

    @pytest.mark.asyncio
    async def test_03_hot_mode_blocked(self, settings: CryptoSettings) -> None:
        """Smoke 3: HOT mode is blocked — DecisionOS never stores private keys."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Hot wallet attempt",
            wallet_type=WalletType.PROVIDER,
            mode=WalletMode.HOT,
            provider="nowpayments",
        )
        with pytest.raises(ValueError, match="HOT mode not allowed"):
            await svc.create_wallet(req)

    @pytest.mark.asyncio
    async def test_04_monero_missing_config_blocked(self, settings: CryptoSettings) -> None:
        """Smoke 4: Monero wallet without config raises error."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Monero without config",
            wallet_type=WalletType.MONERO,
            mode=WalletMode.VIEW_ONLY,
        )
        with pytest.raises(ValueError, match="Monero wallet requires monero_config"):
            await svc.create_wallet(req)


class TestMoneroWallet:

    @pytest.mark.asyncio
    async def test_05_generate_monero_subaddress(self, settings: CryptoSettings, monero_config: MoneroViewOnlyConfig) -> None:
        """Smoke 5: Generate Monero subaddress from view-only wallet."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Monero Subaddress Wallet",
            wallet_type=WalletType.MONERO,
            mode=WalletMode.VIEW_ONLY,
            monero_config=monero_config,
        )
        mock_adapter = MagicMock(spec=MoneroWalletAdapter)
        mock_adapter.health_check = AsyncMock(return_value=True)
        mock_adapter.generate_subaddress = AsyncMock(return_value=MoneroSubaddress(
            wallet_id=UUID("11111111-1111-1111-1111-111111111111"),
            account_index=0,
            subaddress_index=5,
            address="8Abc...monero_subaddress",
            label="DecisionOS-w1",
        ))

        with patch("crypto_payments.wallets.service.MoneroWalletAdapter", return_value=mock_adapter):
            wallet = await svc.create_wallet(req)
            sub_req = GenerateMoneroSubaddressRequest(
                account_index=0,
                label="invoice-123",
                invoice_id=uuid4(),
            )
            result = await svc.generate_monero_subaddress(str(wallet.wallet_id), sub_req)
            assert result.subaddress_index == 5
            assert "monero_subaddress" in result.address
            assert result.wallet_id == wallet.wallet_id

    @pytest.mark.asyncio
    async def test_06_monero_generate_address_creates_subaddress(self, settings: CryptoSettings, monero_config: MoneroViewOnlyConfig) -> None:
        """Smoke 6: generate_address for Monero wallet creates a subaddress."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Monero Deposit Wallet",
            wallet_type=WalletType.MONERO,
            mode=WalletMode.VIEW_ONLY,
            monero_config=monero_config,
        )
        mock_adapter = MagicMock(spec=MoneroWalletAdapter)
        mock_adapter.health_check = AsyncMock(return_value=True)
        mock_adapter.generate_subaddress = AsyncMock(return_value=MoneroSubaddress(
            wallet_id=UUID("11111111-1111-1111-1111-111111111111"),
            account_index=0,
            subaddress_index=3,
            address="8Xyz...monero_deposit_subaddress",
        ))

        with patch("crypto_payments.wallets.service.MoneroWalletAdapter", return_value=mock_adapter):
            wallet = await svc.create_wallet(req)
            addr_req = GenerateAddressRequest(
                currency="XMR",
                network="MONERO",
                invoice_id=uuid4(),
            )
            result = await svc.generate_address(str(wallet.wallet_id), addr_req)
            assert result.is_monero_subaddress is True
            assert result.subaddress_index == 3
            assert result.currency == "XMR"
            assert result.network == "MONERO"

    @pytest.mark.asyncio
    async def test_07_monero_view_only_no_spend_key(self, settings: CryptoSettings, monero_config: MoneroViewOnlyConfig) -> None:
        """Smoke 7: Monero wallet adapter never exposes spend keys."""
        adapter = MoneroWalletAdapter(config=monero_config)
        assert adapter._config.view_key_private is not None
        assert "spend" not in dir(adapter)
        assert not hasattr(adapter, "spend_key")
        assert not hasattr(adapter, "spend_key_private")


class TestWalletRotation:

    @pytest.mark.asyncio
    async def test_08_wallet_rotation(self, settings: CryptoSettings, monero_config: MoneroViewOnlyConfig) -> None:
        """Smoke 8: Rotate wallet and track event."""
        svc = CryptoWalletService(settings=settings)
        req = CreateWalletRequest(
            tenant_id="t1",
            label="Monero Rotatable Wallet",
            wallet_type=WalletType.MONERO,
            mode=WalletMode.VIEW_ONLY,
            monero_config=monero_config,
        )
        mock_adapter = MagicMock(spec=MoneroWalletAdapter)
        mock_adapter.health_check = AsyncMock(return_value=True)

        with patch("crypto_payments.wallets.service.MoneroWalletAdapter", return_value=mock_adapter):
            wallet = await svc.create_wallet(req)

        rotate_req = RotateWalletRequest(
            reason="compliance_rekey",
            new_public_address="4New...monero_rotated_address",
        )
        result = await svc.rotate_wallet(str(wallet.wallet_id), rotate_req)
        assert result.wallet_id == wallet.wallet_id
        assert result.old_status == WalletStatus.ACTIVE
        assert "rotated" in result.message.lower()

    def test_09_monero_view_only_config_model(self) -> None:
        """MoneroViewOnlyConfig Pydantic model validation."""
        config = MoneroViewOnlyConfig(
            primary_address="4Primary...",
            view_key_private="viewkey123...",
        )
        assert config.primary_address == "4Primary..."
        assert config.view_key_private == "viewkey123..."
        assert config.rpc_url == "http://127.0.0.1:18082/json_rpc"
        assert config.subaddress_index == 0

    def test_10_wallet_rotation_event_model(self) -> None:
        """WalletRotationEvent Pydantic model serialization."""
        event = WalletRotationEvent(
            wallet_id=uuid4(),
            tenant_id="t1",
            old_address="old_addr",
            new_address="new_addr",
            reason="security_audit",
        )
        d = event.model_dump()
        assert d["reason"] == "security_audit"
        assert d["old_address"] == "old_addr"
        assert d["new_address"] == "new_addr"
