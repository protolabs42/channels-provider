"""
Normalized message schema for the channels plugin.

All platform messages (Telegram, Discord, WhatsApp, etc.) are converted
to/from ChannelMessage at the adapter boundary. Internal code never
touches platform-specific formats.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Direction(str, Enum):
    """Message flow direction."""
    INBOUND = "inbound"    # platform → agent
    OUTBOUND = "outbound"  # agent → platform


class MessageType(str, Enum):
    """Content type of the message."""
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    STICKER = "sticker"
    LOCATION = "location"
    COMMAND = "command"      # /start, /help — platform bot commands


@dataclass
class Attachment:
    """
    A file, image, or media item attached to a message.

    Adapters populate either `url` (preferred — lazy download) or `data`
    (small inline items like stickers).  The bus never fetches attachments
    unless the agent explicitly requests them via a tool.
    """
    type: MessageType
    url: str | None = None
    data: bytes | None = None
    filename: str | None = None
    mime_type: str | None = None
    size: int | None = None          # bytes, if known
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value}
        if self.url:
            d["url"] = self.url
        if self.filename:
            d["filename"] = self.filename
        if self.mime_type:
            d["mime_type"] = self.mime_type
        if self.size is not None:
            d["size"] = self.size
        if self.metadata:
            d["metadata"] = self.metadata
        return d


@dataclass
class ChannelMessage:
    """
    Normalized message exchanged between adapters, bus, and agent.

    Inbound:  adapter converts platform event → ChannelMessage → bus → agent
    Outbound: agent tool builds ChannelMessage → bus → adapter → platform

    Content uses a common markdown subset (bold, italic, code, links).
    Adapters are responsible for converting to/from platform-native formatting
    at the boundary.
    """

    # ── identity ──
    platform: str                     # "telegram", "discord", "whatsapp"
    channel_id: str                   # platform chat/channel/group ID
    user_id: str                      # sender on platform
    direction: Direction

    # ── content ──
    content: str = ""                 # text body (common markdown subset)
    message_type: MessageType = MessageType.TEXT
    attachments: list[Attachment] = field(default_factory=list)

    # ── context ──
    user_name: str = ""               # display name (best-effort)
    reply_to: str | None = None       # platform message ID this replies to
    thread_id: str | None = None      # thread/topic ID (Discord threads, Telegram topics)

    # ── metadata ──
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    platform_message_id: str | None = None  # original platform message ID
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    # Platform-specific extras live here:
    #   telegram: {"chat_type": "group", "bot_command": "/start"}
    #   discord:  {"guild_id": "...", "channel_name": "general"}
    #   whatsapp: {"phone": "+1234567890"}

    # ── routing (set by bus) ──
    agent_context_id: str | None = None  # A0 AgentContext ID for this conversation

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API responses, logging, and tool arguments."""
        return {
            "id": self.id,
            "platform": self.platform,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "direction": self.direction.value,
            "content": self.content,
            "message_type": self.message_type.value,
            "attachments": [a.to_dict() for a in self.attachments],
            "reply_to": self.reply_to,
            "thread_id": self.thread_id,
            "platform_message_id": self.platform_message_id,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "agent_context_id": self.agent_context_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelMessage:
        """Deserialize from dict (API input, tool args)."""
        attachments = [
            Attachment(
                type=MessageType(a["type"]),
                url=a.get("url"),
                filename=a.get("filename"),
                mime_type=a.get("mime_type"),
                size=a.get("size"),
                metadata=a.get("metadata", {}),
            )
            for a in data.get("attachments", [])
        ]
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            platform=data["platform"],
            channel_id=data["channel_id"],
            user_id=data["user_id"],
            user_name=data.get("user_name", ""),
            direction=Direction(data["direction"]),
            content=data.get("content", ""),
            message_type=MessageType(data.get("message_type", "text")),
            attachments=attachments,
            reply_to=data.get("reply_to"),
            thread_id=data.get("thread_id"),
            platform_message_id=data.get("platform_message_id"),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(timezone.utc),
            metadata=data.get("metadata", {}),
            agent_context_id=data.get("agent_context_id"),
        )

    @property
    def conversation_key(self) -> str:
        """
        Unique key for the conversation this message belongs to.
        Used by the bus to find/create the right AgentContext.
        Thread-aware: messages in a thread share a conversation.
        """
        base = f"{self.platform}:{self.channel_id}"
        if self.thread_id:
            return f"{base}:{self.thread_id}"
        return base
