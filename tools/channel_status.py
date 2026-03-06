"""
Check channel status and list recent messages from a conversation.

The agent uses this to see what channels are connected, list conversations,
and read recent message history.
"""
import sys
from pathlib import Path

from helpers.tool import Tool, Response

_plugin_root = Path(__file__).resolve().parents[1]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class ChannelStatus(Tool):

    async def execute(self, action="status", conversation_key="", limit=20, **kwargs):
        from channels_helpers.bus import get_bus

        bus = get_bus()
        if not bus:
            return Response(message="Channels bus is not running.", break_loop=False)

        if action == "status":
            return self._bus_status(bus)
        elif action == "conversations":
            return self._list_conversations(bus)
        elif action == "history":
            return self._get_history(bus, conversation_key, int(limit))
        else:
            return Response(
                message=f"Unknown action: {action}. Use: status, conversations, history",
                break_loop=False,
            )

    def _bus_status(self, bus) -> Response:
        status = bus.status()
        lines = [f"Bus running: {status['running']}"]
        lines.append(f"Active conversations: {status['active_conversations']}")
        for name, adapter in status["adapters"].items():
            lines.append(f"  {name}: {'connected' if adapter['connected'] else 'disconnected'}")
        return Response(message="\n".join(lines), break_loop=False)

    def _list_conversations(self, bus) -> Response:
        conversations = bus.list_conversations()
        if not conversations:
            return Response(message="No active conversations.", break_loop=False)

        lines = [f"Active conversations ({len(conversations)}):"]
        for conv in conversations:
            thread = f" thread={conv['thread_id']}" if conv.get("thread_id") else ""
            lines.append(
                f"  [{conv['platform']}] {conv['channel_id']}{thread} "
                f"({conv['message_count']} msgs)"
            )
        return Response(message="\n".join(lines), break_loop=False)

    def _get_history(self, bus, conversation_key: str, limit: int) -> Response:
        if not conversation_key:
            return Response(
                message="Provide conversation_key (e.g. telegram:-100123456)",
                break_loop=False,
            )

        messages = bus.get_history(conversation_key, limit)
        if not messages:
            return Response(
                message=f"No messages found for {conversation_key}",
                break_loop=False,
            )

        lines = [f"Recent messages for {conversation_key} ({len(messages)}):"]
        for msg in messages:
            direction = msg.get("direction", "?")
            user = msg.get("user_name") or msg.get("user_id", "?")
            content = msg.get("content", "")[:120]
            ts = msg.get("timestamp", "")[:19]
            arrow = "<-" if direction == "inbound" else "->"
            lines.append(f"  {ts} {arrow} [{user}] {content}")

        return Response(message="\n".join(lines), break_loop=False)
