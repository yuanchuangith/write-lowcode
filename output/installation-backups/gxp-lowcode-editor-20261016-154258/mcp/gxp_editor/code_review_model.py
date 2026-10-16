"""Toolkit-independent, display-only alignment of complete generated code."""
from difflib import SequenceMatcher

from .generated_code_review import normalized_review_lines


def aligned_code_rows(before: str, after: str, *, context=None) -> tuple[list[dict], list[int]]:
    old, new = before.splitlines(), after.splitlines()
    normalized_old, normalized_new = normalized_review_lines(before, after, context)
    rows, changes = [], []
    for kind, start, end, new_start, new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        generated_only = kind == "replace" and end - start == new_end - new_start and normalized_old[start:end] == normalized_new[new_start:new_end]
        if kind != "equal":
            changes.append(len(rows) + 1)
        for offset in range(max(end - start, new_end - new_start)):
            old_index, new_index = start + offset, new_start + offset
            rows.append({"kind": kind, "generated_only": generated_only,
                         "before_number": old_index + 1 if old_index < end else None,
                         "after_number": new_index + 1 if new_index < new_end else None,
                         "before": old[old_index] if old_index < end else "",
                         "after": new[new_index] if new_index < new_end else ""})
    if before != after and not changes:
        changes.append(max(1, len(rows)))
    return rows, changes
