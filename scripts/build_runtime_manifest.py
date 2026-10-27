"""Rebuild byte hashes after all runtime and plugin-version changes are final."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from gxp_editor.runtime_package import write_manifest

if __name__ == "__main__":
    print(write_manifest(ROOT))
