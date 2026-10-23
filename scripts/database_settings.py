"""Shared Look / Write development database settings entry."""

from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.connection_settings import cli_main


if __name__ == "__main__":
    raise SystemExit(cli_main())
