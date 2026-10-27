"""Request-local timings. Never place these observations in signed evidence."""
from __future__ import annotations

import logging
import math
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from .evidence_guidance import guidance

_ACTIVE = ContextVar("gxp_performance_trace", default=None)
_ORIGIN = ContextVar("gxp_performance_origin", default=None)
_DEPTH = ContextVar("gxp_performance_depth", default=0)
_TAGS = {"cache_hit", "cold", "count", "operation_count", "reference_count", "process_id", "slot_wait_ms", "error_code"}
_LOG = logging.getLogger("gxp.performance")


def current_trace_id():
    trace = _ACTIVE.get()
    return trace["trace_id"] if trace else ""


def _append(name, elapsed, tags, *, start_ms=None, depth=None):
    trace = _ACTIVE.get()
    if trace is None or len(trace["timings"]) >= 256:
        return
    if not isinstance(name, str) or len(name) > 96 or not all(c.isalnum() or c in "._-" for c in name):
        return
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        return
    item = {"stage": name, "elapsed_ms": round(elapsed, 3)}
    if start_ms is not None and math.isfinite(start_ms) and start_ms >= 0:
        item["start_ms"] = round(start_ms, 3)
        item["depth"] = depth
    for key, value in tags.items():
        if key in _TAGS and (type(value) in (bool, int) or type(value) is float and math.isfinite(value)
                             or key == "error_code" and isinstance(value, str) and len(value) <= 80
                             and all(c.isalnum() or c in "_-" for c in value)):
            item[key] = value
    trace["timings"].append(item)


@contextmanager
def stage(name, **tags):
    started = time.perf_counter()
    origin = _ORIGIN.get()
    depth = _DEPTH.get()
    token = _DEPTH.set(depth + 1)
    try:
        yield tags
    except Exception as exc:
        tags["error_code"] = getattr(exc, "code", type(exc).__name__)
        raise
    finally:
        _DEPTH.reset(token)
        _append(name, (time.perf_counter() - started) * 1000, tags,
                start_ms=(started - origin) * 1000 if origin is not None else None, depth=depth)


def add_external(spans):
    if isinstance(spans, list):
        for item in spans[:128]:
            if isinstance(item, dict):
                _append(item.get("stage", ""), item.get("elapsed_ms"), item)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def traced_tool(function):
    """Wrap only public responses, so timings cannot enter plans or hashes."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _ACTIVE.get() is not None:
            return function(*args, **kwargs)
        trace = {"trace_id": uuid.uuid4().hex, "timings": []}
        token = _ACTIVE.set(trace)
        started = time.perf_counter()
        origin_token = _ORIGIN.set(started)
        depth_token = _DEPTH.set(0)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            details = getattr(exc, "details", {})
            details = details if isinstance(details, dict) else {}
            result = {"blocked": True, "write_executed": False,
                      "error": str(exc)[:1000],
                      "error_code": getattr(exc, "code", None) or details.get("reason_code") or type(exc).__name__,
                      "error_details": details}
        finally:
            trace["total_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            _DEPTH.reset(depth_token)
            _ORIGIN.reset(origin_token)
            _ACTIVE.reset(token)
        if isinstance(result, dict) and (result.get("error") or result.get("error_code")):
            # Normalize only the transport envelope, including failures returned
            # by a tool instead of raised. Do not alter stored review artifacts.
            details = result.get("error_details")
            details = details if isinstance(details, dict) else {}
            failed = next((item["stage"] for item in reversed(trace["timings"]) if item.get("error_code")), function.__name__)
            result = {**result,
                      "failure_stage": details.get("phase") or failed,
                      "error_code": result.get("error_code") or details.get("reason_code") or "request_failed"}
            if "evidence_gaps" not in result:
                result.update(guidance(result["error_code"], "requested_conclusion",
                    next_read=result.get("next_read"), can_continue=("previously_confirmed_facts",),
                    stop_reason="required_check_failed", attempted=(function.__name__,)))
        # Log only timing metadata, never payloads, diagnostics or exception text.
        _LOG.info("trace=%s elapsed_ms=%s stages=%s", trace["trace_id"], trace["total_elapsed_ms"], trace["timings"])
        return {**result, **trace} if isinstance(result, dict) else result
    return wrapped
