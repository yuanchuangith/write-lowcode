from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from gxp_form_editor.common import FormError
from gxp_form_editor.schema import apply_operations, find_components, inspect_component, restore_approved_changes


def page():
    return {"id": "page-1", "content": {
        "form": {"model": {"id": "main-model", "name": "order"}, "other": [1, 2]},
        "actions": {"keep": "orchestration"},
        "schema": {"type": "object", "properties": {
            "grid": {"type": "void", "x-component": "FormGrid", "properties": {
                "input": {"type": "string", "name": "unchanged", "title": "Old title",
                          "x-component": "EformInput", "required": False,
                          "x-component-props": {"customValue": "", "defaultValueType": "custom", "options": ["a", "b"]}},
                "span": {"type": "void", "x-component": "FormGrid.GridColumn",
                         "x-component-props": {"gridSpan": 6}},
            }},
            "select": {"type": "string", "x-component": "EformDynamicList", "x-component-props": {
                "dataCenter": {"value": "old", "lastModificationTime": "v0"},
                "modelkey": "old", "modelname": "old_model", "showconfig": {"list": ["old"]}}},
            "dictionary": {"type": "string", "x-component": "EformStaticList", "x-component-props": {
                "dataGroupId": "group-old", "staticDataList": "group-old", "defaultValue": []}},
            "table": {"type": "void", "x-component": "AdvancedTables", "x-component-props": {"dataFilter": False}},
        }}}}


def catalog():
    return {"schema_version": 1, "component_version": "test-components", "source_fingerprint": "source-sha256",
            "platform": "web", "components": {
        "EformInput": {"properties": {
            "/title": {"type": "string", "operation": "set_property"},
            "/required": {"type": "boolean", "operation": "set_property"},
            "/x-read-only": {"type": "boolean", "operation": "set_property"},
            "/x-component-props/customValue": {"type": "string", "operation": "set_default"},
            "/x-component-props/options": {"type": "array", "operation": "set_property",
                                            "setter_contract": "json-schema-v1", "items": {"type": "string"}},
        }, "adapters": {"set_default": {"contract": "native-default-v1", "complete": True,
                                           "paths": ["/x-component-props/customValue"], "derived_paths": []}}},
        "FormGrid.GridColumn": {"properties": {
            "/x-component-props/gridSpan": {"type": "integer", "minimum": 1, "maximum": 12, "operation": "set_layout"}}},
        "EformDynamicList": {"properties": {
            "/x-component-props/dataCenter": {"type": "object", "operation": "set_binding"},
            "/x-component-props/modelkey": {"type": "string"},
            "/x-component-props/modelname": {"type": "string"},
            "/x-component-props/showconfig": {"type": "object"},
        }, "adapters": {"set_binding": {"contract": "dynamic-list-dataset-v1", "complete": True,
                    "paths": ["/x-component-props/dataCenter"],
                    "derived_paths": ["/x-component-props/modelkey", "/x-component-props/modelname", "/x-component-props/showconfig"],
                    "modelkey_path": "/x-component-props/modelkey", "modelname_path": "/x-component-props/modelname",
                    "showconfig_path": "/x-component-props/showconfig", "query_field_ids": {"order.id": "stable-1", "order.title": "stable-2"}}}},
        "EformStaticList": {"properties": {
            "/x-component-props/dataGroupId": {"type": "string", "operation": "set_binding"},
            "/x-component-props/staticDataList": {"type": "string"},
            "/x-component-props/defaultValue": {"type": "array", "items": {"type": "string"}, "operation": "set_default"},
        }, "adapters": {
            "set_binding": {"contract": "native-dictionary-v1", "complete": True,
                            "paths": ["/x-component-props/dataGroupId"], "derived_paths": ["/x-component-props/staticDataList"],
                            "static_data_path": "/x-component-props/staticDataList"},
            "set_default": {"contract": "native-dictionary-default-v1", "complete": True,
                            "paths": ["/x-component-props/defaultValue"], "dictionary_id_path": "/x-component-props/dataGroupId"}}},
        "AdvancedTables": {"properties": {"/x-component-props/dataFilter": {"type": "boolean", "operation": "set_filter"}},
                           "adapters": {"set_filter": {"contract": "native-filter-toggle-v1", "complete": True,
                                                       "paths": ["/x-component-props/dataFilter"]}}},
    }}


