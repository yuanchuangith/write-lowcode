"""Offline GXP ActionDesign compiler for Write."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter

from build_write_compiler import build
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.config import (
    CompilerSource, ConfigurationError, DEFAULT_COMPILER_BRIDGE, DEFAULT_FRONTEND_REPO,
    RuntimeReferenceConfig, load_runtime_references,
)
from gxp_editor.runtime_package import RuntimeResourceError
from gxp_editor.runtime_validation import RuntimeCsharpValidator, RuntimeValidationError


_DIAGNOSTIC = re.compile(
    r"^(?:(?P<origin>.+?)\s*:\s*)?(?P<severity>error|warning)\s+"
    r"(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>.*)$",
    re.IGNORECASE,
)
_LOCATION = re.compile(r"^(?P<file>.+)\((?P<line>\d+)(?:,(?P<column>\d+))?(?:,\d+,\d+)?\)$")
_PROJECT_SUFFIX = re.compile(r"\s+\[[^\r\n]+\.csproj\]$", re.IGNORECASE)
_CHECK_EXIT = {"passed": 0, "skipped": 0, "failed": 1, "unavailable": 2}


def csharp_diagnostics(lines: list[str], source_path: Path | None) -> list[dict]:
    diagnostics = []
    seen = set()
    for text in lines:
        match = _DIAGNOSTIC.fullmatch(text.strip())
        if not match:
            continue
        entry = {
            "severity": match["severity"].lower(),
            "code": match["code"].upper(),
            "message": _PROJECT_SUFFIX.sub("", match["message"]),
        }
        location = _LOCATION.fullmatch(match["origin"] or "")
        if location:
            file = location["file"]
            if re.split(r"[/\\]", file)[-1].lower() == "candidate.cs":
                file = str(source_path) if source_path is not None else "Candidate.cs"
            entry.update(file=file, line=int(location["line"]),
                         column=int(location["column"]) if location["column"] else None)
        key = tuple(entry.items())
        if key not in seen:
            seen.add(key)
            diagnostics.append(entry)
    return sorted(diagnostics, key=lambda entry: entry["severity"] != "error")


def write_new_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def check_canvas(args, compiler: CompilerBridge, design: dict) -> dict:
    started = perf_counter()
    has_csharp = any(group.get("actionType") == "csharp" for group in design["actionData"])
    result = compiler.compile_design(design) if has_csharp else compiler.validate_design(design)
    timings = [{"stage": "canvas.generate" if has_csharp else "canvas.validate",
                "elapsed_ms": round((perf_counter() - started) * 1000, 3)}]
    source = result.get("csharp_code", "")
    csharp = {"status": "skipped", "reason": "no_csharp_source", "diagnostics": []}
    if source.strip():
        emitted = args.emit_csharp.absolute() if args.emit_csharp is not None else None
        if emitted is not None:
            if args.output is not None and emitted.resolve() == args.output.resolve():
                raise ValueError("--output and --emit-csharp must name different files")
            write_new_text(emitted, source)
            emitted = emitted.resolve()
        validator = None
        csharp_started = perf_counter()
        try:
            if args.references is not None:
                config = RuntimeReferenceConfig.from_dict({
                    "directory": str(args.references),
                    "target_framework": args.target_framework or "net6.0",
                })
            else:
                config = load_runtime_references()
                if args.target_framework is not None:
                    config = RuntimeReferenceConfig.from_dict({
                        "directory": config.directory, "source_mode": config.source_mode,
                        "dotnet_command": config.dotnet_command, "target_framework": args.target_framework,
                    })
            validator = RuntimeCsharpValidator(config)
            evidence = validator.validate(design["code"], source)
            if evidence.get("compilation_succeeded") is not True:
                raise RuntimeValidationError("The .NET validator did not confirm compilation")
            csharp = {
                "status": "passed",
                "diagnostics": csharp_diagnostics(evidence.get("diagnostics", []), emitted),
                **{key: evidence[key] for key in ("target_framework", "source_mode", "reference_conflicts") if key in evidence},
            }
        except ConfigurationError as exc:
            csharp = {"status": "unavailable", "failure_kind": "configuration_error",
                      "diagnostics": [], "message": f"{exc}. Provide --references with the directory of GXP DLLs."}
        except (RuntimeValidationError, OSError) as exc:
            kind = getattr(exc, "code", "checker_unavailable")
            diagnostics = csharp_diagnostics(getattr(exc, "diagnostics", []), emitted)
            csharp = {"status": "failed" if kind == "compilation_failed" else "unavailable",
                      "failure_kind": kind, "diagnostics": diagnostics}
            if kind != "compilation_failed" or not diagnostics:
                csharp["message"] = str(exc)
        if emitted is not None:
            csharp["source_path"] = str(emitted)
        if validator is not None:
            timings.extend(validator.last_performance)
        timings.append({"stage": "runtime.total", "elapsed_ms": round((perf_counter() - csharp_started) * 1000, 3)})
    report = {"ok": _CHECK_EXIT[csharp["status"]] == 0, "validation": result["validation"], "csharp": csharp}
    if args.timings:
        report["timings"] = timings
        report["total_elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    return report


def read_json(path: Path):
    def reject_constant(value):
        raise ValueError(f"{path}: {value} is not valid JSON")

    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)


def read_canvas(path: Path) -> dict:
    canvas = read_json(path)
    if not isinstance(canvas, dict) or any(
        not isinstance(canvas.get(field), list) for field in ("actionData", "globalVariables")
    ):
        raise ValueError(f"{path}: expected a GXP canvas with actionData and globalVariables arrays")
    for index, group in enumerate(canvas["actionData"]):
        if not isinstance(group, dict) or not isinstance(group.get("data"), list):
            raise ValueError(f"{path}: actionData[{index}] must be an object with a data array")
        for row, node in enumerate(group["data"]):
            params = node.get("paramsValue") if isinstance(node, dict) else None
            if not isinstance(params, dict) or any(
                key in params and not isinstance(params[key], dict) for key in ("inputParams", "outputParams")
            ):
                raise ValueError(f"{path}: actionData[{index}].data[{row}] has invalid paramsValue")
    return canvas


def execute(args) -> dict:
    if args.command == "build":
        return {"ok": True, "bridge_path": str(build(Path(args.frontend)))}
    compiler = CompilerBridge(CompilerSource(args.frontend, args.bridge))
    if args.command == "registry":
        return compiler.describe_registry()
    design = read_canvas(args.canvas)
    if args.command == "validate":
        return compiler.validate_design(design)
    if args.command == "compile":
        return compiler.compile_design(design)
    if args.command == "check":
        return check_canvas(args, compiler, design)
    operations = read_json(args.operations)
    if not isinstance(operations, list):
        raise ValueError(f"{args.operations}: expected a JSON array of Write canvas operations")
    result = compiler.compile_patch(design, operations)
    result.pop("_generation_context", None)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gxp-compiler", description=__doc__)
    parser.add_argument("--frontend", default=DEFAULT_FRONTEND_REPO, help="GXP frontend Git root")
    parser.add_argument("--bridge", default=DEFAULT_COMPILER_BRIDGE, help="auto, managed, or a path inside the frontend")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("build", "build and activate the local compiler"),
        ("registry", "list native ActionDesign elements"),
        ("validate", "check a GXP canvas JSON file"),
        ("compile", "generate JavaScript, C# source and relation data"),
        ("check", "validate the canvas and compile its generated C# using .NET"),
        ("patch", "apply Write operations and generate a local candidate"),
    ):
        sub = commands.add_parser(command, help=help_text)
        if command in {"validate", "compile", "check", "patch"}:
            sub.add_argument("canvas", type=Path)
        if command == "patch":
            sub.add_argument("operations", type=Path)
        if command == "check":
            sub.add_argument("--references", type=Path, help="directory containing GXP reference DLLs")
            sub.add_argument("--target-framework", help="override the .NET target framework")
            sub.add_argument("--emit-csharp", type=Path, help="write the generated C# to a new file")
            sub.add_argument("--timings", action="store_true", help="report canvas, .NET and restore timings")
        if command != "build":
            sub.add_argument("-o", "--output", type=Path, help="write JSON to a new file instead of stdout")

    args = parser.parse_args(argv)
    try:
        result = execute(args)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if getattr(args, "output", None):
            write_new_text(args.output, output)
        else:
            sys.stdout.buffer.write(output.encode("utf-8"))
        return _CHECK_EXIT[result["csharp"]["status"]] if args.command == "check" else 0
    except (CompilerError, RuntimeResourceError, ValueError, OSError, subprocess.SubprocessError) as exc:
        error = {"ok": False, "error": str(exc)}
        if getattr(exc, "details", None):
            error["details"] = exc.details
        sys.stderr.buffer.write((json.dumps(error, ensure_ascii=False) + "\n").encode("utf-8"))
        return 2 if args.command == "check" else 1


if __name__ == "__main__":
    raise SystemExit(main())
