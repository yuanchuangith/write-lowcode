"""Offline service-chain benchmark: synthetic repository, real compiler, temp state.

No credentials/config are loaded, no business database is opened, no generated
business code is executed, and approval launch is stubbed. Counters distinguish
simulated repository calls from actual local SQLite statements and subprocesses.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
from itertools import count
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch


def benchmark(args):
    implementation = args.implementation_root.resolve()
    sys.path.insert(0, str(implementation / "mcp"))
    from gxp_editor import compiler as compiler_module
    from gxp_editor import service as service_module
    from gxp_editor import transaction as transaction_module
    from gxp_editor.approval import transaction_binding_hash
    from gxp_editor.compiler import CompilerBridge
    from gxp_editor.config import DatabaseConfig, EditorConfig, RuntimeReferenceConfig
    from gxp_editor.hashing import design_content_hash, sha256_json
    from gxp_editor.logic_review import plan_binding_hash
    from gxp_editor.runtime_validation import RuntimeCsharpValidator
    from gxp_editor.service import EditorService
    from gxp_editor.state import StateStore
    from gxp_editor.telemetry import stage, traced_tool

    counts = Counter()
    config = EditorConfig(
        database=DatabaseConfig("127.0.0.1", 1, "synthetic", "unused", expected_grants_sha256="c" * 64),
        frontend_repo=str(args.frontend.resolve()), compiler_bridge="dist/action-design-compiler/bridge.js",
        runtime_references=RuntimeReferenceConfig(str(args.frontend.resolve()), target_framework="net9.0"))

    class Bridge(CompilerBridge):
        def _source_key(self):
            counts["source_scans"] += 1
            return super()._source_key()

        def _call(self, payload, **kwargs):
            counts["compiler_command." + payload["command"]] += 1
            return super()._call(payload, **kwargs)

    class State(StateStore):
        @contextmanager
        def _connection(self):
            with super()._connection() as connection:
                def count_sql(sql):
                    counts["local_sqlite_statements"] += 1
                connection.set_trace_callback(count_sql)
                yield connection

    class Database:
        def status(self):
            counts["fake_database.status"] += 1
            return {"connected": True, "synthetic": True}

    class Repository:
        def __init__(self, draft):
            self.draft = draft

        def index_status(self):
            counts["fake_repository.index_status"] += 1
            return {"draft": {"ready": True}, "relations": {"ready": True}}

        def grant_status(self, **kwargs):
            counts["fake_repository.grant_status"] += 1
            return {"safe": True, "dedicated_safe": True, "shared_account_warning": False,
                    "privileged_shared_override": False, "fingerprint_configured": True,
                    "fingerprint_matches": True, "grants_sha256": "c" * 64}

        def load_draft_by_identifier(self, identifier):
            counts["fake_repository.load_draft"] += 1
            return deepcopy(self.draft)

        def load_draft_by_id(self, identifier):
            return self.load_draft_by_identifier(identifier)

        def load_relations(self, design_id):
            counts["fake_repository.load_relations"] += 1
            return deepcopy(self.draft["relations"])

    record = lambda value: {"paramTypes": "custom", "value": value, "code": value,
                            "label": "value", "dataType": "number"}
    empty = {"actionData": [{"key": f"g{i}", "title": f"Group {i}", "actionType": "js", "data": []}
                            for i in range(6)], "globalVariables": [], "code": "ServiceBenchmark"}
    initial = [{"operation": "append_node", "action_group_key": f"g{i}", "element_key": "SetVariable",
        "input_params": {"variableType": "number", "variableValue": record("1")},
        "output_params": {"variableName": {**record(f"answer{i}"), "label": f"answer{i}"}}}
        for i in range(6)]
    template = CompilerBridge(config).compile_patch(empty, initial)["design"]
    rows, candidate_hashes, evidence_hashes = [], {}, {}
    original_run = subprocess.run

    def subprocess_count(command, *positional, **kwargs):
        if isinstance(command, (list, tuple)) and command:
            counts["process." + Path(str(command[0])).stem] += 1
        return original_run(command, *positional, **kwargs)

    @traced_tool
    def request(service, tool, *positional, **kwargs):
        with stage("service." + tool):
            return getattr(service, tool)(*positional, **kwargs)

    def measure(service, tool, positional, kwargs, metadata):
        counts.clear()
        response = request(service, tool, *positional, **kwargs)
        if response.get("blocked") or response.get("error"):
            raise RuntimeError(f"Synthetic {tool} failed: {response.get('error_code', 'blocked')}")
        # Persist only metadata, never operation values, reports or generated code.
        rows.append({**metadata, "tool": tool, "total_elapsed_ms": response["total_elapsed_ms"],
            "response_bytes": len(json.dumps(response, ensure_ascii=False).encode("utf-8")),
            "response_sha256": sha256_json({key: value for key, value in response.items()
                if key not in {"trace_id", "timings", "total_elapsed_ms"}}),
            "counts": dict(counts), "timings": response["timings"]})
        return response

    with tempfile.TemporaryDirectory(prefix="gxp-service-benchmark-") as temporary, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": temporary}))
        stack.enter_context(patch.object(service_module, "approval_secret", return_value="synthetic-offline-key"))
        stack.enter_context(patch("gxp_editor.config.approval_secret", return_value="synthetic-offline-key"))
        stack.enter_context(patch("gxp_editor.handoff_import.discover_handoffs", return_value=[]))
        stack.enter_context(patch.object(service_module, "load_config", return_value=config))
        stack.enter_context(patch.object(service_module, "EditorDatabase", return_value=Database()))
        stack.enter_context(patch.object(service_module, "CompilerBridge", Bridge))
        stack.enter_context(patch("subprocess.run", subprocess_count))
        stack.enter_context(patch.object(EditorService, "_try_launch_approval", return_value={
            "approval_window_opened": False, "approval_launch_stubbed": True}))
        for size in args.sizes:
            design = deepcopy(template)
            for group in design["actionData"]:
                group["data"] = []
            for index in range(size):
                node = deepcopy(template["actionData"][0]["data"][0])
                node["key"] = f"n{index}"
                node["paramsValue"]["outputParams"]["variableName"].update(code=f"answer{index}", value=f"answer{index}", label=f"answer{index}")
                design["actionData"][index % 6]["data"].append(node)
            node = design["actionData"][0]["data"][0]
            operation = {"operation": "set_parameter", "action_group_key": "g0", "target_node_key": node["key"],
                "expected_element_key": "SetVariable", "path": "/inputParams/variableValue",
                "expected_old_value": deepcopy(node["paramsValue"]["inputParams"]["variableValue"]), "value": record("2")}
            saved = CompilerBridge(config).compile_patch(design, [operation])
            canvas = {key: saved["design"][key] for key in ("actionData", "globalVariables")}
            draft = {"id": "synthetic-design", "ref_id": "synthetic-ref", "code": "ServiceBenchmark",
                "name": "Synthetic offline service benchmark", "data_json": canvas,
                "data": json.dumps(canvas, ensure_ascii=False), "js_code": saved["js_code"],
                "csharp_code": saved["csharp_code"], "is_publish": 0, "isDeleted": 0,
                "last_modification_time": "2026-10-26 00:00:00.000", "creatorId": "offline", "lastModifierId": "offline",
                "department": "offline", "metadata": {"action_code": "ServiceBenchmark", "action_name": "Synthetic"},
                "relations": [{"id": f"r{index}", "action_data_id": key, "design_id": "synthetic-design",
                    "isDeleted": 0, "data": value} for index, (key, value) in enumerate(saved["relation_data"].items())]}
            draft["content_hash"] = design_content_hash(draft)
            first = canvas["actionData"][0]["data"][0]
            change = {**operation, "expected_old_value": deepcopy(first["paramsValue"]["inputParams"]["variableValue"]), "value": record("3")}
            hashes, evidence = [], []
            repository = Repository(draft)
            with patch.object(service_module, "EditorRepository", return_value=repository):
                for repeat in range(args.rounds):
                    # Alternating order reduces position bias; each trial has isolated state.
                    for cache in (("cold", "warm") if repeat % 2 == 0 else ("warm", "cold")):
                        if cache == "cold":
                            with compiler_module._CACHE_LOCK:
                                compiler_module._REGISTRY_CACHE.clear()
                                compiler_module._CONTRACT_CACHE.clear()
                        print(f"size={size} round={repeat + 1} cache={cache}", file=sys.stderr, flush=True)
                        trial_root = Path(temporary) / f"{size}-{repeat}-{cache}"
                        identifiers = count(1)

                        class FixedTransactionTime(datetime):
                            @classmethod
                            def now(cls, tz=None):
                                return cls(2026, 10, 27, 12, tzinfo=tz)

                        # Only synthetic identity/time is fixed, so full public
                        # responses and actual approval-binding hashes can be
                        # compared without dropping candidate or SQL evidence.
                        with patch.dict(os.environ, {"LOCALAPPDATA": str(trial_root)}), \
                             patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(identifiers))), \
                             patch.object(transaction_module, "datetime", FixedTransactionTime):
                            service = object.__new__(EditorService)
                            service.state = State(trial_root / "state.sqlite3")
                            service._conversation_id = service.state.create_conversation()["conversation_id"]
                            metadata = {"nodes": size, "round": repeat + 1, "initial_cache": cache}
                            begun = measure(service, "begin_edit_session", ("ServiceBenchmark",), {}, metadata)
                            session_id = begun["session_id"]
                            measure(service, "find_edit_nodes", (session_id,), {"limit": 30}, {**metadata, "page": "first"})
                            measure(service, "find_edit_nodes", (session_id,), {"offset": max(0, size - 30), "limit": 30}, {**metadata, "page": "last"})
                            measure(service, "inspect_edit_context", (session_id, [{"group_key": "g0", "node_key": first["key"]}]), {}, metadata)
                            spec = {"schema_version": 1, "revision": "offline", "scope_policy": "manual_review", "requirements": [],
                                    "scenarios": {"schema_version": 1, "cases": []}}
                            planned = measure(service, "plan_canvas_patch", (session_id, [change]), {"review_spec": spec, "response_detail": "summary"}, metadata)
                            plan = service.state.get_plan(planned["plan_id"])
                            artifact = json.loads(Path(plan["artifact_path"]).read_text(encoding="utf-8"))
                            hashes.append(artifact["hashes"])
                            prepared = measure(service, "prepare_write", (planned["plan_id"],), {"response_detail": "summary"}, metadata)
                            transaction = service.state.get_transaction(prepared["transaction_id"])
                            approval = json.loads(Path(transaction["artifact_path"]).read_text(encoding="utf-8"))
                            if plan["new_hash"] != plan_binding_hash(artifact) or approval["transaction_hash"] != transaction_binding_hash(approval):
                                raise RuntimeError("Plan or approval binding failed its own verification")
                            evidence.append({"plan_artifact_sha256": sha256_json(artifact),
                                "plan_binding_sha256": plan["new_hash"],
                                "approval_artifact_sha256": sha256_json(approval),
                                "approval_binding_sha256": approval["transaction_hash"]})
            if not all(item == hashes[0] for item in hashes):
                raise RuntimeError("Synthetic candidate hash instability")
            candidate_hashes[str(size)] = hashes[0]
            if not all(item == evidence[0] for item in evidence):
                raise RuntimeError("Synthetic plan/approval evidence instability")
            evidence_hashes[str(size)] = evidence[0]
    summary = []
    for size in args.sizes:
        for cache in ("cold", "warm"):
            for tool, page in (("begin_edit_session", None), ("find_edit_nodes", "first"), ("find_edit_nodes", "last"),
                               ("inspect_edit_context", None), ("plan_canvas_patch", None), ("prepare_write", None)):
                selected = [row for row in rows if row["nodes"] == size and row["initial_cache"] == cache and row["tool"] == tool and row.get("page") == page]
                summary.append({"nodes": size, "initial_cache": cache, "tool": tool, "page": page,
                    "n": len(selected), "median_ms": round(statistics.median(row["total_elapsed_ms"] for row in selected), 3),
                    "min_ms": min(row["total_elapsed_ms"] for row in selected), "max_ms": max(row["total_elapsed_ms"] for row in selected),
                    "median_source_scans": statistics.median(row["counts"].get("source_scans", 0) for row in selected)})
    return {"scope": "synthetic offline service pipeline; real compiler; fake business repository/database; local temp SQLite and backups; approval launch stubbed; blank C#; no business execution",
        "implementation_root": str(implementation), "rounds": args.rounds, "sizes": args.sizes,
        "cache_semantics": "cold clears registry/contract cache before begin only; later tools in that pipeline are warm; Node identity and filesystem OS cache not reset",
        "unmeasured": ["real database/network", "model input tokens", "model latency", "real UI approval", "business C# compatibility"],
        "determinism": "synthetic Python UUIDs and transaction timestamps fixed; public response hashes exclude only telemetry fields",
        "candidate_hashes_stable": True, "candidate_hashes": candidate_hashes,
        "evidence_hashes_stable": True, "evidence_hashes": evidence_hashes, "summary": summary, "samples": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--sizes", type=int, nargs="+", default=[6, 1000])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1 or any(size < 6 for size in args.sizes):
        parser.error("rounds >= 1 and sizes >= 6 are required")
    result = benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
