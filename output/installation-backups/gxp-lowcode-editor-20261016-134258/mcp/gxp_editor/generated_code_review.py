"""Conservative display-only recognition of known compiler temporary names.

Never normalize executable/copyable code. A label is lexical evidence, not a
semantic verification. Only unchanged canvas elements enable their known rules.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict


_TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\s+|.",
    re.DOTALL,
)
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*\Z")


def canvas_context(before, after):
    old = {(g["key"], n["key"]): n for g in before.get("actionData", []) for n in g.get("data", [])}
    elements, actions = set(), set()
    for group in after.get("actionData", []):
        for node in group.get("data", []):
            previous = old.get((group["key"], node["key"]))
            if previous is None or any(previous.get(k) != node.get(k) for k in ("elementKey", "paramsValue", "depth")):
                continue
            elements.add(node["elementKey"])
            if node["elementKey"] == "CallAction":
                action = ((node.get("paramsValue") or {}).get("inputParams") or {}).get("actionName") or {}
                name = action.get("value") if isinstance(action, dict) else None
                if isinstance(name, str) and _IDENTIFIER.fullmatch(name):
                    actions.add(name)
    return frozenset(elements), frozenset(actions)


def _names(tokens, context):
    elements, actions = context
    # Keep literals/comments opaque. Unknown regex/interpolation syntax falls
    # back to normal differences rather than inferring executable identifiers.
    if any(token == "/" or token.startswith(('`', '$"', '"""')) for token in tokens):
        return {}
    code = [t for t in tokens if not t.isspace() and not t.startswith(("//", "/*"))]
    declarations = Counter(code[i + 1] for i, t in enumerate(code[:-1]) if t == "var")
    names = {}
    for i in range(len(code) - 5):
        if code[i] != "var" or code[i + 2] != "=":
            continue
        name, tail = code[i + 1], code[i + 3:i + 38]
        if declarations[name] != 1:
            continue
        family = None
        if "SetFormReadOnly" in elements and tail[:5] == ["inbiz", ".", "getFields", "(", ")"]:
            for prefix in ("setFormReadOnlyTableCom_", "setFormReadOnlyOtherCom_"):
                if re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{8}", name):
                    family = prefix
        elif "SelectModelData" in elements and re.fullmatch(r"result[A-Za-z0-9]{8}", name):
            if tail[:5] == ["await", "relatedAttributes", ".", "getSelectModelData", "("]:
                family = "SelectModelData.result"
                # The known generator passes its where variable as the last
                # argument; only that linked declaration can be normalized.
                for j in range(5, len(tail) - 2):
                    where = tail[j]
                    if re.fullmatch(r"where[A-Za-z0-9]{8}", where) and tail[j - 1] == "," and tail[j + 1:j + 3] == [")", ";"] and declarations[where] == 1:
                        names[where] = "SelectModelData.where"
                        break
        elif "CallAction" in elements:
            for action in actions:
                if re.fullmatch(re.escape(action) + r"_[A-Za-z0-9]{8}_Output", name):
                    if tail[:5] in (["await", "self", ".", action, "("], ["await", "this", ".", action, "("]):
                        family = "CallAction." + action
                    elif tail[:5] == ["await", "relatedAttributes", ".", "CallCsharpAction", "("]:
                        # Require the action key to appear as a literal argument.
                        if any(t in ("'" + action + "'", '"' + action + '"') for t in tail[:15]):
                            family = "CallAction." + action
        if family:
            names[name] = family
    # String/comment references and member names could have different meaning.
    opaque_names = {identifier for token in tokens if not _IDENTIFIER.fullmatch(token) for identifier in re.findall(r"[A-Za-z_$][\w$]*", token)}
    member_names = {token for i, token in enumerate(code) if i and code[i - 1] == "."}
    for name in opaque_names | member_names:
        names.pop(name, None)
    return names


def normalized_review_lines(before: str, after: str, context):
    """Return comparison-only lines with consistently paired temporary names."""
    if not context or not context[0]:
        return before.splitlines(), after.splitlines()
    tokens = [list(_TOKENS.findall(source)) for source in (before, after)]
    names = [_names(side, context) for side in tokens]
    families = [defaultdict(list), defaultdict(list)]
    for side in (0, 1):
        seen = set()
        for token in tokens[side]:
            if token in names[side] and token not in seen:
                families[side][names[side][token]].append(token)
                seen.add(token)
    mappings = [{}, {}]
    occurrences = [Counter(side) for side in tokens]
    for family, previous in families[0].items():
        current = families[1].get(family, [])
        if len(previous) != len(current):
            continue
        if any(old != new and (occurrences[0][old] != occurrences[1][new] or occurrences[1][old] or occurrences[0][new]) for old, new in zip(previous, current)):
            continue
        for index, (old, new) in enumerate(zip(previous, current)):
            if old == new:
                continue
            placeholder = f"<compiler-temp:{family}:{index}>"
            mappings[0][old], mappings[1][new] = placeholder, placeholder
    return tuple("".join(mappings[side].get(t, t) for t in tokens[side]).splitlines() for side in (0, 1))
