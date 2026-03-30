"""Plugin install/uninstall hooks.

Called by the Agent Zero plugin installer when the plugin is first installed
or removed. Uses sys.executable to ensure packages are installed into the
correct A0 venv (e.g. /opt/venv-a0/) rather than any other Python environment.
"""
import subprocess
import sys
from pathlib import Path


def install():
    """Install pip dependencies from requirements.txt into the A0 venv."""
    plugin_root = Path(__file__).resolve().parent
    req_file = plugin_root / "requirements.txt"
    if not req_file.exists():
        print("[channels_provider] no requirements.txt found, skipping dep install")
        return

    print(f"[channels_provider] installing dependencies via {sys.executable}...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        print("[channels_provider] dependencies installed successfully")
    else:
        print(f"[channels_provider] pip install failed:\n{result.stderr}")


def uninstall():
    """Remove pip dependencies on plugin uninstall."""
    plugin_root = Path(__file__).resolve().parent
    req_file = plugin_root / "requirements.txt"
    if not req_file.exists():
        return

    print("[channels_provider] removing dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", "-r", str(req_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(f"[channels_provider] uninstall complete (rc={result.returncode})")
