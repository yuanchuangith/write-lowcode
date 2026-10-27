"""Internal fixed-entrypoint observer; launched by the explicitly started UI host."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.ui_observer import main

if __name__ == "__main__":
    raise SystemExit(main())
