"""Compatibility entry point used by the existing systemd unit."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).parent / "src"))

from organiser.api.app import app as app  # noqa: E402
