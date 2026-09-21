"""DashBeam ↔ Support Chat integration.

Adds a "Send via DashBeam" button to chat messages.
Large attachments are routed P2P instead of server upload.
WebSocket events notify peers of ticket creation.
"""

from __future__ import annotations

from typing import Any
from enum import StrEnum


class DashBeamChatEvent(StrEnum):
    """WebSocket events for DashBeam chat integration."""

    TICKET_CREATED = "dashbeam:ticket:created"
    TICKET_ACCEPTED = "dashbeam:ticket:accepted"
    TICKET_COMPLETED = "dashbeam:ticket:completed"
    TICKET_EXPIRED = "dashbeam:ticket:expired"
    TRANSFER_PROGRESS = "dashbeam:transfer:progress"
    PEER_CONNECTED = "dashbeam:peer:connected"
    PEER_DISCONNECTED = "dashbeam:peer:disconnected"


class DashBeamChatIntegration:
    """Integrates DashBeam P2P transfers into the Support Chat UI.

    Chat messages with large attachments (>10MB) get a "Send via DashBeam"
    button instead of uploading to the server. This keeps server storage
    minimal and lets users transfer files directly between devices.
    """

    MAX_INLINE_SIZE_MB: int = 10

    def __init__(self) -> None:
        self._active_tickets: dict[str, str] = {}  # chat_msg_id → ticket_id

    def should_use_dashbeam(self, file_size_bytes: int) -> bool:
        """Determine if a file should use P2P transfer vs server upload."""
        return file_size_bytes > self.MAX_INLINE_SIZE_MB * 1024 * 1024

    def build_chat_button_html(self, ticket_id: str) -> str:
        """Generate the HTML button for a chat message."""
        return (
            f'<button class="dashbeam-chat-btn" '
            f'data-ticket="{ticket_id}" '
            f'onclick="window.DashBeamChat.acceptTicket(\'{ticket_id}\')">'
            f'⬇ Send via DashBeam'
            f'</button>'
        )

    def build_chat_event(
        self, event: DashBeamChatEvent, ticket_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Build a WebSocket event payload."""
        return {
            "type": event.value,
            "ticket_id": ticket_id,
            **kwargs,
        }

    def link_ticket_to_message(self, msg_id: str, ticket_id: str) -> None:
        self._active_tickets[msg_id] = ticket_id

    def get_ticket_for_message(self, msg_id: str) -> str | None:
        return self._active_tickets.get(msg_id)

    def unlink_message(self, msg_id: str) -> None:
        self._active_tickets.pop(msg_id, None)
