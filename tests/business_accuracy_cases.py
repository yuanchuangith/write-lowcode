"""Versioned, synthetic business expectations authored independently of results.

Sources are runner fixtures, not persisted platform drafts. Native compiler
mode/evidence checks live in the companion probe. Every mutation below is
executed; source text matching is never used as the accuracy verdict.
"""
from copy import deepcopy

CORPUS_VERSION = "business-accuracy-v1"


def group_source(body):
    return "function(inbiz){return {main:async function(){" + body + "}};}"


def case(identifier, fields, expected, *, mode=None, steps=None, expect=None):
    environment = {"fields": {name: {"value": value} for name, value in fields.items()}}
    if mode is not None:
        environment["page_mode"] = mode
    return {"id": identifier, "requirement_ids": ["business-rule"], "environment": environment,
            "steps": steps or [{"kind": "call_group", "group_key": "main"}],
            "expect": expect if expect is not None else {"fields": {name: {"value": value} for name, value in expected.items()}}}


def template(identifier, request, bodies, cases, *, complete_sources=False):
    return {"id": identifier, "source_text": request, "scenarios": {"schema_version": 1, "cases": cases},
            "versions": [{"id": name, "expected_status": "passed" if name == "correct" else "failed",
                          "source": body if complete_sources else group_source(body)} for name, body in bodies]}


BUSINESS_TEMPLATES = [
    template("page-mode", "Only initialize the target when adding a record.", [
        ("correct", 'if(inbiz.getPageMode()==="add") inbiz("result").value="new";'),
        ("always-write", 'inbiz("result").value="new";'),
        ("wrong-mode", 'if(inbiz.getPageMode()==="edit") inbiz("result").value="new";'),
    ], [case("add", {"result": "before"}, {"result": "new"}, mode="add"),
        case("edit", {"result": "before"}, {"result": "before"}, mode="edit")]),
    template("and-or", "Write only when adding AND the source is ready.", [
        ("correct", 'if(inbiz.getPageMode()==="add" && inbiz("source").value==="ready") inbiz("result").value="after";'),
        ("or-instead-of-and", 'if(inbiz.getPageMode()==="add" || inbiz("source").value==="ready") inbiz("result").value="after";'),
        ("inverted-condition", 'if(!(inbiz.getPageMode()==="add" && inbiz("source").value==="ready")) inbiz("result").value="after";'),
    ], [case("both", {"source": "ready", "result": "before"}, {"result": "after"}, mode="add"),
        case("mode-only", {"source": "", "result": "before"}, {"result": "before"}, mode="add"),
        case("source-only", {"source": "ready", "result": "before"}, {"result": "before"}, mode="edit")]),
    template("empty-array", "Null and empty selections produce no; a nonempty selection produces yes.", [
        ("correct", 'inbiz("result").value=(inbiz("items").value || []).length>0?"yes":"no";'),
        ("removed-null-guard", 'inbiz("result").value=inbiz("items").value.length>0?"yes":"no";'),
        ("empty-counts-as-selected", 'inbiz("result").value=(inbiz("items").value || []).length>=0?"yes":"no";'),
    ], [case("null", {"items": None, "result": "before"}, {"result": "no"}),
        case("empty", {"items": [], "result": "before"}, {"result": "no"}),
        case("selected", {"items": ["01"], "result": "before"}, {"result": "yes"})]),
    template("field-binding", "Copy the source into result and preserve the other field.", [
        ("correct", 'inbiz("result").value=inbiz("source").value;'),
        ("wrong-target", 'inbiz("other").value=inbiz("source").value;'),
        ("stale-constant", 'inbiz("result").value="stale";'),
    ], [case("copy", {"source": "ready", "result": "before", "other": "keep"}, {"result": "ready", "other": "keep"})]),
    template("branch-effects", "Only add mode may change result; preserve audit in every mode.", [
        ("correct", 'if(inbiz.getPageMode()==="add"){inbiz("result").value="after";}'),
        ("write-outside-branch", 'if(inbiz.getPageMode()==="add"){} inbiz("result").value="after";'),
        ("unrequested-audit-write", 'if(inbiz.getPageMode()==="add"){inbiz("result").value="after";} inbiz("audit").value="changed";'),
    ], [case("add", {"result": "before", "audit": "keep"}, {"result": "after", "audit": "keep"}, mode="add"),
        case("edit", {"result": "before", "audit": "keep"}, {"result": "before", "audit": "keep"}, mode="edit")]),
    template("callback-payload", "Only approve events copy the named payload value into result.", [
        ("correct", 'inbiz("button").configEvent("click",async function(evt){if(evt.id==="approve") inbiz("result").value=evt.value;});'),
        ("wrong-payload-key", 'inbiz("button").configEvent("click",async function(evt){if(evt.actionId==="approve") inbiz("result").value=evt.value;});'),
        ("wrong-payload-value", 'inbiz("button").configEvent("click",async function(evt){if(evt.id==="approve") inbiz("result").value=evt.label;});'),
    ], [case("approve", {"button": None, "result": "before"}, {"result": "agreed"}, steps=[
            {"kind": "call_group", "group_key": "main"},
            {"kind": "emit_event", "component_id": "button", "event_name": "click",
             "args": [{"id": "approve", "value": "agreed", "label": "wrong"}]}]),
        case("other", {"button": None, "result": "before"}, {"result": "before"}, steps=[
            {"kind": "call_group", "group_key": "main"},
            {"kind": "emit_event", "component_id": "button", "event_name": "click", "args": [{"id": "other", "value": "ignored"}]}])]),
    template("metadata-sequence", "Match events set metadata; a later other event clears the prior metadata.", [
        ("correct", 'inbiz("button").configEvent("click",async function(evt){inbiz.configEvent("getPageAddMetaData",()=>evt.id==="match"?[{key:"choice",value:evt.value}]:[]);});'),
        ("stale-metadata", 'inbiz("button").configEvent("click",async function(evt){if(evt.id==="match") inbiz.configEvent("getPageAddMetaData",()=>[{key:"choice",value:evt.value}]);});'),
        ("all-events-metadata", 'inbiz("button").configEvent("click",async function(evt){inbiz.configEvent("getPageAddMetaData",()=>[{key:"choice",value:evt.value}]);});'),
    ], [case("match", {"button": None}, {}, steps=[
            {"kind": "call_group", "group_key": "main"},
            {"kind": "emit_event", "component_id": "button", "event_name": "click", "args": [{"id": "match", "value": "A"}]}],
            expect={"metadata": [{"key": "choice", "value": "A"}], "metadata_registrations": 1}),
        case("match-then-other", {"button": None}, {}, steps=[
            {"kind": "call_group", "group_key": "main"},
            {"kind": "emit_event", "component_id": "button", "event_name": "click", "args": [{"id": "match", "value": "A"}]},
            {"kind": "emit_event", "component_id": "button", "event_name": "click", "args": [{"id": "other", "value": "B"}]},
            {"kind": "read_metadata"}], expect={"metadata": [], "metadata_registrations": 2})]),
    template("output-structure", "Return returnValue null and rows with the exact string identifier 01.", [
        ("correct", 'return {returnValue:null,rows:[{id:"01"}]};'),
        ("numeric-identifier", 'return {returnValue:null,rows:[{id:1}]};'),
        ("wrong-wrapper", 'return {data:{returnValue:null,rows:[{id:"01"}]}};'),
    ], [case("return", {}, {}, expect={"last_return": {"returnValue": None, "rows": [{"id": "01"}]}})]),
]

