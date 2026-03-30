"""Auto-start the channels bus daemon on first agent message."""
import sys
from pathlib import Path
from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class ChannelsBoot(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        config = plugins.get_plugin_config("channels_provider", self.agent)
        if not config or not config.get("auto_start", True):
            return

        from channels_helpers.bus import get_bus
        from channels_helpers.runner import start_daemon

        # Skip if already running
        bus = get_bus()
        if bus and bus._running:
            return

        # Start daemon thread with event loop, register + connect adapters
        start_daemon(config)
