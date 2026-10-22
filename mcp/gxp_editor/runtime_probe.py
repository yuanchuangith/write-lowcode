"""Isolate advisory DLL reads and compilation from the MCP server process."""
from __future__ import annotations

import json
import sys

from .config import RuntimeReferenceConfig
from .runtime_validation import RuntimeCsharpValidator


def main() -> int:
    validator = None
    try:
        # Windows pipes otherwise default to the user ANSI code page (for example GBK).
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        validator = object.__new__(RuntimeCsharpValidator)
        validator.runtime = RuntimeReferenceConfig.from_dict(request["runtime"])
        validator.last_performance = []
        validator.request_id = request.get("request_id")
        if request["command"] == "status":
            result = validator._status()
        elif request["command"] == "validate":
            result = validator._validate(request["action_code"], request["csharp_code"])
        else:
            raise ValueError("Unsupported runtime probe command")
        response = {"ok": True, "result": result}
        code = 0
    except Exception as exc:
        response = {
            "ok": False, "error": str(exc),
            "error_code": getattr(exc, "code", "checker_unavailable"),
            "diagnostics": getattr(exc, "diagnostics", []),
        }
        code = 1
    response["performance"] = getattr(validator, "last_performance", [])
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
