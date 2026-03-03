"""
ChannelBus — routes messages between platform adapters and Agent Zero contexts.

Architecture:
    Platform → Adapter → on_message callback → Bus.route_inbound()
        → create AgentContext(BACKGROUND) → context.communicate()
        → agent processes → response auto-dispatched back to platform

Threading model:
    - Adapters run in their own threads/event loops (Telegram bot, Discord gateway, etc.)
    - Inbound messages are processed via fire-and-forget background threads
    - Each inbound message gets its own AgentContext (new context per message)
    - The agent's final response is auto-dispatched back through the originating adapter
    - Outbound dispatch from agent tools uses a thread-safe queue consumed by
      the adapter's event loop (via dispatch_outbound_threadsafe)

    The send_message tool is for proactive messaging (agent → different channel).
    Normal replies flow automatically: inbound → agent → auto-reply to same channel.
"""

from __future__ import annotations

import asyncio
import logging
import queue as stdlib_queue
import threading
from typing import Any

from channels_helpers.schema import ChannelMessage, Direction, MessageType
from channels_helpers.adapter import ChannelAdapter

logger = logging.getLogger("channels")

# Conversations where the agent explicitly called send_message during processing.
# Prevents double-reply (auto-reply + explicit send) for the same inbound message.
_explicit_sends: set[str] = set()
_explicit_sends_lock = threading.Lock()


def mark_explicit_send(conversation_key: str) -> None:
    """Called by send_message tool to prevent auto-reply."""
    with _explicit_sends_lock:
        _explicit_sends.add(conversation_key)


def check_and_clear_explicit_send(conversation_key: str) -> bool:
    """Check if explicit send occurred, then clear the flag."""
    with _explicit_sends_lock:
        if conversation_key in _explicit_sends:
            _explicit_sends.discard(conversation_key)
            return True
        return False


