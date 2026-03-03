"""
WhatsApp adapter for the channels plugin.

Uses the WhatsApp Business Cloud API:
  - Inbound: aiohttp webhook server receives POST notifications from Meta
  - Outbound: HTTP POST to graph.facebook.com/v21.0/{phone_id}/messages

Config keys (from default_config.yaml):
    phone_id:       WhatsApp Business Phone Number ID
    access_token:   Meta Graph API access token
    verify_token:   Webhook verification token (you choose this)
    webhook_port:   Local port for the webhook server (default 8402)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiohttp import web

from channels_helpers.adapter import ChannelAdapter, InboundCallback
from channels_helpers.schema import (
    Attachment,
    ChannelMessage,
    Direction,
    MessageType,
)

logger = logging.getLogger("channels.whatsapp")

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp Business Cloud API adapter."""

    def __init__(self, config: dict[str, Any], on_message: InboundCallback):
        super().__init__(config, on_message)

        self._phone_id: str = config.get("phone_id", "")
        self._access_token: str = config["access_token"]
        self._verify_token: str = config.get("verify_token", "")
        self._webhook_port: int = int(config.get("webhook_port", 8402))

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._bot_info: dict[str, Any] = {}

    # ── identity ──

    @property
    def platform(self) -> str:
        return "whatsapp"

    # ── lifecycle ──

    async def connect(self) -> None:
        """Start the webhook server and create HTTP session for outbound."""
        if self._connected:
            return

        # Create aiohttp client session for outbound API calls
        self._http_session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            }
        )

        # Verify credentials by fetching phone number info
        try:
            async with self._http_session.get(
                f"{GRAPH_API_BASE}/{self._phone_id}",
                params={"fields": "display_phone_number,verified_name"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._bot_info = {
                        "phone_id": self._phone_id,
                        "phone_number": data.get("display_phone_number", ""),
                        "verified_name": data.get("verified_name", ""),
                    }
                    logger.info(
                        f"[whatsapp] authenticated: {self._bot_info.get('verified_name')} "
                        f"({self._bot_info.get('phone_number')})"
                    )
                else:
                    body = await resp.text()
                    logger.warning(
                        f"[whatsapp] phone verification returned {resp.status}: {body}"
                    )
                    # Don't fail — token might still work for messaging
                    self._bot_info = {"phone_id": self._phone_id}
        except Exception as e:
            logger.warning(f"[whatsapp] credential check failed: {e}")
            self._bot_info = {"phone_id": self._phone_id}

        # Start webhook server
        self._app = web.Application()
        self._app.router.add_get("/webhook", self._handle_verify)
        self._app.router.add_post("/webhook", self._handle_webhook)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._webhook_port)
        await self._site.start()

        self._connected = True
        logger.info(f"[whatsapp] webhook server started on port {self._webhook_port}")

    async def disconnect(self) -> None:
        """Stop the webhook server and close HTTP session."""
        if not self._connected:
            return

        self._connected = False

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

        self._app = None
        self._runner = None
        self._site = None
        self._http_session = None

        logger.info("[whatsapp] disconnected")

    # ── outbound messaging ──

    async def send(self, message: ChannelMessage) -> str | None:
        """Send a ChannelMessage via WhatsApp Cloud API."""
        if not self._http_session:
            logger.error("[whatsapp] HTTP session not initialized")
            return None

        recipient = message.channel_id  # phone number or wa_id
        content = self.format_outbound(message.content) if message.content else ""

        url = f"{GRAPH_API_BASE}/{self._phone_id}/messages"

        try:
            # Build message payload
            if message.attachments:
                for att in message.attachments:
                    if att.type == MessageType.IMAGE and att.url:
                        payload = {
                            "messaging_product": "whatsapp",
                            "to": recipient,
                            "type": "image",
                            "image": {
                                "link": att.url,
                                "caption": content or None,
                            },
                        }
                        return await self._send_api(url, payload)

                    elif att.type in (MessageType.FILE, MessageType.AUDIO, MessageType.VIDEO) and att.url:
                        wa_type = "document"
                        if att.type == MessageType.AUDIO:
                            wa_type = "audio"
                        elif att.type == MessageType.VIDEO:
                            wa_type = "video"

                        media_obj: dict[str, Any] = {"link": att.url}
                        if content and wa_type == "document":
                            media_obj["caption"] = content

                        payload = {
                            "messaging_product": "whatsapp",
                            "to": recipient,
                            "type": wa_type,
                            wa_type: media_obj,
                        }
                        return await self._send_api(url, payload)

            # Text message (default)
            if content:
                # Add reply context if replying
                payload: dict[str, Any] = {
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"body": content},
                }

                if message.reply_to:
                    payload["context"] = {"message_id": message.reply_to}

                return await self._send_api(url, payload)

        except Exception as e:
            logger.error(f"[whatsapp] send failed: {type(e).__name__}: {e}")
            return None

        return None

    async def _send_api(self, url: str, payload: dict[str, Any]) -> str | None:
        """Send a payload to the WhatsApp Cloud API and return message ID."""
        if not self._http_session:
            return None

        async with self._http_session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status in (200, 201):
                messages = data.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id", "")
                    logger.debug(f"[whatsapp] sent message: {msg_id}")
                    return msg_id
            else:
                error = data.get("error", {})
                logger.error(
                    f"[whatsapp] API error {resp.status}: "
                    f"{error.get('message', data)}"
                )
        return None

    # ── format conversion ──

    def format_outbound(self, content: str) -> str:
        """
        Convert common markdown to WhatsApp format.

        WhatsApp uses its own formatting:
            **bold** → *bold*
            _italic_ → _italic_ (same)
            `code` → ```code``` (WhatsApp uses triple backticks for mono)
            ~~strike~~ → ~strike~ (single tilde)
        """
        import re
        # **bold** → *bold*
        content = re.sub(r'\*\*(.+?)\*\*', r'*\1*', content)
        # `inline code` → ```inline code``` (WhatsApp mono)
        content = re.sub(r'(?<!`)(`(?!`))(.+?)(`(?!`))', r'```\2```', content)
        # ~~strike~~ → ~strike~
        content = re.sub(r'~~(.+?)~~', r'~\1~', content)
        return content

    def normalize_inbound(self, content: str) -> str:
        """
        Convert WhatsApp formatting to common markdown.

        *bold* → **bold**
        _italic_ stays
        ```code``` → `code`
        ~strike~ → ~~strike~~
        """
        import re
        # *bold* → **bold** (careful not to match inside **)
        content = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'**\1**', content)
        # ```code``` → `code`
        content = re.sub(r'```(.+?)```', r'`\1`', content)
        # ~strike~ → ~~strike~~
        content = re.sub(r'(?<!~)~(?!~)(.+?)(?<!~)~(?!~)', r'~~\1~~', content)
        return content

    # ── status ──

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["bot_info"] = self._bot_info
        base["webhook_port"] = self._webhook_port
        return base

    # ── private: webhook handlers ──

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """Handle webhook verification GET request from Meta."""
        mode = request.query.get("hub.mode")
        token = request.query.get("hub.verify_token")
        challenge = request.query.get("hub.challenge")

        if mode == "subscribe" and token == self._verify_token:
            logger.info("[whatsapp] webhook verified")
            return web.Response(text=challenge or "")

        logger.warning("[whatsapp] webhook verification failed")
        return web.Response(status=403, text="Forbidden")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming message POST notifications from Meta."""
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        # Always respond 200 quickly to avoid webhook retries
        # Process messages in background
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if change.get("field") == "messages":
                        await self._process_webhook_value(value)

        return web.Response(text="OK")

    async def _process_webhook_value(self, value: dict[str, Any]) -> None:
        """Process a single webhook notification value."""
        contacts = {
            c.get("wa_id", ""): c.get("profile", {}).get("name", "")
            for c in value.get("contacts", [])
        }

        for msg in value.get("messages", []):
            try:
                await self._on_whatsapp_message(msg, contacts)
            except Exception as e:
                logger.error(f"[whatsapp] error processing message: {e}")

    async def _on_whatsapp_message(
        self, msg: dict[str, Any], contacts: dict[str, str]
    ) -> None:
        """Convert a WhatsApp webhook message to ChannelMessage and route."""
        from_id = msg.get("from", "")
        msg_id = msg.get("id", "")
        msg_type_str = msg.get("type", "text")
        timestamp_str = msg.get("timestamp", "")

        # Parse timestamp
        try:
            ts = datetime.fromtimestamp(int(timestamp_str), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            ts = datetime.now(timezone.utc)

        # Extract content and type
        content = ""
        msg_type = MessageType.TEXT
        attachments: list[Attachment] = []

        if msg_type_str == "text":
            content = msg.get("text", {}).get("body", "")

        elif msg_type_str == "image":
            msg_type = MessageType.IMAGE
            image = msg.get("image", {})
            content = image.get("caption", "")
            attachments.append(
                Attachment(
                    type=MessageType.IMAGE,
                    mime_type=image.get("mime_type"),
                    metadata={"media_id": image.get("id")},
                )
            )

        elif msg_type_str == "document":
            msg_type = MessageType.FILE
            doc = msg.get("document", {})
            content = doc.get("caption", "")
            attachments.append(
                Attachment(
                    type=MessageType.FILE,
                    filename=doc.get("filename"),
                    mime_type=doc.get("mime_type"),
                    metadata={"media_id": doc.get("id")},
                )
            )

        elif msg_type_str == "audio":
            msg_type = MessageType.AUDIO
            audio = msg.get("audio", {})
            attachments.append(
                Attachment(
                    type=MessageType.AUDIO,
                    mime_type=audio.get("mime_type"),
                    metadata={"media_id": audio.get("id"), "voice": audio.get("voice", False)},
                )
            )

        elif msg_type_str == "video":
            msg_type = MessageType.VIDEO
            video = msg.get("video", {})
            content = video.get("caption", "")
            attachments.append(
                Attachment(
                    type=MessageType.VIDEO,
                    mime_type=video.get("mime_type"),
                    metadata={"media_id": video.get("id")},
                )
            )

        elif msg_type_str == "sticker":
            msg_type = MessageType.STICKER
            sticker = msg.get("sticker", {})
            attachments.append(
                Attachment(
                    type=MessageType.STICKER,
                    mime_type=sticker.get("mime_type"),
                    metadata={
                        "media_id": sticker.get("id"),
                        "animated": sticker.get("animated", False),
                    },
                )
            )

        elif msg_type_str == "location":
            msg_type = MessageType.LOCATION
            loc = msg.get("location", {})
            content = f"Location: {loc.get('latitude')}, {loc.get('longitude')}"
            if loc.get("name"):
                content += f" ({loc['name']})"

        elif msg_type_str == "contacts":
            # Contact cards — serialize as text
            for contact in msg.get("contacts", []):
                name = contact.get("name", {}).get("formatted_name", "Unknown")
                phones = [p.get("phone", "") for p in contact.get("phones", [])]
                content += f"Contact: {name} — {', '.join(phones)}\n"

        elif msg_type_str == "reaction":
            # Reactions — skip for now (not a user message)
            return

        else:
            content = f"[Unsupported message type: {msg_type_str}]"

        # Reply context
        reply_to = None
        context = msg.get("context", {})
        if context.get("id"):
            reply_to = context["id"]

        # User info
        user_name = contacts.get(from_id, "")

        # Build ChannelMessage
        channel_msg = ChannelMessage(
            platform="whatsapp",
            channel_id=from_id,  # WhatsApp uses phone number as channel
            user_id=from_id,
            user_name=user_name,
            direction=Direction.INBOUND,
            content=content,
            message_type=msg_type,
            attachments=attachments,
            reply_to=reply_to,
            platform_message_id=msg_id,
            timestamp=ts,
            metadata={
                "phone": from_id,
                "wa_msg_type": msg_type_str,
            },
        )

        # Route through bus
        await self._on_message(channel_msg)
