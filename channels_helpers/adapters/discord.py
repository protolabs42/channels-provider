"""
Discord adapter for the channels plugin.

Uses discord.py with gateway connection via client.start() (async, non-blocking).
Runs inside the bus daemon's asyncio event loop.

Config keys (from default_config.yaml):
    bot_token:         Discord bot token from Discord Developer Portal
    allowed_guilds:    Comma-separated guild IDs (empty = accept all)
    allowed_channels:  Comma-separated channel IDs (empty = accept all)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord import Intents

from channels_helpers.adapter import ChannelAdapter, InboundCallback
from channels_helpers.schema import (
    Attachment,
    ChannelMessage,
    Direction,
    MessageType,
)

logger = logging.getLogger("channels.discord")


class DiscordAdapter(ChannelAdapter):
    """Discord gateway adapter using discord.py."""

    def __init__(self, config: dict[str, Any], on_message: InboundCallback):
        super().__init__(config, on_message)

        self._bot_token: str = config["bot_token"]

        # Parse allowed guild IDs
        raw_guilds = config.get("allowed_guilds", "")
        self._allowed_guilds: set[int] = set()
        if raw_guilds and raw_guilds.strip():
            for chunk in str(raw_guilds).split(","):
                chunk = chunk.strip()
                if chunk.isdigit():
                    self._allowed_guilds.add(int(chunk))

        # Parse allowed channel IDs
        raw_channels = config.get("allowed_channels", "")
        self._allowed_channels: set[int] = set()
        if raw_channels and raw_channels.strip():
            for chunk in str(raw_channels).split(","):
                chunk = chunk.strip()
                if chunk.isdigit():
                    self._allowed_channels.add(int(chunk))

        self._client: discord.Client | None = None
        self._gateway_task: asyncio.Task | None = None
        self._bot_info: dict[str, Any] = {}
        self._ready_event = asyncio.Event()

    # ── identity ──

    @property
    def platform(self) -> str:
        return "discord"

    # ── lifecycle ──

    async def connect(self) -> None:
        """Start the Discord gateway connection."""
        if self._connected:
            return

        intents = Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = False  # don't need member list

        self._client = discord.Client(intents=intents)
        self._register_handlers()

        # Start gateway in background task (non-blocking)
        self._gateway_task = asyncio.ensure_future(
            self._client.start(self._bot_token)
        )

        # Wait for the on_ready event (with timeout)
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.error("[discord] timed out waiting for gateway ready")
            raise ConnectionError("Discord gateway timeout")

        self._connected = True
        logger.info("[discord] gateway connected")

    async def disconnect(self) -> None:
        """Close the Discord gateway."""
        if not self._connected:
            return

        self._connected = False

        if self._client and not self._client.is_closed():
            await self._client.close()

        if self._gateway_task and not self._gateway_task.done():
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except (asyncio.CancelledError, Exception):
                pass

        self._client = None
        self._gateway_task = None
        self._ready_event.clear()

        logger.info("[discord] disconnected")

    # ── outbound messaging ──

    async def send(self, message: ChannelMessage) -> str | None:
        """Send a ChannelMessage to a Discord channel."""
        if not self._client:
            logger.error("[discord] client not initialized")
            return None

        try:
            channel_id = int(message.channel_id)
        except (ValueError, TypeError):
            logger.error(f"[discord] invalid channel_id: {message.channel_id}")
            return None

        channel = self._client.get_channel(channel_id)
        if channel is None:
            # Try fetching if not in cache
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception as e:
                logger.error(f"[discord] channel {channel_id} not found: {e}")
                return None

        if not hasattr(channel, "send"):
            logger.error(f"[discord] channel {channel_id} is not a text channel")
            return None

        content = self.format_outbound(message.content) if message.content else ""

        # Build send kwargs
        send_kwargs: dict[str, Any] = {}

        # Reply reference
        if message.reply_to:
            try:
                ref_id = int(message.reply_to)
                send_kwargs["reference"] = discord.MessageReference(
                    message_id=ref_id,
                    channel_id=channel_id,
                    fail_if_not_exists=False,
                )
            except (ValueError, TypeError):
                pass

        try:
            # Handle attachments
            files = []
            for att in message.attachments:
                if att.url:
                    # Discord can't send by URL in message.files — we'd need to
                    # download first. For now, append URL to content.
                    content += f"\n{att.url}"
                elif att.data and att.filename:
                    import io
                    files.append(
                        discord.File(io.BytesIO(att.data), filename=att.filename)
                    )

            # Discord has a 2000-char limit
            if len(content) > 2000:
                # Split into multiple messages
                chunks = self._split_message(content, 2000)
                last_msg = None
                for i, chunk in enumerate(chunks):
                    kwargs = dict(send_kwargs) if i == 0 else {}
                    if i == len(chunks) - 1 and files:
                        kwargs["files"] = files
                    last_msg = await channel.send(chunk, **kwargs)
                return str(last_msg.id) if last_msg else None

            if content or files:
                result = await channel.send(
                    content=content or None,
                    files=files or None,
                    **send_kwargs,
                )
                return str(result.id)

        except Exception as e:
            logger.error(f"[discord] send failed: {type(e).__name__}: {e}")
            return None

        return None

    # ── format conversion ──

    def format_outbound(self, content: str) -> str:
        """
        Convert common markdown to Discord markdown.

        Discord uses a markdown dialect very similar to our common subset:
            **bold** → **bold** (same)
            _italic_ → *italic* (Discord prefers * for italic)
            `code` → `code` (same)
            ```block``` → ```block``` (same)
            [text](url) → [text](url) (Discord auto-links in embeds but
                          masked links work in regular messages)

        Minimal conversion needed — Discord markdown is close to standard.
        """
        # Discord uses *italic* not _italic_
        # But _italic_ also works in Discord, so mostly passthrough
        return content

    def normalize_inbound(self, content: str) -> str:
        """
        Convert Discord markdown to common markdown.

        Discord markdown is very close to standard, so mostly passthrough.
        Convert *italic* → _italic_ for consistency with common format.
        """
        return content

    # ── status ──

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["bot_info"] = self._bot_info
        if self._allowed_guilds:
            base["allowed_guilds"] = list(self._allowed_guilds)
        if self._allowed_channels:
            base["allowed_channels"] = list(self._allowed_channels)
        if self._client:
            base["guild_count"] = len(self._client.guilds)
        return base

    # ── private: handler registration ──

    def _register_handlers(self) -> None:
        """Register discord.py event handlers on the client."""
        if not self._client:
            return

        @self._client.event
        async def on_ready():
            if self._client and self._client.user:
                self._bot_info = {
                    "id": self._client.user.id,
                    "username": self._client.user.name,
                    "discriminator": getattr(self._client.user, "discriminator", "0"),
                }
                logger.info(
                    f"[discord] authenticated as {self._client.user.name} "
                    f"(id={self._client.user.id}), "
                    f"{len(self._client.guilds)} guilds"
                )
            self._ready_event.set()

        @self._client.event
        async def on_message(dc_msg: discord.Message):
            await self._on_discord_message(dc_msg)

    async def _on_discord_message(self, dc_msg: discord.Message) -> None:
        """Convert a Discord message to ChannelMessage and route inbound."""
        # Skip bot's own messages
        if self._client and dc_msg.author == self._client.user:
            return

        # Skip messages from other bots
        if dc_msg.author.bot:
            return

        # Enforce allowed_guilds filter
        if dc_msg.guild and self._allowed_guilds:
            if dc_msg.guild.id not in self._allowed_guilds:
                logger.debug(
                    f"[discord] ignoring message from non-allowed guild {dc_msg.guild.id}"
                )
                return

        # Enforce allowed_channels filter
        if self._allowed_channels:
            if dc_msg.channel.id not in self._allowed_channels:
                logger.debug(
                    f"[discord] ignoring message from non-allowed channel {dc_msg.channel.id}"
                )
                return

        # Extract content
        content = self.normalize_inbound(dc_msg.content) if dc_msg.content else ""
        msg_type = MessageType.TEXT

        # Process attachments
        attachments: list[Attachment] = []
        for att in dc_msg.attachments:
            att_type = self._classify_attachment(att.content_type, att.filename)
            if att_type == MessageType.IMAGE:
                msg_type = MessageType.IMAGE
            attachments.append(
                Attachment(
                    type=att_type,
                    url=att.url,
                    filename=att.filename,
                    mime_type=att.content_type,
                    size=att.size,
                    metadata={
                        "width": att.width,
                        "height": att.height,
                        "proxy_url": att.proxy_url,
                    },
                )
            )

        # User info
        user_id = str(dc_msg.author.id)
        user_name = dc_msg.author.display_name or dc_msg.author.name

        # Thread ID (for Discord threads)
        thread_id = None
        if isinstance(dc_msg.channel, discord.Thread):
            thread_id = str(dc_msg.channel.id)

        # Reply-to tracking
        reply_to = None
        if dc_msg.reference and dc_msg.reference.message_id:
            reply_to = str(dc_msg.reference.message_id)

        # Build metadata
        metadata: dict[str, Any] = {}
        if dc_msg.guild:
            metadata["guild_id"] = str(dc_msg.guild.id)
            metadata["guild_name"] = dc_msg.guild.name
        metadata["channel_name"] = getattr(dc_msg.channel, "name", "DM")
        metadata["channel_type"] = str(dc_msg.channel.type)

        # Channel ID: use the channel (or thread parent for thread context)
        channel_id = str(dc_msg.channel.id)

        # Build ChannelMessage
        channel_msg = ChannelMessage(
            platform="discord",
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            direction=Direction.INBOUND,
            content=content,
            message_type=msg_type,
            attachments=attachments,
            reply_to=reply_to,
            thread_id=thread_id,
            platform_message_id=str(dc_msg.id),
            metadata=metadata,
        )

        # Route through bus
        await self._on_message(channel_msg)

    @staticmethod
    def _classify_attachment(content_type: str | None, filename: str | None) -> MessageType:
        """Classify a Discord attachment by MIME type or filename."""
        if content_type:
            ct = content_type.lower()
            if ct.startswith("image/"):
                return MessageType.IMAGE
            if ct.startswith("video/"):
                return MessageType.VIDEO
            if ct.startswith("audio/"):
                return MessageType.AUDIO
        if filename:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                return MessageType.IMAGE
            if ext in ("mp4", "webm", "mov", "avi"):
                return MessageType.VIDEO
            if ext in ("mp3", "ogg", "wav", "flac", "m4a"):
                return MessageType.AUDIO
        return MessageType.FILE

    @staticmethod
    def _split_message(content: str, max_len: int = 2000) -> list[str]:
        """Split a long message into chunks respecting Discord's limit."""
        if len(content) <= max_len:
            return [content]

        chunks = []
        while content:
            if len(content) <= max_len:
                chunks.append(content)
                break

            # Try to split at a newline
            split_at = content.rfind("\n", 0, max_len)
            if split_at == -1 or split_at < max_len // 2:
                # No good newline — split at space
                split_at = content.rfind(" ", 0, max_len)
            if split_at == -1 or split_at < max_len // 2:
                # No good space — hard split
                split_at = max_len

            chunks.append(content[:split_at])
            content = content[split_at:].lstrip()

        return chunks
