"""Independent native platform connection settings; no action editor imports."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))


def main() -> int:
    from gxp_form_editor.common import file_lock, runtime_root
    from gxp_form_editor.ui import run_connection_window
    from gxp_editor.runtime_package import RuntimeLease
    with RuntimeLease(Path(__file__).resolve().parents[1], role="ui"):
        with file_lock(runtime_root() / "locks" / "connection-window.lock"):
            return run_connection_window()


if __name__ == "__main__":
    raise SystemExit(main())
