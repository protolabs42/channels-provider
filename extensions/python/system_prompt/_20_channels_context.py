"""Inject channel awareness into the agent's system prompt."""
import sys
from pathlib import Path
from python.helpers.extension import Extension
from python.helpers import plugins
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class ChannelsContext(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        config = plugins.get_plugin_config("channels", self.agent)
        if not config:
            return

        from channels_helpers.bus import get_bus

        bus = get_bus()
        if not bus or not bus._running:
            return

        # Build context about active channels
        adapters = bus.adapters
        if not adapters:
            return

        lines = [
            "## Active Channel Connections",
            "You are connected to external messaging platforms. "
            "Messages from these channels appear as inbound notifications with "
            "[Channel: ...] headers. Your response is automatically sent back "
            "to the originating channel. Use the send_message tool only for "
            "proactive messages to different channels.",
            "",
        ]

        for name, adapter in adapters.items():
            status = "connected" if adapter.connected else "disconnected"
            lines.append(f"- **{name}**: {status}")

        conversations = bus.list_conversations()
        if conversations:
            lines.append("")
            lines.append(f"Active conversations: {len(conversations)}")

        context_text = "\n".join(lines)

        # Append to system prompt extras
        if hasattr(loop_data, "extras_persistent"):
            loop_data.extras_persistent["channels"] = context_text
