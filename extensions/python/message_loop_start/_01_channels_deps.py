"""Auto-install channels plugin pip dependencies to persistent volume."""
import subprocess
import sys
import logging
from pathlib import Path
from python.helpers.extension import Extension
from agent import LoopData

logger = logging.getLogger("channels")

# Persistent lib dir on the a0-usr volume — survives container rebuilds
_USR_LIB = Path("/a0/usr/lib")
_CHECKED = False


class ChannelsDeps(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        global _CHECKED
        if _CHECKED:
            return
        _CHECKED = True

        # Ensure usr/lib is on sys.path so imports find volume-installed packages
        lib_str = str(_USR_LIB)
        if lib_str not in sys.path:
            sys.path.insert(0, lib_str)

        # Find requirements.txt relative to plugin root
        plugin_root = Path(__file__).resolve().parents[3]
        req_file = plugin_root / "requirements.txt"
        if not req_file.exists():
            return

        # Quick check: can we import the key packages?
        missing = []
        try:
            import aiogram  # noqa: F401
        except ImportError:
            missing.append("aiogram")
        try:
            import discord  # noqa: F401
        except ImportError:
            missing.append("discord.py")

        if not missing:
            logger.debug("[deps] all channel dependencies available")
            return

        # Install to persistent volume path (survives image rebuilds)
        _USR_LIB.mkdir(parents=True, exist_ok=True)
        logger.info(f"[deps] missing: {missing} — installing to {_USR_LIB}")
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "-q", "--target", lib_str,
                    "-r", str(req_file),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                logger.info("[deps] installed to persistent volume")
            else:
                logger.error(f"[deps] pip install failed: {result.stderr}")
        except Exception as e:
            logger.error(f"[deps] failed to install: {e}")