UNSUPPORTED_CASES = [
    {"id": "unsupported-sdk", "source": group_source('try{inbiz("result").save()}catch(error){} inbiz("result").value="after";')},
    {"id": "legacy-eval", "source": group_source('inbiz("result").value="after"; eval("1");')},
    {"id": "nondeterministic-date", "source": group_source('inbiz("result").value=Date.now();')},
]


def attribution_cases():
    source = 'function(inbiz){const self={main:async function(){return true;},other:async function(){return true;},callback:async function(){return true;}};return self;}'
    rows = [
        ("group-observed", [{"group_key": "main"}], "main", "passed"),
        ("unrelated-group", [{"group_key": "main"}], "other", "unverified"),
        ("skipped-node", [{"group_key": "main", "node_key": "assign"}], "main", "unverified"),
        ("field-without-node", [{"group_key": "main", "field_path": "paramsValue/inputParams/value"}], "main", "unverified"),
        ("csharp-target", [{"group_key": "backend"}], "main", "unverified"),
        ("unknown-target", [{"group_key": "unknown"}], "main", "unverified"),
        ("missing-node", [{"group_key": "main", "node_key": "missing"}], "main", "unverified"),
        ("configuration-only", [{"group_key": "main", "node_key": "assign", "field_path": "paramsValue/inputParams/value"}], "main", "unverified"),
        ("unlinked-rule", [{"group_key": "main"}], "main", "unverified"),
    ]
    cases = []
    for identifier, targets, group, expected in rows:
        fixture = case(identifier, {}, {}, steps=[{"kind": "call_group", "group_key": group}], expect={"last_return": True})
        if identifier == "unlinked-rule":
            fixture["requirement_ids"] = []
        cases.append({"id": identifier, "targets": targets, "source": source, "case": fixture, "expected_status": expected})
    event = 'function(inbiz){const self={main:async function(){inbiz("button").configEvent("click",self.callback);},callback:async function(){return true;},other:async function(){return true;}};return self;}'
    for identifier, candidate in (("event-not-group", event), ("overwritten-event", event.replace('self.callback);', 'self.callback);inbiz("button").configEvent("click",self.other);'))):
        fixture = case(identifier, {"button": None}, {}, steps=[{"kind": "call_group", "group_key": "main"},
            {"kind": "emit_event", "component_id": "button", "event_name": "click"}], expect={"last_return": True})
        cases.append({"id": identifier, "targets": [{"group_key": "callback"}], "source": candidate,
                      "case": fixture, "expected_status": "unverified"})
    failed = case("failure-remains-blocking", {}, {}, expect={"last_return": True})
    cases.append({"id": failed["id"], "targets": [{"group_key": "backend"}], "source": group_source('return false;'),
                  "case": failed, "expected_status": "failed"})
    return deepcopy(cases)
