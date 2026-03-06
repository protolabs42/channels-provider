"""
Send a message to a platform channel via the channels bus.

The agent uses this tool to reply on Telegram, Discord, or WhatsApp.

When the agent is processing an inbound channel message, the bus auto-replies
with the agent's final response. Use this tool for:
  - Proactive messages to a different channel
  - Sending to a specific thread
  - Overriding auto-reply (calling this suppresses auto-reply for that conversation)
"""
import sys
from pathlib import Path

from helpers.tool import Tool, Response

_plugin_root = Path(__file__).resolve().parents[1]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class SendMessage(Tool):

    async def execute(self, platform="", channel_id="", content="", reply_to="", thread_id="", **kwargs):
        if not platform or not channel_id or not content:
            return Response(
                message="Missing required args: platform, channel_id, and content.",
                break_loop=False,
            )

        from channels_helpers.bus import get_bus, mark_explicit_send
        from channels_helpers.schema import ChannelMessage, Direction

        bus = get_bus()
        if not bus:
            return Response(message="Channels bus is not running.", break_loop=False)

        adapter = bus.get_adapter(platform)
        if not adapter:
            return Response(
                message=f"No adapter registered for platform: {platform}",
                break_loop=False,
            )

        if not adapter.connected:
            return Response(
                message=f"Adapter {platform} is not connected.",
                break_loop=False,
            )

        msg = ChannelMessage(
            platform=platform,
            channel_id=channel_id,
            user_id="agent",
            user_name="Agent Zero",
            direction=Direction.OUTBOUND,
            content=content,
            reply_to=reply_to or None,
            thread_id=thread_id or None,
        )

        # Mark this conversation as having an explicit send
        # This suppresses auto-reply for the current inbound message
        mark_explicit_send(msg.conversation_key)

        # Queue for adapter event loop (thread-safe — this tool runs
        # in the agent's DeferredTask thread, not the adapter's loop)
        bus.dispatch_outbound_threadsafe(msg)

        return Response(
            message=f"Message queued for {platform}:{channel_id}",
            break_loop=False,
        )
