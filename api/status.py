"""Channels plugin status and control API handler."""
import sys
from pathlib import Path
from python.helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class StatusHandler(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "status")

        if action == "status":
            return self._status()
        elif action == "start":
            return await self._start()
        elif action == "stop":
            return await self._stop()
        elif action == "conversations":
            return self._conversations()
        return {"ok": False, "error": f"Unknown action: {action}"}

    def _status(self) -> dict:
        from channels_helpers.bus import get_bus

        bus = get_bus()
        if not bus or not bus._running:
            return {"ok": True, "running": False, "adapters": {}, "active_conversations": 0}

        status = bus.status()
        return {
            "ok": True,
            "running": True,
            "adapters": status["adapters"],
            "active_conversations": status["active_conversations"],
        }

    async def _start(self) -> dict:
        from python.helpers import plugins
        from channels_helpers.runner import start_daemon

        config = plugins.get_plugin_config("channels") or {}

        try:
            start_daemon(config)
            return {"ok": True, "running": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _stop(self) -> dict:
        from channels_helpers.runner import run_in_bus_loop, stop_daemon

        future = run_in_bus_loop(stop_daemon())
        if future:
            try:
                future.result(timeout=10)
            except Exception:
                pass
        return {"ok": True, "running": False}

    def _conversations(self) -> dict:
        from channels_helpers.bus import get_bus

        bus = get_bus()
        if not bus or not bus._running:
            return {"ok": True, "conversations": []}

        return {"ok": True, "conversations": bus.list_conversations()}
