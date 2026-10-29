"""Public input shapes; native preflight remains authoritative for business rules.

Typed dictionaries preserve omitted fields and JSON values exactly. Operation
names/required fields come from the same contract as native batch preflight.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BeforeValidator, ConfigDict, Field
from typing_extensions import NotRequired, Required, TypedDict


def _shape(name, fields, *, extra="forbid"):
    result = TypedDict(name, fields)
    result.__pydantic_config__ = ConfigDict(extra=extra, strict=True)
    # Resolve dynamically constructed definitions in this module, not in server.
    result.__module__ = __name__
    globals()[name] = result
    return result


Nonblank = Annotated[str, Field(min_length=1, pattern=r"\S")]
Nonempty = Annotated[str, Field(min_length=1, max_length=128)]
Offset = Annotated[int, Field(ge=0, strict=True)]
ValueBudget = Annotated[int, Field(ge=1024, le=65536, strict=True)]


def _exact_integer(value):
    if type(value) is not int:
        raise ValueError("schema_version must be the integer 1")
    return value


def _exact_true(value):
    if value is not True:
        raise ValueError("last_return_undefined must be the boolean true")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_exact_integer)]
ExactTrue = Annotated[Literal[True], BeforeValidator(_exact_true)]
Target = _shape("Target", {"group_key": Required[Nonblank], "node_key": Required[Nonblank]})
RuleTarget = _shape("RuleTarget", {
    "group_key": Required[Nonblank], "node_key": NotRequired[str], "field_path": NotRequired[str],
})
RowSelector = _shape("RowSelector", {
    "field": Required[Literal["id", "key"]], "value": Required[Union[Nonblank, int, float]],
})
EventContract = _shape("EventContract", {
    "component_type": Required[Nonblank], "event_name": Required[Nonblank],
})


def _optional_event_contract(value):
    # Native operations historically treat JSON-falsy values as no contract.
    # Do not normalize them to {}, null, or a boolean during MCP validation.
    if value is None or value is False or value == 0 or value == "" or value == [] or value == {}:
        return value
    from pydantic import TypeAdapter
    return TypeAdapter(EventContract).validate_python(value)


OptionalEventContract = Annotated[Any, BeforeValidator(_optional_event_contract), Field(
    description="Optional {component_type, event_name}. Omit for ordinary groups; legacy JSON-falsy values retain their original no-contract meaning.",
    json_schema_extra={"anyOf": [
        {"type": "object", "properties": {"component_type": {"type": "string"}, "event_name": {"type": "string"}},
         "required": ["component_type", "event_name"], "additionalProperties": False},
        {"enum": [None, False, 0, "", [], {}]},
    ]},
)]


def _optional_binding_text(value):
    if value is None or value is False or value == 0 or value == "":
        return value
    if type(value) is not str or not value.strip():
        raise ValueError("binding references must be nonblank text when supplied")
    return value


OptionalBindingText = Annotated[Any, BeforeValidator(_optional_binding_text), Field(
    json_schema_extra={"anyOf": [{"type": "string"}, {"enum": [None, False, 0]}]},
)]


def _optional_position(value):
    value = _optional_binding_text(value)
    if value and value not in ("before", "after", "append"):
        raise ValueError("position must be before, after, or append")
    return value
Declaration = _shape("Declaration", {
    "title": Required[Nonblank], "type": Required[Nonblank],
    "description": NotRequired[str], "required": NotRequired[bool],
})


def _operations():
    contract = json.loads(Path(__file__).with_name("operation_contracts.json").read_text(encoding="utf-8"))
    variants = []
    for name, spec in contract["operations"].items():
        fields = {}
        for key in [*spec["required"], *spec["optional"]]:
            kind = Nonblank
            if key == "operation":
                kind = Literal[name]
            elif key in {"expected_old_value", "value"}:
                kind = Any
            elif key == "item_key":
                kind = RowSelector
            elif key == "action_type":
                kind = Literal["js", "csharp"]
            elif key == "position":
                kind = Literal["before", "after", "append"] if name == "bind_component_event" else Literal["before", "after"]
            elif key in {"input_params", "output_params"}:
                kind = list[Declaration] if name == "create_action_group" else dict[str, Any]
            elif key == "event_contract":
                kind = OptionalEventContract
            elif key in {"group_description", "component_title"}:
                kind = str
            if name == "bind_component_event" and key in {"target_node_key", "anchor_node_key"}:
                kind = OptionalBindingText
            if name == "bind_component_event" and key == "position":
                kind = Annotated[Any, BeforeValidator(_optional_position), Field(
                    json_schema_extra={"anyOf": [{"enum": ["before", "after", "append"]}, {"enum": [None, False, 0, ""]}]},
                )]
            fields[key] = Required[kind] if key in spec["required"] else NotRequired[kind]
        label = "".join(word.title() for word in name.split("_"))
        extra = "forbid" if spec["strict_fields"] else "allow"
        variants.append(_shape(label, fields, extra=extra))
        if name in {"set_parameter", "set_array_item"}:
            # Exactly one old-value mode, including explicit expected null.
            guarded = {key: value for key, value in fields.items() if key != "expected_old_value"}
            guarded["guard_ref"] = Required[Nonblank]
            variants.append(_shape(label + "Guarded", guarded))
    return Union[tuple(variants)]


CanvasOperation = _operations()
Requirement = _shape("Requirement", {
    "id": Required[Nonblank], "text": Required[Nonblank], "source_text": Required[Nonblank],
    "targets": NotRequired[list[RuleTarget]],
})
FieldFixture = _shape("FieldFixture", {
    "value": NotRequired[Any], "text": NotRequired[Any], "selected_rows": NotRequired[list[Any]],
})
FieldExpectation = _shape("FieldExpectation", {"value": Required[Any]})
Environment = _shape("Environment", {
    "fields": NotRequired[dict[str, FieldFixture]], "child_fields": NotRequired[dict[str, FieldFixture]],
    "page_mode": NotRequired[Nonempty],
})
CallGroup = _shape("CallGroup", {
    "kind": Required[Literal["call_group"]], "group_key": Required[Nonblank], "args": NotRequired[list[Any]],
})
EmitEvent = _shape("EmitEvent", {
    "kind": Required[Literal["emit_event"]], "component_id": Required[Nonempty],
    "event_name": Required[Nonempty], "args": NotRequired[list[Any]], "inject_inner_inbiz": NotRequired[bool],
})
SetInputs = _shape("SetInputs", {"kind": Required[Literal["set_inputs"]], **{
    key: value for key, value in Environment.__annotations__.items()
}})
ReadMetadata = _shape("ReadMetadata", {"kind": Required[Literal["read_metadata"]]})
ScenarioStep = Union[CallGroup, EmitEvent, SetInputs, ReadMetadata]
MetadataExpectation = _shape("MetadataExpectation", {"key": Required[Nonempty], "value": Required[Any]})
Expectation = _shape("Expectation", {
    "fields": NotRequired[dict[str, FieldExpectation]],
    "metadata": NotRequired[list[MetadataExpectation] | None],
    "metadata_registrations": NotRequired[Offset], "last_return": NotRequired[Any],
    "last_return_undefined": NotRequired[ExactTrue], "call_counts": NotRequired[dict[str, Offset]],
    "call_order": NotRequired[Annotated[list[Nonblank], Field(max_length=1000)]],
})
ScenarioCase = _shape("ScenarioCase", {
    "id": Required[Nonempty], "title": NotRequired[str], "requirement_ids": NotRequired[list[Nonempty]],
    "environment": NotRequired[Environment],
    "steps": Required[Annotated[list[ScenarioStep], Field(min_length=1)]],
    "expect": NotRequired[Expectation],
})
Scenarios = _shape("Scenarios", {
    "schema_version": Required[SchemaVersion], "cases": Required[list[ScenarioCase]],
})
AssertionTarget = _shape("AssertionTarget", {
    "group_key": Required[Nonblank], "node_key": Required[Nonblank], "element_key": Required[Nonblank],
})
_assertion_fields = {
    "id": Required[Nonblank], "requirement_ids": Required[list[Nonblank]],
    "target": Required[AssertionTarget], "path": Required[Nonblank],
}
ValueAssertion = _shape("ValueAssertion", {
    **_assertion_fields, "operator": Required[Literal["equals", "contains"]], "value": Required[Any],
})
UnchangedAssertion = _shape("UnchangedAssertion", {
    **_assertion_fields, "operator": Required[Literal["unchanged"]],
})
ReviewSpec = _shape("ReviewSpec", {
    "schema_version": Required[SchemaVersion], "revision": Required[Nonblank],
    "requirements": Required[list[Requirement]],
    "scenarios": NotRequired[Scenarios], "scope_policy": NotRequired[Literal["strict", "manual_review"]],
    "change_source_text": NotRequired[Nonblank],
    "canvas_assertions": NotRequired[list[Union[ValueAssertion, UnchangedAssertion]]],
})
