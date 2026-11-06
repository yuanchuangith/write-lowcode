from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.profile_codex_thread import main, profile_file, profile_records


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def record(second, record_type, **payload):
    return {"timestamp": (BASE + timedelta(seconds=second)).isoformat(), "type": record_type, "payload": payload}


def usage(second, response_id, input_tokens, output_tokens):
    return record(second, "token_usage_record", response_id=response_id,
                  usage={"input_tokens": input_tokens, "output_tokens": output_tokens,
                         "total_tokens": input_tokens + output_tokens, "cached_input_tokens": input_tokens // 2})


def mcp(second, identity, duration, detail="full"):
    return record(second, "event_msg", type="item_completed", item={
        "type": "McpToolCall", "id": identity, "server": "test-server", "tool": "inspect_edit_context",
        "arguments": {"response_detail": detail, "private_path": "C:/DO-NOT-EMIT/token-private"},
        "result": {"structuredContent": {"total_elapsed_ms": duration * 1000, "secret": "DO-NOT-EMIT"},
                   "content": [{"type": "text", "text": "DO-NOT-EMIT business configuration"}]},
        "duration": {"secs": duration, "nanos": 0}})


class CodexThreadProfileTests(unittest.TestCase):
    def test_response_and_completed_call_ids_are_deduplicated_and_token_count_is_not_added(self):
        rows = [usage(1, "r1", 100, 10), usage(2, "r1", 100, 10), usage(3, "r2", 300, 20),
                record(4, "event_msg", type="token_count", info={"total_token_usage": {"input_tokens": 999999}}),
                mcp(5, "m1", 2), mcp(5, "m1", 2)]
        result = profile_records(rows)
        self.assertEqual(result["usage"]["unique_responses"], 2)
        self.assertEqual(result["usage"]["totals"]["input_tokens"], 400)
        self.assertEqual(result["usage"]["totals"]["output_tokens"], 30)
        self.assertEqual(result["usage"]["first_input_tokens"], 100)
        self.assertEqual(result["usage"]["last_input_tokens"], 300)
        self.assertEqual(result["mcp_total"]["unique_calls"], 1)
        self.assertEqual(result["mcp_total"]["duration_sum_seconds_not_wall_time"], 2)

    def test_active_idle_and_nested_parallel_intervals_use_unions(self):
        rows = [record(0, "event_msg", type="task_started", turn_id="t1", model_context_window=1000),
                record(0, "turn_context", turn_id="t1", model="gpt-test", effort="high"),
                record(5, "response_item", type="custom_tool_call", name="exec", call_id="c1", input="DO-NOT-EMIT"),
                mcp(8, "m1", 6),
                record(10, "response_item", type="custom_tool_call_output", call_id="c1", output="DO-NOT-EMIT"),
                record(20, "event_msg", type="task_complete", turn_id="t1", time_to_first_token_ms=100),
                record(30, "event_msg", type="task_started", turn_id="t2", model_context_window=1000),
                record(40, "event_msg", type="turn_aborted", turn_id="t2")]
        result = profile_records(rows)
        timeline = result["timeline"]
        self.assertEqual(timeline["wall_seconds"], 40)
        self.assertEqual(timeline["closed_active_span_seconds"], 30)
        self.assertEqual(timeline["outside_closed_active_span_seconds"], 10)
        self.assertEqual(timeline["observed_outer_first_return_wait_seconds"], 5)
        self.assertEqual(timeline["mcp_duration_union_within_active_seconds"], 6)
        self.assertEqual(timeline["all_observed_tool_intervals_union_within_active_seconds"], 8)
        self.assertEqual(timeline["active_unattributed_seconds"], 22)
        self.assertEqual(result["models_by_turn_context"], {"gpt-test": 1})

    def test_simultaneous_returns_do_not_count_the_same_unknown_gap_twice(self):
        rows = [record(0, "event_msg", type="task_started", turn_id="t1"),
                record(0, "turn_context", turn_id="t1", model="gpt-test", effort="high"),
                record(3, "response_item", type="function_call", name="wait", call_id="a", arguments="private"),
                record(4, "response_item", type="function_call", name="wait", call_id="b", arguments="private"),
                record(10, "response_item", type="function_call_output", call_id="a", output="private"),
                record(12, "response_item", type="function_call_output", call_id="b", output="private"),
                record(15, "response_item", type="message", role="assistant", content="private"),
                record(20, "event_msg", type="task_complete", turn_id="t1")]
        result = profile_records(rows)
        gap = result["tool_return_to_next_assistant_action_unknown_gaps"]
        self.assertEqual(gap["duration_seconds"]["count"], 1)
        self.assertEqual(gap["longest_seconds"], [3])
        self.assertEqual(result["timeline"]["observed_outer_first_return_wait_seconds"], 9)

    def test_output_contains_no_body_values_paths_identifiers_or_credentials(self):
        rows = [mcp(1, "DO-NOT-EMIT-call-id", 1),
                record(2, "response_item", type="message", role="user", content="DO-NOT-EMIT credential"),
                record(3, "session_meta", cwd="C:/DO-NOT-EMIT", base_instructions="DO-NOT-EMIT"),
                usage(4, "DO-NOT-EMIT-response-id", 100, 10)]
        encoded = json.dumps(profile_records(rows), ensure_ascii=False)
        self.assertNotIn("DO-NOT-EMIT", encoded)
        self.assertNotIn("private_path", encoded)
        self.assertIn("input_json_bytes", encoded)
        self.assertIn("result_json_bytes", encoded)
        self.assertIn("unknown", encoded)

    def test_full_summary_and_parameter_projection_are_counted_without_argument_values(self):
        first = mcp(1, "m1", 1)
        second = mcp(2, "m2", 1, "summary")
        second["payload"]["item"]["arguments"]["parameter_paths"] = {"secret": ["/private"]}
        result = profile_records([first, second])
        tool = result["mcp_tools"][0]
        self.assertEqual(tool["response_detail"], {"full": 1, "summary": 1})
        self.assertEqual(tool["parameter_paths_calls"], 1)
        self.assertEqual(tool["server_elapsed_ms"]["sum"], 2000)

    def test_identical_reads_and_explicit_compaction_events_are_metadata_only(self):
        first = mcp(1, "m1", 1)
        second = mcp(2, "m2", 1)
        result = profile_records([first, second, record(3, "compacted", summary="DO-NOT-EMIT")])
        tool = result["mcp_tools"][0]
        self.assertEqual(tool["identical_argument_repeat_groups"], 1)
        self.assertEqual(tool["extra_calls_with_identical_arguments"], 1)
        self.assertEqual(result["explicit_compaction_events"], 1)
        self.assertNotIn("DO-NOT-EMIT", json.dumps(result))

    def test_file_command_reads_only_explicit_input_and_redacts_failures(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sensitive-thread-path.jsonl"
            source.write_text(json.dumps(usage(1, "r1", 20, 1)) + "\ninvalid\n", encoding="utf-8")
            output = root / "report.json"
            self.assertEqual(main([str(source), "--output", str(output)]), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["invalid_jsonl_lines"], 1)
            self.assertEqual(result["usage"]["unique_responses"], 1)
            self.assertNotIn(str(root), output.read_text(encoding="utf-8"))
            self.assertEqual(profile_file(source)["source_bytes"], source.stat().st_size)


if __name__ == "__main__":
    unittest.main()
