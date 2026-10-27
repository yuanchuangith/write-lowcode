"""Start explicitly from a user terminal, outside the MCP client's process tree."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.ui_host import main

if __name__ == "__main__":
    raise SystemExit(main())