def resources():
    return {"datasets": {"dataset-1": {"id": "dataset-1", "primaryKey": "id", "model": "order", "name": "Orders",
            "group": "sales", "lastModificationTime": "v1", "configModel": {"queryFields": [
                {"id": "field-1", "name": "order.id", "tableName": "order", "type": "BIGINT", "columnName": "ID", "description": "Key"},
                {"id": "field-2", "name": "order.title", "tableName": "order", "type": "VARCHAR", "columnName": "Title", "description": "Name"},
            ]}}}, "dictionaries": {
                "group-1": {"id": "group-1", "version": "v1", "items": [{"Value": "A", "Text": "Alpha"}, {"Value": "B", "Text": "Beta"}]},
                "group-old": {"id": "group-old", "version": "v0", "items": [{"Value": "old", "Text": "Old"}]}}}


def operation(component_id="input", component_type="EformInput", path="/required", old=False, value=True, kind="set_property", **extra):
    return {"operation": kind, "component_id": component_id, "expected_component_type": component_type,
            "path": path, "expected_old_value": old, "value": value, **extra}


def binding():
    return operation("select", "EformDynamicList", "/x-component-props/dataCenter",
                     {"value": "old", "lastModificationTime": "v0"}, {"id": "dataset-1", "version": "v1"}, "set_binding")


