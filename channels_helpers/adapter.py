"""
Abstract base class for platform channel adapters.

Each platform (Telegram, Discord, WhatsApp) implements this ABC.
Adapters handle:
  - Connection lifecycle (auth, polling/webhooks, disconnect)
  - Inbound: platform event → ChannelMessage normalization
  - Outbound: ChannelMessage → platform API call
  - Format conversion (markdown dialect at the boundary)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from channels_helpers.schema import ChannelMessage

logger = logging.getLogger("channels")

# Callback signature: async (ChannelMessage) -> None
InboundCallback = Callable[[ChannelMessage], Awaitable[None]]


class ChannelAdapter(ABC):
    """
    Base adapter for a messaging platform.

    Lifecycle:
        1. __init__(config, on_message) — store config, register callback
        2. connect()                    — authenticate & start listening
        3. ... messages flow ...
        4. disconnect()                 — clean shutdown

    Adapters MUST:
        - Normalize all inbound events to ChannelMessage before calling on_message
        - Convert outbound ChannelMessage content to platform-native format in send()
        - Handle reconnection internally (exponential backoff)
        - Be safe to call disconnect() even if not connected
    """

    def __init__(self, config: dict[str, Any], on_message: InboundCallback):
        self._config = config
        self._on_message = on_message
        self._connected = False

    # ── identity ──

    @property
    @abstractmethod
    def platform(self) -> str:
        """Platform identifier: 'telegram', 'discord', 'whatsapp'."""
        ...

    @property
    def connected(self) -> bool:
        return self._connected

    # ── lifecycle ──

    @abstractmethod
    async def connect(self) -> None:
        """
        Authenticate with the platform and start receiving messages.
        Sets self._connected = True on success.
        Raises ConnectionError on auth failure.
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Gracefully shut down. Safe to call multiple times.
        Sets self._connected = False.
        """
        ...

    # ── messaging ──

    @abstractmethod
    async def send(self, message: ChannelMessage) -> str | None:
        """
        Send a message to the platform.

        Args:
            message: Outbound ChannelMessage with channel_id and content set.

        Returns:
            Platform message ID on success, None on failure.

        The adapter is responsible for:
            - Converting message.content from common markdown to platform format
            - Uploading attachments via platform API
            - Handling reply_to threading if the platform supports it
        """
        ...

    # ── optional hooks ──

    async def on_connect(self) -> None:
        """Called after successful connect(). Override for post-auth setup."""
        pass

    async def on_disconnect(self) -> None:
        """Called before disconnect teardown. Override for cleanup."""
        pass

    # ── formatting helpers (adapters override as needed) ──

    def format_outbound(self, content: str) -> str:
        """
        Convert common markdown to platform-native format.
        Default: pass through unchanged. Override per platform.

        Common markdown subset:
            **bold**  _italic_  `code`  ```codeblock```  [text](url)
        """
        return content

    def normalize_inbound(self, content: str) -> str:
        """
        Convert platform-native format to common markdown.
        Default: pass through unchanged. Override per platform.
        """
        return content

    # ── status ──

    def status(self) -> dict[str, Any]:
        """Return adapter status for the dashboard API."""
        return {
            "platform": self.platform,
            "connected": self.connected,
        }
