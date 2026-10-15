"""Isolate advisory DLL reads and compilation from the MCP server process."""
from __future__ import annotations

import json
import sys

from .config import RuntimeReferenceConfig
from .runtime_validation import RuntimeCsharpValidator


def main() -> int:
    try:
        request = json.load(sys.stdin)
        validator = object.__new__(RuntimeCsharpValidator)
        validator.runtime = RuntimeReferenceConfig.from_dict(request["runtime"])
        if request["command"] == "status":
            result = validator._status()
        elif request["command"] == "validate":
            result = validator._validate(request["action_code"], request["csharp_code"])
        else:
            raise ValueError("Unsupported runtime probe command")
        response = {"ok": True, "result": result}
        code = 0
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}
        code = 1
    print(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
