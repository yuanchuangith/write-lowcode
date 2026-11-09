"""Open the installed human flow handler; never release a flow here."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import _atomic_json, _read, profiles_root


def _record_path():
    from importlib import import_module
    role = "gxp_editor" if (Path(__file__).resolve().parents[1] / "gxp_editor").is_dir() else "gxp_core"
    settings = import_module(role + ".connection_settings")
    return profiles_root(settings.settings_path()) / "session-handler.json"


def register_launcher(root: Path, executable=None):
    from gxp_editor.runtime_package import assert_runtime_resources
    root = Path(root).resolve()
    info = assert_runtime_resources(root)
    if not (root / "scripts/manage_project_sessions.py").is_file():
        raise ValueError("未安装本地流程管理窗口。")
    record = {"schema_version": 1, "root": str(root),
              "executable": str(Path(executable or sys.executable).resolve()),
              "manifest_sha256": info["manifest_sha256"]}
    _atomic_json(_record_path(), record)
    return record


def open_session_manager():
    root = Path(__file__).resolve().parents[2]
    if (root / "mcp/gxp_editor/runtime_package.py").is_file():
        record = register_launcher(root)
    else:
        path = _record_path()
        if not path.is_file():
            raise ValueError("请先双击 Write 文件夹里的“处理未结束流程.cmd”，打开本地流程管理窗口。")
        record = _read(path)
    if record.get("schema_version") != 1:
        raise ValueError("流程管理窗口的本地登记无效，请双击“处理未结束流程.cmd”。")
    root = Path(record["root"]).resolve()
    manifest = root / "runtime-manifest.json"
    if not manifest.is_file():
        raise ValueError("Write 运行包已更新，请重新双击其“处理未结束流程.cmd”。")
    content = json.loads(manifest.read_bytes())
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if digest != record.get("manifest_sha256"):
        raise ValueError("Write 运行包已更新，请重新双击其“处理未结束流程.cmd”。")
    python = Path(record["executable"])
    if os.name == "nt" and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    if not python.is_file() or not (root / "scripts/manage_project_sessions.py").is_file():
        raise ValueError("流程管理程序或 Python 运行环境不存在。")
    env = {**os.environ, "GXP_EDITOR_RUNTIME_ROOT": str(root),
           "GXP_EDITOR_RUNTIME_MANIFEST_SHA256": record["manifest_sha256"],
           "PYTHONPATH": str(root / "mcp") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    process = subprocess.Popen([str(python), str(root / "scripts/manage_project_sessions.py")], cwd=root, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    return {"ok": True, "opened": True, "process_id": process.pid,
            "message": "流程管理窗口已启动，请在窗口中确认处理；尚未释放任何流程。"}