class ChannelBus:
    """
    Central message router for the channels plugin.

    Singleton — accessed via get_bus() / ensure_bus().
    """

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_lock = threading.Lock()

        # conversation_key → last context ID (informational, not reused)
        self._context_map: dict[str, str] = {}
        self._context_lock = threading.Lock()

        # recent messages per conversation (bounded ring buffer for tool access)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._history_lock = threading.Lock()
        self._history_max = int(config.get("history_per_conversation", 50))

        # thread-safe outbound queue for cross-thread dispatch
        # (agent DeferredTask thread → adapter event loop)
        self._outbound_queue: stdlib_queue.Queue[ChannelMessage | None] = stdlib_queue.Queue()

        # per-conversation processing lock prevents parallel agent runs
        self._processing: dict[str, threading.Lock] = {}
        self._processing_meta_lock = threading.Lock()

        self._running = False

    # ── adapter management ──

    def register(self, adapter: ChannelAdapter) -> None:
        """Register a platform adapter. Replaces existing adapter for same platform."""
        with self._adapter_lock:
            old = self._adapters.get(adapter.platform)
            self._adapters[adapter.platform] = adapter
        if old and old.connected:
            logger.info(f"Replacing existing {adapter.platform} adapter")

    def unregister(self, platform: str) -> ChannelAdapter | None:
        """Remove and return adapter. Caller is responsible for disconnect."""
        with self._adapter_lock:
            return self._adapters.pop(platform, None)

    def get_adapter(self, platform: str) -> ChannelAdapter | None:
        with self._adapter_lock:
            return self._adapters.get(platform)

    @property
    def adapters(self) -> dict[str, ChannelAdapter]:
        with self._adapter_lock:
            return dict(self._adapters)

    # ── inbound routing ──

    async def route_inbound(self, message: ChannelMessage) -> None:
        """
        Route an inbound message from a platform adapter to the agent.

        Fires processing in a background thread so the adapter isn't blocked.
        Each conversation is serialized (one message at a time) via a per-conversation lock.
        """
        message.direction = Direction.INBOUND
        conv_key = message.conversation_key

        self._record_history(conv_key, message)

        logger.info(
            f"[inbound] {message.platform}:{message.channel_id} "
            f"user={message.user_name or message.user_id}"
        )

        # fire and forget — process in background thread
        thread = threading.Thread(
            target=self._process_inbound_sync,
            args=(message,),
            daemon=True,
            name=f"channels-{conv_key}-{message.id}",
        )
        thread.start()

    # ── outbound dispatch ──

    async def dispatch_outbound(self, message: ChannelMessage) -> str | None:
        """
        Send an outbound message from the agent to a platform.

        Must be called from the adapter's event loop (or any async context
        where the adapter's send() can execute).
        """
        message.direction = Direction.OUTBOUND
        conv_key = message.conversation_key

        adapter = self.get_adapter(message.platform)
        if not adapter:
            logger.error(f"No adapter registered for platform: {message.platform}")
            return None

        if not adapter.connected:
            logger.error(f"Adapter {message.platform} is not connected")
            return None

        self._record_history(conv_key, message)

        logger.info(f"[outbound] → {message.platform}:{message.channel_id}")

        try:
            platform_msg_id = await adapter.send(message)
            if platform_msg_id:
                message.platform_message_id = platform_msg_id
            return platform_msg_id
        except Exception as e:
            logger.error(f"[outbound] send failed: {type(e).__name__}: {e}")
            return None

    def dispatch_outbound_threadsafe(self, message: ChannelMessage) -> None:
        """
        Queue an outbound message from any thread (e.g., agent DeferredTask).

        The message is placed on a thread-safe queue. Call drain_outbound()
        from the adapter's event loop to actually send.
        """
        message.direction = Direction.OUTBOUND
        self._outbound_queue.put(message)

    async def drain_outbound(self) -> int:
        """
        Process all queued outbound messages. Call from adapter event loop.

        Returns number of messages dispatched.
        """
        count = 0
        while True:
            try:
                message = self._outbound_queue.get_nowait()
                if message is None:
                    break
                await self.dispatch_outbound(message)
                count += 1
            except stdlib_queue.Empty:
                break
        return count

    # ── history access (for tools) ──

    def get_history(self, conversation_key: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent messages for a conversation. Used by channel_status tool."""
        with self._history_lock:
            history = self._history.get(conversation_key, [])
            return history[-limit:]

    def list_conversations(self) -> list[dict[str, Any]]:
        """List active conversations with metadata. Used by dashboard."""
        with self._context_lock:
            conversations = []
            for conv_key, context_id in self._context_map.items():
                parts = conv_key.split(":", 2)
                platform = parts[0] if len(parts) > 0 else "unknown"
                channel_id = parts[1] if len(parts) > 1 else ""
                thread_id = parts[2] if len(parts) > 2 else None

                with self._history_lock:
                    msg_count = len(self._history.get(conv_key, []))

                conversations.append({
                    "conversation_key": conv_key,
                    "platform": platform,
                    "channel_id": channel_id,
                    "thread_id": thread_id,
                    "agent_context_id": context_id,
                    "message_count": msg_count,
                })
            return conversations

    # ── context mapping ──

    def get_context_id(self, conversation_key: str) -> str | None:
        """Look up the last AgentContext ID for a conversation."""
        with self._context_lock:
            return self._context_map.get(conversation_key)

    def set_context_id(self, conversation_key: str, context_id: str) -> None:
        """Bind a conversation to an AgentContext."""
        with self._context_lock:
            self._context_map[conversation_key] = context_id

    # ── conversation management ──

    def clear_conversation(self, conversation_key: str) -> bool:
        """Clear history and context for a conversation. Returns True if found."""
        found = False
        with self._history_lock:
            if conversation_key in self._history:
                del self._history[conversation_key]
                found = True
        with self._context_lock:
            if conversation_key in self._context_map:
                del self._context_map[conversation_key]
                found = True
        return found

    # ── lifecycle ──

    async def start(self) -> None:
        """Connect all configured adapters."""
        with self._adapter_lock:
            adapters = list(self._adapters.values())

        for adapter in adapters:
            if not adapter.connected:
                try:
                    await adapter.connect()
                    await adapter.on_connect()
                    logger.info(f"Connected: {adapter.platform}")
                except Exception as e:
                    logger.error(f"Failed to connect {adapter.platform}: {e}")

        self._running = True
        logger.info("Channel bus started")

    async def stop(self) -> None:
        """Disconnect all adapters gracefully."""
        self._running = False

        with self._adapter_lock:
            adapters = list(self._adapters.values())

        for adapter in adapters:
            if adapter.connected:
                try:
                    await adapter.on_disconnect()
                    await adapter.disconnect()
                    logger.info(f"Disconnected: {adapter.platform}")
                except Exception as e:
                    logger.error(f"Error disconnecting {adapter.platform}: {e}")

        logger.info("Channel bus stopped")

    # ── status ──

    def status(self) -> dict[str, Any]:
        """Full bus status for dashboard API."""
        with self._adapter_lock:
            adapter_statuses = {
                name: adapter.status()
                for name, adapter in self._adapters.items()
            }
        with self._context_lock:
            active_conversations = len(self._context_map)

        return {
            "running": self._running,
            "adapters": adapter_statuses,
            "active_conversations": active_conversations,
            "outbound_queue_size": self._outbound_queue.qsize(),
        }

    # ── private: inbound processing ──

    def _get_processing_lock(self, conv_key: str) -> threading.Lock:
        """Get or create a per-conversation processing lock."""
        with self._processing_meta_lock:
            if conv_key not in self._processing:
                self._processing[conv_key] = threading.Lock()
            return self._processing[conv_key]

    def _process_inbound_sync(self, message: ChannelMessage) -> None:
        """
        Process an inbound message synchronously in a background thread.

        Creates a new AgentContext(BACKGROUND), sends the message via
        context.communicate(), awaits the result, and auto-dispatches
        the response back to the originating channel.

        Reaction lifecycle: seen → processing → done/error
        Per-conversation lock ensures messages are processed in order.
        """
        conv_key = message.conversation_key
        lock = self._get_processing_lock(conv_key)

        if not lock.acquire(timeout=30):
            logger.warning(f"[process] timed out waiting for lock: {conv_key}")
            return

        context = None
        typing_stop = threading.Event()

        # Read platform-specific config before try block (available in except handlers)
        platform = message.platform
        reaction_on = self._config.get(f"{platform}_reaction_enabled", True)
        r_seen = self._config.get(f"{platform}_reaction_seen", "\U0001F440")
        r_processing = self._config.get(f"{platform}_reaction_processing", "\U0001F914")
        r_done = self._config.get(f"{platform}_reaction_done", "\U0001F44D")
        r_error = self._config.get(f"{platform}_reaction_error", "\U0001F494")
        typing_on = self._config.get(f"{platform}_typing_enabled", True)

        try:
            # Late imports — only available inside the A0 container
            from agent import AgentContext, AgentContextType, UserMessage
            from initialize import initialize_agent

            # Create a fresh BACKGROUND context for this message
            cfg = initialize_agent()
            context = AgentContext(cfg, type=AgentContextType.BACKGROUND)
            self.set_context_id(conv_key, context.id)
            message.agent_context_id = context.id

            logger.info(
                f"[process] {conv_key} → context {context.id} "
                f"msg={message.id}"
            )

            # Reaction: seen (👀)
            if reaction_on:
                self._send_reaction(message, r_seen)

            # Start typing loop
            if typing_on:
                typing_stop = self._start_typing_loop(message)

            # Reaction: processing (⏳) — replaces seen
            if reaction_on:
                self._send_reaction(message, r_processing)

            # Format the inbound message with channel context
            user_text = self._format_inbound_message(message)

            # Send to agent and wait for response (blocking)
            task = context.communicate(UserMessage(user_text))
            result = task.result_sync(timeout=120)  # 2 min timeout

            # Stop typing indicator
            typing_stop.set()

            # Auto-reply: dispatch response back to channel
            # Skip if the agent already used send_message tool for this conversation
            if result and not check_and_clear_explicit_send(conv_key):
                response_text = str(result).strip()
                if response_text:
                    response_msg = ChannelMessage(
                        platform=message.platform,
                        channel_id=message.channel_id,
                        user_id="agent",
                        user_name="Agent Zero",
                        direction=Direction.OUTBOUND,
                        content=response_text,
                        reply_to=message.platform_message_id,
                        thread_id=message.thread_id,
                    )
                    # Queue for adapter event loop (cross-thread safe)
                    self.dispatch_outbound_threadsafe(response_msg)
                    self._record_history(conv_key, response_msg)

            # Reaction: done (✅)
            if reaction_on:
                self._send_reaction(message, r_done)

            logger.info(f"[process] {conv_key} complete (context {context.id})")

        except TimeoutError:
            typing_stop.set()
            if reaction_on:
                self._send_reaction(message, r_error)
            logger.error(f"[process] agent timed out for {conv_key}")
        except ImportError as e:
            if reaction_on:
                self._send_reaction(message, r_error)
            logger.error(f"[process] A0 imports unavailable: {e}")
        except Exception as e:
            typing_stop.set()
            if reaction_on:
                self._send_reaction(message, r_error)
            logger.error(f"[process] error for {conv_key}: {type(e).__name__}: {e}")
        finally:
            # Cleanup context
            if context:
                try:
                    from agent import AgentContext as AC
                    context.reset()
                    AC.remove(context.id)
                except Exception:
                    logger.debug(f"[cleanup] error for context {context.id}", exc_info=True)
            lock.release()

    def _send_reaction(self, message: ChannelMessage, emoji: str) -> None:
        """Send an emoji reaction to the inbound message."""
        adapter = self.get_adapter(message.platform)
        if not adapter or not adapter.connected:
            return
        if hasattr(adapter, "send_reaction") and message.platform_message_id:
            from channels_helpers.runner import run_in_bus_loop
            future = run_in_bus_loop(
                adapter.send_reaction(message.channel_id, message.platform_message_id, emoji)
            )
            if future:
                try:
                    future.result(timeout=5)
                except Exception:
                    pass

    def _start_typing_loop(self, message: ChannelMessage) -> threading.Event:
        """Start a background thread that sends typing every 4s until stop_event is set."""
        stop_event = threading.Event()
        adapter = self.get_adapter(message.platform)
        if not adapter or not adapter.connected or not hasattr(adapter, "send_typing"):
            return stop_event

        def _loop():
            from channels_helpers.runner import run_in_bus_loop
            while not stop_event.is_set():
                try:
                    future = run_in_bus_loop(adapter.send_typing(message.channel_id))
                    if future:
                        future.result(timeout=5)
                except Exception:
                    break
                stop_event.wait(4)  # Telegram typing expires after ~5s

        t = threading.Thread(target=_loop, daemon=True, name=f"typing-{message.conversation_key}")
        t.start()
        return stop_event

    def _format_inbound_message(self, message: ChannelMessage) -> str:
        """
        Format a ChannelMessage into a user-facing text for the agent.

        Includes channel metadata so the agent knows where the message came from
        and can respond using send_message or just reply naturally.
        """
        header_parts = [
            f"Channel: {message.platform}",
            f"Chat: {message.channel_id}",
        ]
        if message.user_name:
            header_parts.append(f"From: {message.user_name} ({message.user_id})")
        else:
            header_parts.append(f"From: {message.user_id}")

        if message.thread_id:
            header_parts.append(f"Thread: {message.thread_id}")

        if message.platform_message_id:
            header_parts.append(f"MsgID: {message.platform_message_id}")

        header = " | ".join(header_parts)

        parts = [f"[{header}]"]

        if message.content:
            parts.append(message.content)

        if message.attachments:
            for att in message.attachments:
                att_desc = f"[Attachment: {att.type.value}"
                if att.filename:
                    att_desc += f" - {att.filename}"
                if att.url:
                    att_desc += f" - {att.url}"
                att_desc += "]"
                parts.append(att_desc)

        if message.message_type == MessageType.COMMAND:
            parts.append(f"(Bot command: {message.content})")

        return "\n".join(parts)

    def _record_history(self, conv_key: str, message: ChannelMessage) -> None:
        """Append message to bounded conversation history."""
        with self._history_lock:
            if conv_key not in self._history:
                self._history[conv_key] = []
            self._history[conv_key].append(message.to_dict())
            if len(self._history[conv_key]) > self._history_max:
                self._history[conv_key] = self._history[conv_key][-self._history_max:]


# ── singleton ──

_bus_instance: ChannelBus | None = None
_bus_lock = threading.Lock()


def get_bus() -> ChannelBus | None:
    """Get the singleton ChannelBus instance."""
    with _bus_lock:
        return _bus_instance


def ensure_bus(config: dict[str, Any]) -> ChannelBus:
    """Get or create the singleton ChannelBus."""
    global _bus_instance
    with _bus_lock:
        if _bus_instance is None:
            _bus_instance = ChannelBus(config)
        return _bus_instance
