"""Display-only alignment and safe rich captions for the component canvas."""
from __future__ import annotations

from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any


class _Caption(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[bool] = []
        self.runs: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "br":
            self.handle_data(" ")
        elif tag not in {"img", "hr", "input", "meta", "link"}:
            classes = set(attributes.get("class", "").replace("\\", "").replace('"', "").replace("'", "").split())
            colored = (tag in {"a", "code"} or bool(classes & {"var", "action-link", "variable", "parameter"})
                       or "color" in attributes or "color:" in attributes.get("style", "").replace(" ", "").lower())
            self.stack.append(colored or any(self.stack))

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        # Captions are text only; HTML is never loaded or executed.
        if data:
            self.runs.append((data.replace("\n", " "), any(self.stack)))


def caption_runs(node: dict[str, Any]) -> list[tuple[str, bool]]:
    parser = _Caption()
    parser.feed(str(node.get("description") or ""))
    return parser.runs or [("双击查看完整元件配置", False)]


def review_groups(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    old = {group["key"]: group for group in before.get("actionData", [])}
    new = {group["key"]: group for group in after.get("actionData", [])}
    groups = [{"key": key, "title": (new.get(key) or old[key]).get("title") or key,
               "before": old.get(key, {}).get("data", []), "after": new.get(key, {}).get("data", [])}
              for key in dict.fromkeys([*new, *old])]
    if before.get("globalVariables") != after.get("globalVariables"):
        def variables(data):
            return [{"key": "globalVariables", "title": "全局变量", "elementKey": "globalVariables",
                     "description": "点击查看全局变量配置差异", "paramsValue": {
                         "inputParams": {"globalVariables": data.get("globalVariables", [])}}}]
        groups.append({"key": "globalVariables", "title": "全局变量",
                       "before": variables(before), "after": variables(after)})
    return groups


def aligned_rows(group: dict[str, Any], collapsed: set[str]) -> list[dict[str, Any]]:
    """Preserve each side's order and actual line numbers, including moves."""
    sides = {side: [{"node": node, "line": index + 1} for index, node in enumerate(group[side])]
             for side in ("before", "after")}
    matcher = SequenceMatcher(None, [item["node"]["key"] for item in sides["before"]],
                              [item["node"]["key"] for item in sides["after"]], autojunk=False)
    rows = []
    for _, a, b, c, d in matcher.get_opcodes():
        left, right = sides["before"][a:b], sides["after"][c:d]
        for index in range(max(len(left), len(right))):
            row = {side: items[index] if index < len(items) else None
                   for side, items in (("before", left), ("after", right))}
            for side, item in row.items():
                if item and collapsed.intersection(item["node"].get("depth") or []):
                    row[side] = None
            if any(row.values()):
                rows.append(row)
    return rows
