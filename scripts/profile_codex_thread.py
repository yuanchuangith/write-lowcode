"""Summarize one explicitly supplied Codex JSONL using metadata only.

No transcript text, command arguments, business values, credentials, file paths,
or session identifiers are emitted. Tool bytes refer to serialized JSON values,
not transport bytes. Timing gaps cannot identify model, network, or review time.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable


def _number(value: Any) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _label(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "unknown"


def _bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _stats(values: Iterable[float]) -> dict[str, Any]:
    values = sorted(values)
    if not values:
        return {"count": 0, "sum": 0, "median": None, "p95": None, "max": None}
    size = len(values)
    middle = values[size // 2] if size % 2 else (values[size // 2 - 1] + values[size // 2]) / 2
    return {"count": size, "sum": round(sum(values), 3), "median": round(middle, 3),
            "p95": round(values[max(0, math.ceil(size * .95) - 1)], 3), "max": round(values[-1], 3)}


def _merge(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _seconds(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge(intervals))


def _within(intervals, bounds) -> float:
    return _seconds((max(start, left), min(end, right)) for start, end in _merge(intervals)
                    for left, right in _merge(bounds) if max(start, left) <= min(end, right))


def _result_data(result: Any) -> dict:
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    # Some older tools only expose JSON text. Parse internally, never emit it.
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                value = json.loads(block.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def profile_records(records: Iterable[dict], *, source_bytes: int | None = None,
                    invalid_lines: int = 0) -> dict[str, Any]:
    """Accept records from one thread and return only aggregate metadata."""
    record_types = Counter()
    models = Counter()
    efforts = Counter()
    windows = Counter()
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    end_kinds = Counter()
    first_token_ms = []
    usage_by_response: dict[str, dict] = {}
    usage_contexts: list[int] = []
    mcp_by_id: dict[str, tuple[float, dict]] = {}
    outer_by_id: dict[str, dict] = {}
    actions = []
    context_compactions = 0
    timestamps = []
    turn_id = ""
    user_input_times = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        record_types[_label(record.get("type"))] += 1
        stamp = _timestamp(record.get("timestamp"))
        if stamp is not None:
            timestamps.append(stamp)
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        kind = payload.get("type")
        if record.get("type") in {"compacted", "context_compacted"} or kind in {"compacted", "context_compacted"}:
            context_compactions += 1
        if record.get("type") == "turn_context":
            turn_id = str(payload.get("turn_id") or "")
            models[_label(payload.get("model"))] += 1
            efforts[_label(payload.get("effort"))] += 1
        if kind == "task_started" and stamp is not None:
            starts[str(payload.get("turn_id") or "")] = stamp
            window = _number(payload.get("model_context_window"))
            if window is not None:
                windows[int(window)] += 1
        if kind in {"task_complete", "turn_aborted"} and stamp is not None:
            ends[str(payload.get("turn_id") or "")] = stamp
            end_kinds[kind] += 1
            first = _number(payload.get("time_to_first_token_ms"))
            if first is not None:
                first_token_ms.append(first)
        if record.get("type") == "token_usage_record":
            response_key = str(payload.get("response_id") or f"record-{index}")
            usage_by_response[response_key] = payload
        if kind == "item_completed" and stamp is not None:
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "McpToolCall":
                key = str(item.get("id") or f"record-{index}")
                mcp_by_id[key] = (stamp, item)
        if record.get("type") != "response_item" or stamp is None:
            continue
        if kind == "message" and payload.get("role") == "user":
            user_input_times.append(stamp)
        if kind in {"custom_tool_call", "function_call"}:
            call_id = str(payload.get("call_id") or f"record-{index}")
            outer_by_id.setdefault(call_id, {"start": stamp, "turn_id": turn_id,
                                             "name": _label(payload.get("name")), "outputs": [],
                                             "input_bytes": _bytes(payload.get("arguments", payload.get("input", "")))})
            actions.append(stamp)
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            call = outer_by_id.get(str(payload.get("call_id") or ""))
            if call is not None:
                call["outputs"].append((stamp, _bytes(payload.get("output", ""))))
        elif kind == "message" and payload.get("role") == "assistant":
            actions.append(stamp)

    active_bounds = _merge((start, ends[key]) for key, start in starts.items() if key in ends)
    active_seconds = _seconds(active_bounds)
    wall_seconds = max(timestamps) - min(timestamps) if timestamps else 0
    outer_waits = []
    outer_tools = defaultdict(lambda: {"calls": 0, "input_json_bytes": 0, "output_json_bytes": 0, "seconds": []})
    for call in outer_by_id.values():
        group = outer_tools[call["name"]]
        group["calls"] += 1
        group["input_json_bytes"] += call["input_bytes"]
        group["output_json_bytes"] += sum(size for _, size in call["outputs"])
        if call["outputs"]:
            first_output = call["outputs"][0][0]
            group["seconds"].append(first_output - call["start"])
            outer_waits.append((call["start"], first_output))

    tools = defaultdict(lambda: {"calls": 0, "input_json_bytes": 0, "result_json_bytes": 0,
                                "duration_seconds": [], "server_elapsed_ms": [],
                                "response_detail": Counter(), "parameter_paths_calls": 0,
                                "operations_count": [], "generation_mode": Counter(),
                                "argument_fingerprints": Counter(), "result_sizes": []})
    mcp_intervals = []
    top_mcp = []
    for stamp, item in mcp_by_id.values():
        tool = _label(item.get("tool"))
        group = tools[tool]
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = item.get("result")
        group["calls"] += 1
        group["input_json_bytes"] += _bytes(args)
        group["result_json_bytes"] += _bytes(result)
        group["result_sizes"].append(_bytes(result))
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        group["argument_fingerprints"][fingerprint] += 1
        group["response_detail"][_label(args.get("response_detail", "omitted"))] += 1
        group["parameter_paths_calls"] += int("parameter_paths" in args)
        operations = args.get("operations")
        if isinstance(operations, list):
            group["operations_count"].append(len(operations))
        if "generation_mode" in args:
            group["generation_mode"][_label(args.get("generation_mode"))] += 1
        duration = item.get("duration")
        elapsed = None
        if isinstance(duration, dict):
            seconds = _number(duration.get("secs"))
            nanos = _number(duration.get("nanos"))
            if seconds is not None and nanos is not None:
                elapsed = seconds + nanos / 1e9
                group["duration_seconds"].append(elapsed)
                mcp_intervals.append((stamp - elapsed, stamp))
        data = _result_data(result)
        native = _number(data.get("total_elapsed_ms"))
        if native is not None:
            group["server_elapsed_ms"].append(native)
        top_mcp.append({"tool": tool, "duration_seconds": round(elapsed, 3) if elapsed is not None else None,
                        "server_elapsed_ms": round(native, 3) if native is not None else None,
                        "result_json_bytes": _bytes(result)})

    totals = Counter()
    for payload in usage_by_response.values():
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            number = _number(usage.get(name))
            if number is not None:
                totals[name] += int(number)
        context = _number(usage.get("input_tokens"))
        if context is not None:
            usage_contexts.append(int(context))
    contexts = usage_contexts
    known_intervals = _merge([*outer_waits, *mcp_intervals])
    known_active_seconds = _within(known_intervals, active_bounds)

    # One gap per following action: simultaneous tool returns must not create
    # overlapping "model time" claims. Preserve only the latest preceding return.
    gap_by_action = {}
    actions.sort()
    for call in outer_by_id.values():
        if not call["outputs"]:
            continue
        last_return = call["outputs"][-1][0]
        turn_end = ends.get(call["turn_id"])
        if turn_end is None:
            continue
        following = next((stamp for stamp in actions if last_return <= stamp <= turn_end), None)
        if following is not None:
            gap_by_action[following] = max(last_return, gap_by_action.get(following, last_return))
    gap_intervals = [(start, end) for end, start in gap_by_action.items()]
    gaps = [end - start for start, end in gap_intervals]
    user_input_gaps = sum(any(start < stamp < end for stamp in user_input_times) for start, end in gap_intervals)

    mcp_summary = []
    for name, data in sorted(tools.items()):
        mcp_summary.append({"tool": name, "calls": data["calls"], "input_json_bytes": data["input_json_bytes"],
                            "result_json_bytes": data["result_json_bytes"], "duration_seconds": _stats(data["duration_seconds"]),
                            "server_elapsed_ms": _stats(data["server_elapsed_ms"]), "response_detail": dict(data["response_detail"]),
                            "parameter_paths_calls": data["parameter_paths_calls"], "operations_count": _stats(data["operations_count"]),
                            "generation_mode": dict(data["generation_mode"]),
                            "identical_argument_repeat_groups": sum(value > 1 for value in data["argument_fingerprints"].values()),
                            "extra_calls_with_identical_arguments": sum(value - 1 for value in data["argument_fingerprints"].values()),
                            "max_result_json_bytes": max(data["result_sizes"], default=0),
                            "results_over_16k": sum(size > 16384 for size in data["result_sizes"]),
                            "results_over_64k": sum(size > 65536 for size in data["result_sizes"])})
    output = {
        "schema_version": 1, "source_bytes": source_bytes, "invalid_jsonl_lines": invalid_lines,
        "record_count": sum(record_types.values()), "record_types": dict(record_types),
        "models_by_turn_context": dict(models), "efforts_by_turn_context": dict(efforts),
        "reported_context_windows": dict(sorted(windows.items())), "explicit_compaction_events": context_compactions,
        "timeline": {"wall_seconds": round(wall_seconds, 3), "closed_active_span_seconds": round(active_seconds, 3),
                     "outside_closed_active_span_seconds": round(max(0, wall_seconds - active_seconds), 3),
                     "started_turns": len(starts), "ended_turns": dict(end_kinds), "unfinished_turns": len(set(starts) - set(ends)),
                     "first_token_ms": _stats(first_token_ms),
                     "observed_outer_first_return_wait_seconds": round(_within(outer_waits, active_bounds), 3),
                     "mcp_duration_union_within_active_seconds": round(_within(mcp_intervals, active_bounds), 3),
                     "all_observed_tool_intervals_union_within_active_seconds": round(known_active_seconds, 3),
                     "active_unattributed_seconds": round(max(0, active_seconds - known_active_seconds), 3)},
        "usage": {"unique_responses": len(usage_by_response), "totals": dict(totals),
                  "single_request_input_tokens": _stats(contexts), "first_input_tokens": contexts[0] if contexts else None,
                  "last_input_tokens": contexts[-1] if contexts else None,
                  "requests_over_200k": sum(value > 200000 for value in contexts),
                  "requests_over_500k": sum(value > 500000 for value in contexts),
                  "requests_over_600k": sum(value > 600000 for value in contexts)},
        "outer_tools": [{"tool": name, "calls": value["calls"], "input_json_bytes": value["input_json_bytes"],
                         "output_json_bytes": value["output_json_bytes"], "first_return_seconds": _stats(value["seconds"])}
                        for name, value in sorted(outer_tools.items())],
        "mcp_tools": mcp_summary,
        "mcp_total": {"unique_calls": len(mcp_by_id), "duration_sum_seconds_not_wall_time": round(sum(sum(data["duration_seconds"]) for data in tools.values()), 3),
                      "result_json_bytes": sum(data["result_json_bytes"] for data in tools.values())},
        "top_mcp_calls": sorted(top_mcp, key=lambda value: value["duration_seconds"] or 0, reverse=True)[:15],
        "tool_return_to_next_assistant_action_unknown_gaps": {"duration_seconds": _stats(gaps),
            "over_30_seconds": sum(value > 30 for value in gaps), "over_60_seconds": sum(value > 60 for value in gaps),
            "containing_user_input": user_input_gaps, "longest_seconds": [round(value, 3) for value in sorted(gaps, reverse=True)[:10]]},
        "limits": ["Active task spans may include review waits and steered user input; they are not pure compute time.",
                   "Unknown gaps do not distinguish model reasoning, provider/network delay, scheduling, or human review.",
                   "Outer tool intervals and nested MCP intervals overlap; only their union is a wall-clock observation.",
                   "Serialized JSON sizes are metadata estimates and can count duplicated text/structured results.",
                   "No explicit compaction event is evidence only for the supplied file, not every internal server operation."]}
    return output


def profile_file(path: Path) -> dict[str, Any]:
    invalid = 0
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except ValueError:
                invalid += 1
    return profile_records(records, source_bytes=path.stat().st_size, invalid_lines=invalid)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="The exact thread JSONL to read; no directory scan is performed.")
    parser.add_argument("--output", type=Path, help="Optional destination for the safe metadata JSON.")
    args = parser.parse_args(argv)
    try:
        result = profile_file(args.jsonl)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
    except (OSError, UnicodeError, ValueError, TypeError):
        sys.stderr.write('{"error":"thread_profile_failed","message":"Cannot read or summarize the supplied JSONL."}\n')
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
