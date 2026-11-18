"""Build and activate the local ActionDesign compiler used by Write."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from gxp_editor.config import managed_compiler_path


def build(frontend: Path) -> Path:
    frontend = frontend.expanduser().resolve()
    root = subprocess.check_output(["git", "-C", str(frontend), "rev-parse", "--show-toplevel"],
                                   encoding="utf-8", timeout=20).strip()
    if frontend != Path(root).resolve():
        raise ValueError("Pass the GXP frontend Git root")
    target = managed_compiler_path(frontend)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".build-", dir=target.parent) as directory:
        staging = Path(directory)
        subprocess.run(
            ["node", str(ROOT / "scripts" / "build_compiler_bridge.cjs"),
             str(frontend), str(staging), "--single-bundle"],
            check=True, timeout=130, stdout=sys.stderr,
        )
        if {file.name for file in staging.glob("*.js")} != {"bridge.js"}:
            raise ValueError("Expected one self-contained compiler bundle")
        bundle = staging / "bridge.js"
        probe = subprocess.run(
            ["node", str(ROOT / "mcp" / "gxp_editor" / "compiler_runner.cjs"), str(bundle)],
            input='{"command":"describe_registry"}',
            encoding="utf-8", capture_output=True, cwd=frontend, timeout=30,
        )
        result = json.loads(probe.stdout or "{}")
        if probe.returncode or result.get("ok") is not True or not result.get("elements"):
            raise ValueError(result.get("error") or probe.stderr.strip() or "Native registry probe failed")
        os.replace(bundle, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frontend_repo", type=Path, help="GXP frontend Git root with npm dependencies installed")
    args = parser.parse_args()
    try:
        print(build(args.frontend_repo))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Write compiler build failed: {exc}\n")


if __name__ == "__main__":
    main()
