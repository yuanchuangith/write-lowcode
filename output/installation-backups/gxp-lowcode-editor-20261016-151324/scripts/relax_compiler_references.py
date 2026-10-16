"""Upgrade the installed native compiler bridge's static reference policy.

Only reference diagnostics become warnings. Structural validation and native
code generation are unchanged. Run again after rebuilding an older bridge.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def advisory_references(source: str) -> str:
    start = source.index("function validateReferences(")
    end = source.index("function validateDesign(", start)
    references = source[start:end]
    if "actionKeys, warnings)" in references:
        return source
    if references.count("fail(") != 5:
        raise ValueError("Unrecognized reference validator; bridge was not changed")
    references = references.replace("actionKeys)", "actionKeys, warnings)", 1)
    references = references.replace("fail(", "warnings.push(")
    updated = source[:start] + references + source[end:]
    replacements = {
        "  var nodeCount = 0;": "  var nodeCount = 0;\n  var warnings = [];",
        "validateReferences(design, _action, node, actionKeys);":
            "validateReferences(design, _action, node, actionKeys, warnings);",
        "      valid: true,": "      valid: true,\n      warnings,",
    }
    for before, after in replacements.items():
        if updated.count(before) != 1:
            raise ValueError("Unrecognized canvas validator; bridge was not changed")
        updated = updated.replace(before, after, 1)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge", type=Path)
    args = parser.parse_args()
    original = args.bridge.read_text(encoding="utf-8")
    updated = advisory_references(original)
    if updated != original:
        args.bridge.write_text(updated, encoding="utf-8", newline="\n")
    print("Static variable/action references are advisory; structural checks remain required.")


if __name__ == "__main__":
    main()