class FormSchemaTests(unittest.TestCase):
    def assert_blocked(self, code, operations, *, cat=None, refs=None, document=None):
        with self.assertRaises(FormError) as caught:
            apply_operations(document or page(), operations, cat or catalog(), refs)
        self.assertEqual(caught.exception.code, code)

    def test_enumeration_and_inspection_return_native_component_ids_and_copies(self):
        document = page()
        rows = find_components(document)
        self.assertEqual([row["component_id"] for row in rows], ["grid", "input", "span", "select", "dictionary", "table"])
        self.assertEqual(rows[1]["parent_component_id"], "grid")
        inspected = inspect_component(document, "input")
        inspected["schema"]["title"] = "mutated copy"
        self.assertEqual(inspect_component(document, "input")["schema"]["title"], "Old title")

    def test_duplicate_native_ids_are_ambiguous(self):
        document = page()
        document["content"]["schema"]["properties"]["input"] = {"x-component": "EformInput"}
        with self.assertRaises(FormError) as caught:
            inspect_component(document, "input")
        self.assertEqual(caught.exception.code, "component_not_unique")

    def test_simple_edit_preserves_model_orchestration_identity_and_input(self):
        document = page()
        original = deepcopy(document)
        result = apply_operations(document, [operation()], catalog())
        self.assertEqual(document, original)
        self.assertEqual(result["page"]["content"]["form"], original["content"]["form"])
        self.assertEqual(result["page"]["content"]["actions"], original["content"]["actions"])
        edited = inspect_component(result["page"], "input")["schema"]
        self.assertEqual((edited["name"], edited["type"]), ("unchanged", "string"))
        self.assertTrue(edited["required"])
        self.assertTrue(result["validation"]["schema_roundtrip"])
        self.assertEqual(len(result["changes"]), 1)
        self.assertFalse(result["changes"][0]["derived"])

    def test_strict_boolean_guard_and_value_types(self):
        self.assert_blocked("old_value_mismatch", [operation(old=0)])
        self.assert_blocked("property_type_mismatch", [operation(value=1)])

    def test_sequential_guards_include_previous_batch_edits(self):
        result = apply_operations(page(), [operation(), operation(old=True, value=False)], catalog())
        self.assertEqual(len(result["changes"]), 2)
        self.assertFalse(inspect_component(result["page"], "input")["schema"]["required"])
        self.assert_blocked("old_value_mismatch", [operation(), operation(old=False, value=False)])

    def test_absent_and_null_have_distinct_guards(self):
        self.assert_blocked("old_value_mismatch", [operation(path="/x-read-only", old=None)])
        result = apply_operations(page(), [operation(path="/x-read-only", old=None, expected_old_exists=False)], catalog())
        self.assertFalse(result["changes"][0]["old_exists"])

    def test_noop_has_no_changes(self):
        result = apply_operations(page(), [operation(value=False)], catalog())
        self.assertEqual(result["changes"], [])

    def test_unknown_component_property_and_operation_are_rejected(self):
        self.assert_blocked("component_not_unique", [operation(component_id="new")])
        self.assert_blocked("property_readonly", [operation(path="/unregistered", old=None, expected_old_exists=False)])
        self.assert_blocked("invalid_operation", [operation(kind="add_component")])
        self.assert_blocked("component_type_mismatch", [operation(component_type="Other")])

    def test_structural_and_executable_paths_are_blocked_even_if_catalog_advertises_them(self):
        for path in ("/properties", "/children", "/name", "/id", "/type", "/x-component", "/x-reactions", "/x-component-props", "/x-component-props/0"):
            with self.subTest(path=path):
                cat = catalog()
                cat["components"]["EformInput"]["properties"][path] = {"type": "string", "operation": "set_property"}
                self.assert_blocked("array_index_forbidden" if path.endswith("/0") else "protected_path",
                                    [operation(path=path, old=None, expected_old_exists=False)], cat=cat)

    def test_array_replacement_is_guarded_as_a_whole(self):
        result = apply_operations(page(), [operation(path="/x-component-props/options", old=["a", "b"], value=["b", "c"])], catalog())
        self.assertEqual(result["changes"][0]["old_value"], ["a", "b"])
        self.assert_blocked("old_value_mismatch", [operation(path="/x-component-props/options", old=["a"], value=["c"])])

    def test_unknown_or_incomplete_complex_setters_are_readonly(self):
        cat = catalog()
        cat["components"]["EformInput"]["properties"]["/title"]["setter"] = "UnrecognizedCustomSetter"
        self.assert_blocked("unknown_setter", [operation(path="/title", old="Old title", value="New")], cat=cat)
        cat = catalog()
        del cat["components"]["EformInput"]["properties"]["/x-component-props/options"]["items"]
        self.assert_blocked("unknown_setter", [operation(path="/x-component-props/options", old=["a", "b"], value=["c"])], cat=cat)

    def test_complex_native_shapes_reject_unregistered_nested_keys(self):
        document = page()
        document["content"]["schema"]["properties"]["grid"]["properties"]["input"]["x-component-props"]["style"] = {"color": "red"}
        cat = catalog()
        cat["components"]["EformInput"]["properties"]["/x-component-props/style"] = {
            "type": "object", "properties": {"color": {"type": "string"}}, "additionalProperties": False,
            "setter_contract": "json-schema-v1", "operation": "set_layout"}
        op = operation(path="/x-component-props/style", old={"color": "red"}, value={"color": "blue", "unknown": True}, kind="set_layout")
        self.assert_blocked("property_contract_mismatch", [op], document=document, cat=cat)
        op["value"] = {"color": "blue"}
        self.assertEqual(apply_operations(document, [op], cat)["changes"][0]["value"], {"color": "blue"})

    def test_json_pointer_escaping_uses_exact_property_names(self):
        cat = catalog()
        cat["components"]["EformInput"]["properties"]["/x-component-props/a~1b~0c"] = {"type": "string", "operation": "set_property"}
        op = operation(path="/x-component-props/a~1b~0c", old=None, value="text", expected_old_exists=False)
        result = apply_operations(page(), [op], cat)
        self.assertEqual(inspect_component(result["page"], "input")["schema"]["x-component-props"]["a/b~c"], "text")
        self.assert_blocked("invalid_path", [{**op, "path": "/x-component-props/a~2b"}], cat=cat)

    def test_platform_cannot_be_overridden_and_wap_properties_are_separate(self):
        cat = catalog()
        cat["components"]["EformInput"]["properties"]["/title"]["platforms"] = ["wap"]
        op = operation(path="/title", old="Old title", value="New")
        self.assert_blocked("platform_mismatch", [op], cat=cat)
        self.assert_blocked("platform_mismatch", [{**op, "platform": "wap"}], cat=cat)
        cat["platform"] = "wap"
        result = apply_operations(page(), [op], cat)
        self.assertEqual(result["changes"][0]["value"], "New")

    def test_layout_limits_enforce_native_grid_span(self):
        for value in (0, 13, 1.0, True):
            with self.subTest(value=value):
                self.assert_blocked("property_type_mismatch" if type(value) is not int else "property_range_mismatch",
                    [operation("span", "FormGrid.GridColumn", "/x-component-props/gridSpan", 6, value, "set_layout")])
        result = apply_operations(page(), [operation("span", "FormGrid.GridColumn", "/x-component-props/gridSpan", 6, 12, "set_layout")], catalog())
        self.assertEqual(result["changes"][0]["value"], 12)

    def test_native_defaults_require_complete_adapter(self):
        op = operation(path="/x-component-props/customValue", old="", value="Hello", kind="set_default")
        result = apply_operations(page(), [op], catalog())
        self.assertEqual(result["changes"][0]["value"], "Hello")
        cat = catalog()
        cat["components"]["EformInput"]["adapters"]["set_default"]["complete"] = False
        self.assert_blocked("unknown_setter", [op], cat=cat)

    def test_dataset_binding_rebuilds_all_native_linked_configuration(self):
        document = page()
        result = apply_operations(document, [binding()], catalog(), resources())
        self.assertEqual(len(result["changes"]), 4)
        self.assertEqual(sum(c["derived"] for c in result["changes"]), 3)
        props = inspect_component(result["page"], "select")["schema"]["x-component-props"]
        self.assertEqual((props["modelkey"], props["modelname"]), ("dataset-1", "order"))
        self.assertEqual(props["dataCenter"]["queryField"][0]["dbType"], "number")
        config = props["showconfig"]
        self.assertEqual(config["list"], [])
        self.assertEqual((config["storageField"], config["selectName"], config["dataCenterId"]), ("id", "id", "dataset-1"))
        self.assertEqual([(row["Value"], row["dbName"], row["id"]) for row in config["allList"]],
                         [("id", "order.id", "stable-1"), ("title", "order.title", "stable-2")])
        self.assertEqual(document, page())

    def test_dataset_join_aliases_and_field_types_are_preserved(self):
        refs = resources()
        refs["datasets"]["dataset-1"]["configModel"]["queryFields"].append(
            {"id": "field-3", "name": "customer.name", "tableName": "customer", "type": "VARCHAR", "alias": "customer_name"})
        cat = catalog()
        cat["components"]["EformDynamicList"]["adapters"]["set_binding"]["query_field_ids"]["customer.name"] = "stable-3"
        result = apply_operations(page(), [binding()], cat, refs)
        row = inspect_component(result["page"], "select")["schema"]["x-component-props"]["showconfig"]["allList"][-1]
        self.assertEqual((row["Value"], row["YsField"], row["dbType"]), ("customer_name", "customer$name", "text"))

    def test_native_dataset_serialization_omits_undefined_optional_fields(self):
        result = apply_operations(page(), [binding()], catalog(), resources())
        props = inspect_component(result["page"], "select")["schema"]["x-component-props"]
        field = props["dataCenter"]["queryField"][0]
        self.assertNotIn("format", field)
        self.assertNotIn("AnotherName", field)
        self.assertNotIn("format", props["showconfig"]["allList"][0])

    def test_duplicate_native_field_ids_and_result_aliases_are_invalid(self):
        for duplicate in ("id", "alias"):
            refs = resources()
            fields = refs["datasets"]["dataset-1"]["configModel"]["queryFields"]
            if duplicate == "id":
                fields[1]["id"] = fields[0]["id"]
            else:
                fields[1]["alias"] = "id"
            self.assert_blocked("invalid_reference_metadata", [binding()], refs=refs)

    def test_reference_version_id_and_types_are_verified(self):
        stale = binding()
        stale["value"]["version"] = "old-version"
        self.assert_blocked("reference_version_mismatch", [stale], refs=resources())
        self.assert_blocked("reference_not_found", [binding()], refs={})
        for defect in ("type", "primaryKey", "id"):
            refs = resources()
            if defect == "type":
                refs["datasets"]["dataset-1"]["configModel"]["queryFields"][0]["type"] = "UNKNOWN"
            elif defect == "primaryKey":
                refs["datasets"]["dataset-1"]["primaryKey"] = "missing"
            else:
                refs["datasets"]["dataset-1"]["configModel"]["queryFields"][0]["id"] = ""
            self.assert_blocked("invalid_reference_metadata", [binding()], refs=refs)

    def test_dataset_cannot_change_undeclared_derived_paths(self):
        cat = catalog()
        cat["components"]["EformDynamicList"]["adapters"]["set_binding"]["derived_paths"].remove("/x-component-props/showconfig")
        self.assert_blocked("undeclared_derived_change", [binding()], cat=cat, refs=resources())

    def test_unchanged_dataset_version_preserves_user_showconfig(self):
        document = page()
        node = document["content"]["schema"]["properties"]["select"]
        node["x-component-props"]["dataCenter"]["lastModificationTime"] = "v1"
        op = binding()
        op["expected_old_value"]["lastModificationTime"] = "v1"
        result = apply_operations(document, [op], catalog(), resources())
        config = inspect_component(result["page"], "select")["schema"]["x-component-props"]["showconfig"]
        self.assertEqual(config, {"list": ["old"]})

    def test_dictionary_binding_matches_native_select_group_effect(self):
        op = operation("dictionary", "EformStaticList", "/x-component-props/dataGroupId", "group-old",
                       {"id": "group-1", "version": "v1"}, "set_binding")
        result = apply_operations(page(), [op], catalog(), resources())
        props = inspect_component(result["page"], "dictionary")["schema"]["x-component-props"]
        self.assertEqual((props["dataGroupId"], props["staticDataList"]), ("group-1", "group-1"))
        self.assertEqual(len(result["changes"]), 2)

    def test_dictionary_defaults_must_exist_in_current_dictionary(self):
        op = operation("dictionary", "EformStaticList", "/x-component-props/defaultValue", [], ["absent"], "set_default")
        self.assert_blocked("invalid_reference", [op], refs=resources())
        op["value"] = ["old"]
        result = apply_operations(page(), [op], catalog(), resources())
        self.assertEqual(result["changes"][0]["value"], ["old"])

    def test_only_known_native_filter_contract_is_accepted(self):
        op = operation("table", "AdvancedTables", "/x-component-props/dataFilter", False, True, "set_filter")
        result = apply_operations(page(), [op], catalog())
        self.assertTrue(result["changes"][0]["value"])
        cat = catalog()
        cat["components"]["AdvancedTables"]["adapters"]["set_filter"]["contract"] = "guessed-condition-ast"
        self.assert_blocked("unknown_setter", [op], cat=cat)

    def test_failure_is_atomic_and_does_not_mutate_inputs(self):
        document, cat = page(), catalog()
        original, original_catalog = deepcopy(document), deepcopy(cat)
        self.assert_blocked("old_value_mismatch", [operation(), operation(old="wrong")], document=document, cat=cat)
        self.assertEqual(document, original)
        self.assertEqual(cat, original_catalog)

    def test_invalid_json_and_unbound_catalog_are_rejected(self):
        self.assert_blocked("invalid_json", [operation(value=float("nan"))])
        cat = catalog()
        cat["source_fingerprint"] = ""
        self.assert_blocked("invalid_catalog", [operation()], cat=cat)

    def test_restore_inverts_explicit_and_native_derived_changes_exactly(self):
        document, cat = page(), catalog()
        result = apply_operations(document, [operation(), binding()], cat, resources())
        restored = restore_approved_changes(result["page"], result["changes"], cat)
        self.assertEqual(restored["page"], document)
        self.assertEqual(len(restored["changes"]), 5)
        self.assertTrue(restored["validation"]["schema_roundtrip"])

    def test_restore_deletes_only_new_properties_and_originally_created_parents(self):
        document, cat = page(), catalog()
        cat["components"]["EformInput"]["properties"]["/x-decorator-props/style/width"] = {"type": "string", "operation": "set_layout"}
        op = operation(path="/x-decorator-props/style/width", old=None, value="10px", kind="set_layout", expected_old_exists=False)
        result = apply_operations(document, [op], cat)
        self.assertEqual(result["changes"][0]["created_parents"], ["/x-decorator-props", "/x-decorator-props/style"])
        self.assertEqual(restore_approved_changes(result["page"], result["changes"], cat)["page"], document)
        document["content"]["schema"]["properties"]["grid"]["properties"]["input"]["x-decorator-props"] = {"style": {}}
        result = apply_operations(document, [op], cat)
        self.assertEqual(result["changes"][0]["created_parents"], [])
        self.assertEqual(restore_approved_changes(result["page"], result["changes"], cat)["page"], document)

    def test_restore_replays_sequential_guards_in_reverse_order(self):
        document, cat = page(), catalog()
        result = apply_operations(document, [operation(), operation(old=True, value=False)], cat)
        self.assertEqual(restore_approved_changes(result["page"], result["changes"], cat)["page"], document)

    def test_restore_refuses_drift_unknown_catalog_and_structural_changes(self):
        document, cat = page(), catalog()
        result = apply_operations(document, [operation()], cat)
        edited = deepcopy(result["page"])
        edited["content"]["schema"]["properties"]["grid"]["properties"]["input"]["required"] = False
        with self.assertRaises(FormError) as caught:
            restore_approved_changes(edited, result["changes"], cat)
        self.assertEqual(caught.exception.code, "restore_guard_mismatch")
        bad_changes = deepcopy(result["changes"])
        bad_changes[0]["path"] = "/properties"
        with self.assertRaises(FormError) as caught:
            restore_approved_changes(result["page"], bad_changes, cat)
        self.assertEqual(caught.exception.code, "protected_path")
        del cat["components"]["EformInput"]["properties"]["/required"]
        with self.assertRaises(FormError) as caught:
            restore_approved_changes(result["page"], result["changes"], cat)
        self.assertEqual(caught.exception.code, "property_readonly")


if __name__ == "__main__":
    unittest.main()
