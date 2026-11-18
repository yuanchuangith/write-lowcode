"""Native annotation location and candidate-bound readability report tests."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from tests.helpers import baseline
from gxp_editor.readability_review import build_readability, validate_readability
from gxp_editor.review_targets import resolve_review_targets
from gxp_editor.logic_review import normalize_review_spec, LogicReviewError


def node(key, kind, depth=(), **inputs):
    return {"key": key, "elementKey": kind, "depth": list(depth), "paramsValue": {"inputParams": inputs}}


def canvas(*nodes):
    return {"actionData": [{"key": "group", "actionType": "js", "data": list(nodes)}], "globalVariables": []}


def report(before, after, blocks=()):
    with patch("gxp_editor.readability_review._analyzer_findings", return_value=[]):
        return build_readability({"comment_blocks": blocks}, before, after)


def test_required_note_is_immediately_before_target_in_same_branch():
    block = {"id": "reason", "requirement_id": "task", "group_key": "group", "anchor_node_key": "work"}
    begin, work = node("if", "IfCondition"), node("work", "DataFilter", ["if"])
    note = lambda depth: node("reason", "CustomExt", depth, content="说明为何匹配时才执行")
    original = canvas(begin, work)
    candidate = canvas(begin, note(["if"]), work)
    assert report(original, candidate, [block])["findings"] == []
    for bad in (canvas(note([]), begin, work), canvas(begin, note([]), work), canvas(begin, work, note(["if"]))):
        result = report(original, bad, [block])
        assert result["findings"][0]["rule"] == "required_native_comment"
        tampered = deepcopy(result)
        tampered["findings"] = []
        with pytest.raises(ValueError, match="画布证据"):
            validate_readability(tampered, {"comment_blocks": [block]}, bad, original)


def test_else_note_is_first_child_and_group_note_is_first_root_node():
    begin = node("if", "IfCondition")
    fallback = node("fallback", "Else", ["if"])
    work = node("work", "DataFilter", ["fallback"])
    note = node("reason", "CustomExt", ["fallback"], content="其余情况保留已有记录")
    header = node("header", "CustomExt", content="先校验再修改")
    original = canvas(begin, fallback, work)
    blocks = [{"id": "entry", "requirement_id": "rule", "group_key": "group"},
              {"id": "fallback", "requirement_id": "rule", "group_key": "group", "anchor_node_key": "fallback"}]
    assert report(original, canvas(header, begin, fallback, note, work), blocks)["findings"] == []
    assert report(original, canvas(header, begin, fallback, work, note), blocks)["findings"][0]["id"] == "comment:fallback"


@pytest.mark.parametrize("unsafe", [None, {}, " ", "note\rnext", "note\nnext", "note\x85next", "note\u2028next", "note\u2029next"])
def test_new_invalid_note_blocks_even_without_requirement_but_legacy_is_ignored(unsafe):
    before = canvas()
    bad = canvas(node("reason", "CustomExt", content=unsafe))
    result = report(before, bad)
    assert result["findings"][0]["rule"] == "invalid_native_comment"
    assert report(bad, bad)["findings"] == []


def test_automatic_note_only_for_new_or_changed_multi_guard_critical_write():
    outer = node("outer", "IfCondition")
    inner = node("inner", "IfCondition", ["outer"])
    write = node("write", "UpdateData", ["outer", "inner"])
    action = canvas(outer, inner, write)
    assert report(action, action)["findings"] == []
    before = canvas(outer, node("write", "UpdateData", ["outer"]))
    missing = report(before, action)
    assert missing["automatic_comment_blocks"] == [{"group_key": "group", "anchor_node_key": "inner"}]
    assert missing["findings"][0]["rule"] == "critical_write_comment"
    note = node("reason", "CustomExt", ["outer"], content="关联记录通过校验后才写入")
    assert report(before, canvas(outer, note, inner, write))["findings"] == []


def test_comment_spec_checks_scope_and_resolves_existing_native_aliases():
    spec = normalize_review_spec({"schema_version": 1, "revision": "1", "requirements": [{
        "id": "rule", "text": "Add a note", "source_text": "Add a note", "targets": [{"group_key": "@new", "node_key": "@target"}],
    }], "comment_blocks": [{"id": "header", "requirement_id": "rule", "group_key": "@new", "anchor_node_key": "@target"}]})
    bindings = {"groups": {"new": "group"}, "nodes": {"target": {"group_key": "group", "node_key": "work"}}}
    assert resolve_review_targets(spec, bindings, canvas(node("work", "DataFilter")))["comment_blocks"][0] == {
        "id": "header", "requirement_id": "rule", "group_key": "group", "anchor_node_key": "work"}
    spec["comment_blocks"][0]["anchor_node_key"] = "@other"
    with pytest.raises(LogicReviewError, match="需求"):
        normalize_review_spec(spec)
