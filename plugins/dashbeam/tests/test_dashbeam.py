"""DashBeam Transfer Plugin — 14 smoke tests."""

from __future__ import annotations


import pytest

from plugins.dashbeam.domain import (
    DashBeamTicket, DashBeamSession, DashBeamRelayConfig,
    PairedDevice, TicketState, TicketType,
)
from plugins.dashbeam.relay_manager import RelayManager, DEFAULT_RELAY
from plugins.dashbeam.policy_actions import (
    can_create_ticket, can_send_file, can_configure_relay,
)
from plugins.dashbeam.integrations.crypto_wallets import MoneroSharingValidator
from plugins.dashbeam.integrations.support_chat import DashBeamChatIntegration, DashBeamChatEvent
from plugins.dashbeam.integrations.audit import DashBeamAuditExporter
from plugins.dashbeam.iroh_adapter import IrohAdapter


class TestDashBeamDomain:
    """Domain model constructors, defaults, validation."""

    def test_ticket_defaults(self):
        t = DashBeamTicket(tenant_id="t1", created_by="u1", ticket_type=TicketType.FILE_TRANSFER)
        assert t.state == TicketState.CREATED
        assert t.tenant_id == "t1"
        assert t.file_size_bytes == 0

    def test_ticket_state_enum(self):
        t = DashBeamTicket(tenant_id="t2", created_by="u2",
                           ticket_type=TicketType.FILE_TRANSFER,
                           state=TicketState.PENDING)
        assert t.state == TicketState.PENDING

    def test_session_defaults(self):
        s = DashBeamSession(ticket_id="t-1", tenant_id="t1", sender_device_id="d1")
        assert s.state == TicketState.PENDING
        assert s.ticket_id == "t-1"
        assert s.bytes_transferred == 0

    def test_session_requires_ticket_id(self):
        with pytest.raises(ValueError):
            DashBeamSession(tenant_id="t1", sender_device_id="d1")

    def test_relay_config_defaults(self):
        c = DashBeamRelayConfig(tenant_id="t1", relay_url="https://r.io")
        assert c.relay_url == "https://r.io"
        assert c.is_custom is False

    def test_paired_device(self):
        d = PairedDevice(tenant_id="t1", user_id="u1", device_name="laptop", device_fingerprint="fp1")
        assert d.device_name == "laptop"


class TestRelayManager:
    """Relay configuration management."""

    def test_default_config(self):
        rm = RelayManager()
        c = rm.get_config("t1")
        assert c.relay_url == DEFAULT_RELAY
        assert c.is_custom is False

    def test_enterprise_custom_relay(self):
        rm = RelayManager()
        c = rm.set_config("e1", "https://myrelay.io", tier="enterprise")
        assert c.is_custom is True

    def test_free_custom_relay_blocked(self):
        rm = RelayManager()
        with pytest.raises(PermissionError, match="Enterprise"):
            rm.set_config("f1", "https://myrelay.io", tier="free")


class TestPolicyActions:
    """Policy Engine integration."""

    def test_free_ticket_limit(self):
        ok, _ = can_create_ticket("t1", "free", 5)
        assert ok
        ok2, _ = can_create_ticket("t1", "free", 11)
        assert not ok2

    def test_free_file_size_limit(self):
        ok, _ = can_send_file("t1", "free", 50 * 1024 * 1024)
        assert ok
        ok2, _ = can_send_file("t1", "free", 200 * 1024 * 1024)
        assert not ok2

    def test_enterprise_can_configure_relay(self):
        ok, _ = can_configure_relay("e1", "enterprise", True)
        assert ok


class TestMoneroSharing:
    """Monero view-only sharing — spend-key blocking."""

    def test_spend_key_blocked(self):
        v = MoneroSharingValidator()
        with pytest.raises(ValueError, match="SPEND KEY"):
            v.validate_share_data({"spend_key": "secret"})

    def test_seed_blocked(self):
        v = MoneroSharingValidator()
        with pytest.raises(ValueError, match="SPEND KEY"):
            v.validate_share_data({"mnemonic": "words here"})

    def test_valid_view_only_payload(self):
        v = MoneroSharingValidator()
        p = v.build_monero_share_payload("4" + "a" * 94, "v" * 64, label="test")
        assert p["type"] == "monero_view_only_share"
        assert "VIEW-ONLY" in p["warning"]


class TestChatIntegration:
    """Support Chat integration."""

    def test_should_use_dashbeam(self):
        ci = DashBeamChatIntegration()
        assert ci.should_use_dashbeam(20 * 1024 * 1024) is True
        assert ci.should_use_dashbeam(1024) is False

    def test_build_chat_event(self):
        ci = DashBeamChatIntegration()
        evt = ci.build_chat_event(DashBeamChatEvent.TICKET_CREATED, "t-1")
        assert evt["type"] == "dashbeam:ticket:created"

    def test_link_ticket_to_message(self):
        ci = DashBeamChatIntegration()
        ci.link_ticket_to_message("msg1", "t-1")
        assert ci.get_ticket_for_message("msg1") == "t-1"
        ci.unlink_message("msg1")
        assert ci.get_ticket_for_message("msg1") is None


class TestAuditExport:
    """Audit Trail export."""

    def test_build_export_payload(self):
        ex = DashBeamAuditExporter()
        p = ex.build_export_payload("t1", [{"a": 1}], date_from="2026-01-01")
        assert p["tenant_id"] == "t1"
        assert p["total_records"] == 1

    def test_export_all_tiers(self):
        ex = DashBeamAuditExporter()
        assert ex.validate_export_request("t1", "free") is True


class TestIrohAdapterSync:
    """Iroh adapter — sync operations only (no async needed)."""

    def test_adapter_creation(self):
        a = IrohAdapter()
        assert len(a.node_id) == 32
        assert a.relay_url == "https://relay.dashbeam.io"

    def test_create_session(self):
        a = IrohAdapter()
        s = DashBeamSession(ticket_id="t-1", tenant_id="t1", sender_device_id="d1")
        assert s.state == TicketState.PENDING
