"""
Channel bus daemon runner.

Runs the bus event loop in a dedicated daemon thread, providing an asyncio
event loop for adapters (Telegram polling, Discord gateway, webhook servers).

Pattern matches agui-provider's aiohttp server thread:
    - Singleton daemon thread with asyncio event loop
    - Bus start/stop runs in this loop
    - Adapters register their polling/webhook coroutines in this loop
    - Outbound queue drain runs as a periodic task in this loop
"""

from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from typing import Any

from channels_helpers.bus import ChannelBus, ensure_bus, get_bus

logger = logging.getLogger("channels")

_runner_thread: threading.Thread | None = None
_runner_loop: asyncio.AbstractEventLoop | None = None
_runner_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop | None:
    """Get the bus daemon event loop (for scheduling adapter coroutines)."""
    return _runner_loop


def run_in_bus_loop(coro) -> asyncio.Future | None:
    """
    Schedule a coroutine in the bus daemon event loop from any thread.

    Returns an asyncio.Future that can be awaited from the bus loop,
    or waited on with future.result() from other threads.
    """
    loop = get_loop()
    if loop and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop)
    logger.warning("[runner] bus loop not running, cannot schedule coroutine")
    return None


def start_daemon(config: dict[str, Any]) -> ChannelBus:
    """
    Start the bus daemon thread if not already running.

    Creates the bus singleton, starts a daemon thread with an asyncio event loop,
    connects all enabled adapters, and starts the outbound drain loop.

    Safe to call multiple times — idempotent.
    """
    global _runner_thread, _runner_loop

    with _runner_lock:
        if _runner_thread and _runner_thread.is_alive():
            bus = get_bus()
            if bus:
                return bus

        bus = ensure_bus(config)

        ready = threading.Event()

        def _run():
            global _runner_loop
            _runner_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_runner_loop)
            ready.set()
            _runner_loop.run_forever()

        _runner_thread = threading.Thread(
            target=_run,
            daemon=True,
            name="channels-bus-daemon",
        )
        _runner_thread.start()
        ready.wait(timeout=5)

        if _runner_loop:
            # Start bus + adapters in the daemon loop
            future = asyncio.run_coroutine_threadsafe(
                _startup(bus, config), _runner_loop
            )
            # Wait for startup to complete (10s timeout)
            try:
                future.result(timeout=10)
            except Exception as e:
                logger.error(f"[runner] startup failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")

        return bus


async def _startup(bus: ChannelBus, config: dict[str, Any]) -> None:
    """Initialize bus and start adapters + drain loop (runs in daemon loop)."""

    # Register enabled adapters
    _register_adapters(bus, config)

    # Connect all registered adapters
    await bus.start()

    # Start outbound drain loop
    asyncio.ensure_future(_outbound_drain_loop(bus))

    logger.info("[runner] daemon startup complete")


def _register_adapters(bus: ChannelBus, config: dict[str, Any]) -> None:
    """
    Instantiate and register enabled adapters based on config.

    Each adapter is conditionally imported and registered only if its
    _enabled flag is True and credentials are provided.
    """
    # Telegram
    if config.get("telegram_enabled") and config.get("telegram_bot_token"):
        try:
            from channels_helpers.adapters.telegram import TelegramAdapter
            adapter = TelegramAdapter(
                config={
                    "bot_token": config["telegram_bot_token"],
                    "allowed_chats": config.get("telegram_allowed_chats", ""),
                    "parse_mode": config.get("telegram_parse_mode", "Markdown"),
                    "commands": config.get("telegram_commands", "[]"),
                    "handle_commands_internally": config.get("telegram_handle_commands_internally", ""),
                },
                on_message=bus.route_inbound,
            )
            bus.register(adapter)
            logger.info("[runner] registered Telegram adapter")
        except ImportError as e:
            logger.error(f"[runner] Telegram adapter import failed: {e}\n{traceback.format_exc()}")
        except Exception as e:
            logger.error(f"[runner] failed to create Telegram adapter: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    # Discord
    if config.get("discord_enabled") and config.get("discord_bot_token"):
        try:
            from channels_helpers.adapters.discord import DiscordAdapter
            adapter = DiscordAdapter(
                config={
                    "bot_token": config["discord_bot_token"],
                    "allowed_guilds": config.get("discord_allowed_guilds", ""),
                    "allowed_channels": config.get("discord_allowed_channels", ""),
                },
                on_message=bus.route_inbound,
            )
            bus.register(adapter)
            logger.info("[runner] registered Discord adapter")
        except ImportError as e:
            logger.error(f"[runner] Discord adapter import failed: {e}\n{traceback.format_exc()}")
        except Exception as e:
            logger.error(f"[runner] failed to create Discord adapter: {e}")

    # WhatsApp
    if config.get("whatsapp_enabled") and config.get("whatsapp_access_token"):
        try:
            from channels_helpers.adapters.whatsapp import WhatsAppAdapter
            adapter = WhatsAppAdapter(
                config={
                    "phone_id": config.get("whatsapp_phone_id", ""),
                    "access_token": config["whatsapp_access_token"],
                    "verify_token": config.get("whatsapp_verify_token", ""),
                    "webhook_port": config.get("whatsapp_webhook_port", 8402),
                },
                on_message=bus.route_inbound,
            )
            bus.register(adapter)
            logger.info("[runner] registered WhatsApp adapter")
        except ImportError as e:
            logger.error(f"[runner] WhatsApp adapter import failed: {e}\n{traceback.format_exc()}")
        except Exception as e:
            logger.error(f"[runner] failed to create WhatsApp adapter: {e}")


async def _outbound_drain_loop(bus: ChannelBus) -> None:
    """
    Periodically drain the outbound queue and dispatch messages.

    Runs forever in the daemon event loop. This is how messages from
    the agent's DeferredTask thread get dispatched through adapters.
    """
    while bus._running:
        try:
            count = await bus.drain_outbound()
            if count > 0:
                logger.debug(f"[drain] dispatched {count} outbound messages")
        except Exception as e:
            logger.error(f"[drain] error: {type(e).__name__}: {e}")
        await asyncio.sleep(0.1)  # 100ms poll interval


async def stop_daemon() -> None:
    """Stop the bus and all adapters gracefully."""
    bus = get_bus()
    if bus:
        await bus.stop()

    global _runner_loop
    if _runner_loop and _runner_loop.is_running():
        _runner_loop.call_soon_threadsafe(_runner_loop.stop)
