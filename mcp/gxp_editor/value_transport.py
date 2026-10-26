"""Bounded JSON projections; complete values remain in local bound evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .hashing import canonical_json


def value_summary(value: Any, *, preview_chars: int = 120) -> dict[str, Any]:
    raw = canonical_json(value).encode("utf-8")
    kind = ("null" if value is None else "boolean" if type(value) is bool else
            "object" if type(value) is dict else "array" if type(value) is list else
            "string" if type(value) is str else "number")
    result = {"type": kind, "utf8_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if type(value) in (dict, list, str):
        result["length"] = len(value)
    if type(value) is str:
        result.update(preview=value[:preview_chars], preview_complete=len(value) <= preview_chars)
    elif type(value) not in (dict, list):
        result["value"] = value
    return result


def value_page(value: Any, *, offset: int = 0, max_output_bytes: int = 16384) -> dict[str, Any]:
    """Offsets count Unicode code points in canonical JSON, never bytes or rows.

    Fragment responses never claim a complete value, including the last page.
    Reserve room for metadata so the value response stays below the budget.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative character offset")
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536:
        raise ValueError("max_output_bytes must be between 1024 and 65536")
    text = canonical_json(value)
    raw = text.encode("utf-8")
    if offset > len(text):
        raise ValueError("offset is outside this value")
    digest = hashlib.sha256(raw).hexdigest()
    result = {"encoding": "canonical_json_text", "offset_unit": "unicode_code_points",
              "value_sha256": digest, "total_bytes": len(raw), "total_chars": len(text),
              "offset": offset, "max_output_bytes": max_output_bytes}
    if offset == 0 and len(raw) <= max_output_bytes - 768:
        return {**result, "value": value, "complete": True, "next_offset": None, "truncated": False}
    # JSON quoting can expand control characters in the text fragment. Bound
    # the actual serialized page, not just the unescaped source bytes.
    end = min(len(text), offset + max(1, (max_output_bytes - 768) // 2))
    while True:
        fragment = text[offset:end]
        page = {**result, "text": fragment, "complete": False, "truncated": True,
                "next_offset": end if end < len(text) else None, "continuation_complete": end == len(text)}
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= max_output_bytes:
            return page
        end = offset + max(1, (end - offset) // 2)


def field_value(value: Any, path: str) -> Any:
    """Resolve a read-only JSON pointer in report evidence."""
    if type(path) is not str or not path.startswith("/") or len(path) > 2048:
        raise ValueError("field_path must be a JSON pointer of at most 2048 characters")
    for token in path[1:].split("/"):
        if "~" in token.replace("~0", "").replace("~1", ""):
            raise ValueError("field_path contains an invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if type(value) is dict and token in value:
            value = value[token]
        elif type(value) is list and token.isascii() and token.isdecimal() and str(int(token)) == token and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError("field_path does not exist in the selected evidence")
    return value
