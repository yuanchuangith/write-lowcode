"""Local form review entry point; deliberately not an MCP execute tool."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))


def main() -> int:
    parser = argparse.ArgumentParser(description="GXP form configuration local approval")
    parser.add_argument("--write-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    arguments = parser.parse_args()
    from gxp_form_editor.common import digest, file_lock, runtime_root
    from gxp_form_editor.ui import run_review_window
    from gxp_editor.runtime_package import RuntimeLease
    with RuntimeLease(Path(__file__).resolve().parents[1], role="ui"):
        with file_lock(runtime_root() / "locks" / ("ui-" + digest(arguments.write_id) + ".lock")):
            return run_review_window(arguments.write_id, arguments.conversation_id)


if __name__ == "__main__":
    raise SystemExit(main())
