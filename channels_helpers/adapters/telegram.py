"""
Telegram adapter for the channels plugin.

Uses aiogram v3 with long-polling mode. Runs inside the bus daemon's
asyncio event loop — dp.start_polling() is a long-lived coroutine that
receives updates and dispatches them through our handlers.

Config keys (from default_config.yaml):
    bot_token:      Telegram bot token from @BotFather
    allowed_chats:  Comma-separated chat IDs (empty = accept all)
    parse_mode:     "Markdown" | "MarkdownV2" | "HTML"
    commands:        JSON array of {command, description} for BotFather
    handle_commands_internally:  Comma-separated commands handled by plugin
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, Message as TgMessage

from channels_helpers.adapter import ChannelAdapter, InboundCallback
from channels_helpers.schema import (
    Attachment,
    ChannelMessage,
    Direction,
    MessageType,
)

logger = logging.getLogger("channels.telegram")

# Map config string → aiogram ParseMode enum
_PARSE_MODES = {
    "markdown": ParseMode.MARKDOWN,
    "markdownv2": ParseMode.MARKDOWN_V2,
    "html": ParseMode.HTML,
}


class TelegramAdapter(ChannelAdapter):
    """Telegram Bot API adapter using aiogram v3 long-polling."""

    def __init__(self, config: dict[str, Any], on_message: InboundCallback):
        super().__init__(config, on_message)

        self._bot_token: str = config["bot_token"]
        self._parse_mode_str: str = config.get("parse_mode", "Markdown").lower()
        self._parse_mode: ParseMode = _PARSE_MODES.get(
            self._parse_mode_str, ParseMode.MARKDOWN
        )

        # Parse allowed chat IDs (empty string = accept all)
        raw = config.get("allowed_chats", "")
        self._allowed_chats: set[int] = set()
        if raw and raw.strip():
            for chunk in str(raw).split(","):
                chunk = chunk.strip()
                if chunk.lstrip("-").isdigit():
                    self._allowed_chats.add(int(chunk))

        # Parse bot commands config
        self._commands: list[dict[str, str]] = []
        raw_cmds = config.get("commands", "[]")
        try:
            self._commands = json.loads(raw_cmds) if isinstance(raw_cmds, str) else raw_cmds
        except (json.JSONDecodeError, TypeError):
            self._commands = []

        # Commands handled by the plugin (not forwarded to agent)
        raw_internal = config.get("handle_commands_internally", "")
        self._internal_commands: set[str] = set()
        if raw_internal and raw_internal.strip():
            for cmd in str(raw_internal).split(","):
                cmd = cmd.strip().lstrip("/")
                if cmd:
                    self._internal_commands.add(cmd)

        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._polling_task: asyncio.Task | None = None
        self._bot_info: dict[str, Any] = {}

    # ── identity ──

    @property
    def platform(self) -> str:
        return "telegram"

    # ── lifecycle ──

    async def connect(self) -> None:
        """Start aiogram Bot + Dispatcher in long-polling mode."""
        if self._connected:
            return

        self._bot = Bot(
            token=self._bot_token,
            default=DefaultBotProperties(parse_mode=self._parse_mode),
        )

        # Verify token by fetching bot info
        me = await self._bot.me()
        self._bot_info = {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
        }
        logger.info(
            f"[telegram] authenticated as @{me.username} (id={me.id})"
        )

        # Set up dispatcher with handlers
        self._dp = Dispatcher()
        self._register_handlers()

        # Start polling in background task (non-blocking)
        self._polling_task = asyncio.ensure_future(
            self._dp.start_polling(self._bot, handle_signals=False)
        )
        self._connected = True

        logger.info("[telegram] long-polling started")

    async def on_connect(self) -> None:
        """Register bot commands with BotFather after successful connect."""
        if not self._bot or not self._commands:
            return
        try:
            bot_commands = [
                BotCommand(command=c["command"], description=c.get("description", ""))
                for c in self._commands
                if "command" in c
            ]
            if bot_commands:
                await self._bot.set_my_commands(bot_commands)
                logger.info(f"[telegram] registered {len(bot_commands)} bot commands")
        except Exception as e:
            logger.warning(f"[telegram] failed to register commands: {e}")

    async def disconnect(self) -> None:
        """Stop polling and close bot session."""
        if not self._connected:
            return

        self._connected = False

        if self._dp:
            # Signal dispatcher to stop
            await self._dp.stop_polling()

        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._bot:
            await self._bot.session.close()

        self._bot = None
        self._dp = None
        self._polling_task = None

        logger.info("[telegram] disconnected")

    # ── outbound messaging ──

    async def send(self, message: ChannelMessage) -> str | None:
        """Send a ChannelMessage to Telegram."""
        if not self._bot:
            logger.error("[telegram] bot not initialized")
            return None

        chat_id = message.channel_id
        content = self.format_outbound(message.content) if message.content else ""

        # Build reply parameters
        reply_kwargs: dict[str, Any] = {}
        if message.reply_to:
            try:
                reply_kwargs["reply_to_message_id"] = int(message.reply_to)
            except (ValueError, TypeError):
                pass

        if message.thread_id:
            try:
                reply_kwargs["message_thread_id"] = int(message.thread_id)
            except (ValueError, TypeError):
                pass

        try:
            # Send text message
            if message.message_type == MessageType.TEXT or not message.attachments:
                if content:
                    result = await self._bot.send_message(
                        chat_id=chat_id,
                        text=content,
                        **reply_kwargs,
                    )
                    return str(result.message_id)

            # Send with attachments
            for att in message.attachments:
                if att.type == MessageType.IMAGE and att.url:
                    result = await self._bot.send_photo(
                        chat_id=chat_id,
                        photo=att.url,
                        caption=content or None,
                        **reply_kwargs,
                    )
                    content = ""  # caption only on first attachment
                    return str(result.message_id)

                elif att.type in (MessageType.FILE, MessageType.VIDEO, MessageType.AUDIO) and att.url:
                    result = await self._bot.send_document(
                        chat_id=chat_id,
                        document=att.url,
                        caption=content or None,
                        **reply_kwargs,
                    )
                    content = ""
                    return str(result.message_id)

            # Fallback: if we had attachments but couldn't send them, send text
            if content:
                result = await self._bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    **reply_kwargs,
                )
                return str(result.message_id)

        except Exception as e:
            logger.error(f"[telegram] send failed: {type(e).__name__}: {e}")
            return None

        return None

    async def send_typing(self, chat_id: str) -> None:
        """Send 'typing...' chat action to indicate processing."""
        if self._bot:
            try:
                await self._bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logger.debug(f"[telegram] send_typing failed: {e}")

    async def send_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """React to a message with an emoji."""
        if self._bot:
            try:
                from aiogram.types import ReactionTypeEmoji
                await self._bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    reaction=[ReactionTypeEmoji(emoji=emoji)],
                )
            except Exception as e:
                logger.debug(f"[telegram] send_reaction failed: {e}")

    # ── format conversion ──

    def format_outbound(self, content: str) -> str:
        """
        Convert common markdown to Telegram-compatible format.

        Common markdown subset → Telegram Markdown (v1):
            **bold** → *bold*
            _italic_ stays _italic_
            `code` stays `code`
            ```block``` stays ```block```
            [text](url) stays [text](url)

        For MarkdownV2, we'd need to escape special chars — but v1 is simpler
        and works well for agent output.
        """
        if self._parse_mode == ParseMode.HTML:
            # Convert markdown to HTML
            content = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', content)
            content = re.sub(r'_(.+?)_', r'<i>\1</i>', content)
            content = re.sub(r'`{3}(\w*)\n(.*?)`{3}', r'<pre>\2</pre>', content, flags=re.DOTALL)
            content = re.sub(r'`(.+?)`', r'<code>\1</code>', content)
            return content

        if self._parse_mode == ParseMode.MARKDOWN:
            # **bold** → *bold* for Telegram Markdown v1
            content = re.sub(r'\*\*(.+?)\*\*', r'*\1*', content)
            return content

        # MarkdownV2: escape special characters outside formatting
        # This is complex — for now, fall back to stripping formatting
        if self._parse_mode == ParseMode.MARKDOWN_V2:
            content = re.sub(r'\*\*(.+?)\*\*', r'*\1*', content)
            # Escape MarkdownV2 special chars outside of formatting
            # This is a known pain point — keep it simple
            return content

        return content

    def normalize_inbound(self, content: str) -> str:
        """
        Convert Telegram formatting to common markdown.

        Telegram messages from users are typically plain text, so this is
        mostly a passthrough. Entity-based formatting would need the
        message entities array for full conversion.
        """
        return content

    # ── status ──

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["bot_info"] = self._bot_info
        if self._allowed_chats:
            base["allowed_chats"] = list(self._allowed_chats)
        return base

    # ── private: handler registration ──

    def _register_handlers(self) -> None:
        """Register aiogram message handlers on the dispatcher."""
        if not self._dp:
            return

        @self._dp.message()
        async def _handle_message(tg_msg: TgMessage) -> None:
            """Universal handler for all incoming messages."""
            await self._on_telegram_message(tg_msg)

    async def _on_telegram_message(self, tg_msg: TgMessage) -> None:
        """Convert a Telegram message to ChannelMessage and route inbound."""
        # Skip messages from the bot itself
        if tg_msg.from_user and self._bot_info.get("id") == tg_msg.from_user.id:
            return

        chat_id = str(tg_msg.chat.id)

        # Enforce allowed_chats filter
        if self._allowed_chats and tg_msg.chat.id not in self._allowed_chats:
            logger.debug(
                f"[telegram] ignoring message from non-allowed chat {chat_id}"
            )
            return

        # Handle internal commands (respond directly, don't forward to agent)
        if tg_msg.text and tg_msg.text.startswith("/"):
            cmd = tg_msg.text.split()[0].lstrip("/").split("@")[0]  # strip /cmd@botname
            if cmd in self._internal_commands:
                await self._handle_internal_command(cmd, tg_msg)
                return

        # Determine message type and extract content
        content = ""
        msg_type = MessageType.TEXT
        attachments: list[Attachment] = []

        if tg_msg.text:
            content = self.normalize_inbound(tg_msg.text)
            # Check if it's a bot command
            if tg_msg.text.startswith("/"):
                msg_type = MessageType.COMMAND

        elif tg_msg.caption:
            content = self.normalize_inbound(tg_msg.caption)

        # Photos
        if tg_msg.photo:
            msg_type = MessageType.IMAGE
            # Take the largest resolution (last in the array)
            photo = tg_msg.photo[-1]
            file_url = await self._get_file_url(photo.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.IMAGE,
                    url=file_url,
                    size=photo.file_size,
                    metadata={"width": photo.width, "height": photo.height},
                )
            )

        # Documents
        if tg_msg.document:
            msg_type = MessageType.FILE
            doc = tg_msg.document
            file_url = await self._get_file_url(doc.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.FILE,
                    url=file_url,
                    filename=doc.file_name,
                    mime_type=doc.mime_type,
                    size=doc.file_size,
                )
            )

        # Audio
        if tg_msg.audio:
            msg_type = MessageType.AUDIO
            audio = tg_msg.audio
            file_url = await self._get_file_url(audio.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.AUDIO,
                    url=file_url,
                    filename=audio.file_name,
                    mime_type=audio.mime_type,
                    size=audio.file_size,
                    metadata={"duration": audio.duration},
                )
            )

        # Video
        if tg_msg.video:
            msg_type = MessageType.VIDEO
            video = tg_msg.video
            file_url = await self._get_file_url(video.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.VIDEO,
                    url=file_url,
                    filename=video.file_name,
                    mime_type=video.mime_type,
                    size=video.file_size,
                    metadata={
                        "duration": video.duration,
                        "width": video.width,
                        "height": video.height,
                    },
                )
            )

        # Voice
        if tg_msg.voice:
            msg_type = MessageType.AUDIO
            voice = tg_msg.voice
            file_url = await self._get_file_url(voice.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.AUDIO,
                    url=file_url,
                    mime_type=voice.mime_type,
                    size=voice.file_size,
                    metadata={"duration": voice.duration},
                )
            )

        # Sticker
        if tg_msg.sticker:
            msg_type = MessageType.STICKER
            sticker = tg_msg.sticker
            file_url = await self._get_file_url(sticker.file_id)
            attachments.append(
                Attachment(
                    type=MessageType.STICKER,
                    url=file_url,
                    metadata={
                        "emoji": sticker.emoji,
                        "set_name": sticker.set_name,
                        "is_animated": sticker.is_animated,
                    },
                )
            )

        # Location
        if tg_msg.location:
            msg_type = MessageType.LOCATION
            loc = tg_msg.location
            content = f"Location: {loc.latitude}, {loc.longitude}"

        # Build user info
        user_id = str(tg_msg.from_user.id) if tg_msg.from_user else "unknown"
        user_name = ""
        if tg_msg.from_user:
            user_name = tg_msg.from_user.full_name or tg_msg.from_user.username or ""

        # Thread ID (for forum topics / message threads)
        thread_id = None
        if tg_msg.message_thread_id:
            thread_id = str(tg_msg.message_thread_id)

        # Reply-to tracking
        reply_to = None
        if tg_msg.reply_to_message:
            reply_to = str(tg_msg.reply_to_message.message_id)

        # Build ChannelMessage
        channel_msg = ChannelMessage(
            platform="telegram",
            channel_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            direction=Direction.INBOUND,
            content=content,
            message_type=msg_type,
            attachments=attachments,
            reply_to=reply_to,
            thread_id=thread_id,
            platform_message_id=str(tg_msg.message_id),
            metadata={
                "chat_type": tg_msg.chat.type,
                "chat_title": tg_msg.chat.title or tg_msg.chat.first_name or "",
            },
        )

        # Route through bus
        await self._on_message(channel_msg)

    async def _handle_internal_command(self, cmd: str, tg_msg: TgMessage) -> None:
        """Handle commands that the plugin responds to directly."""
        chat_id = tg_msg.chat.id
        if not self._bot:
            return

        try:
            if cmd == "help":
                lines = [f"*{self._bot_info.get('first_name', 'Agent Zero')}*"]
                if self._commands:
                    lines.append("")
                    for c in self._commands:
                        lines.append(f"/{c['command']} — {c.get('description', '')}")
                lines.append("\nSend any message to chat with the agent.")
                await self._bot.send_message(chat_id=chat_id, text="\n".join(lines),
                                             reply_to_message_id=tg_msg.message_id)

            elif cmd == "status":
                from channels_helpers.bus import get_bus
                bus = get_bus()
                if bus:
                    info = bus.status()
                    adapters = info.get("adapters", {})
                    connected = sum(1 for a in adapters.values() if a.get("connected"))
                    text = (
                        f"*Status*\n"
                        f"Bus: {'running' if info.get('running') else 'stopped'}\n"
                        f"Adapters: {connected}/{len(adapters)} connected\n"
                        f"Conversations: {info.get('active_conversations', 0)}"
                    )
                else:
                    text = "Bus not initialized"
                await self._bot.send_message(chat_id=chat_id, text=text,
                                             reply_to_message_id=tg_msg.message_id)

            elif cmd == "forget":
                from channels_helpers.bus import get_bus
                bus = get_bus()
                conv_key = f"telegram:{chat_id}"
                if bus:
                    bus.clear_conversation(conv_key)
                await self._bot.send_message(chat_id=chat_id, text="Conversation cleared.",
                                             reply_to_message_id=tg_msg.message_id)

            else:
                await self._bot.send_message(chat_id=chat_id, text=f"Unknown command: /{cmd}",
                                             reply_to_message_id=tg_msg.message_id)

        except Exception as e:
            logger.error(f"[telegram] internal command /{cmd} failed: {e}")

    async def _get_file_url(self, file_id: str) -> str | None:
        """Get a temporary download URL for a Telegram file."""
        if not self._bot:
            return None
        try:
            file = await self._bot.get_file(file_id)
            if file.file_path:
                return f"https://api.telegram.org/file/bot{self._bot_token}/{file.file_path}"
        except Exception as e:
            logger.warning(f"[telegram] get_file failed: {e}")
        return None
