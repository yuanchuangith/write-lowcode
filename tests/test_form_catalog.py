from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from gxp_form_editor.catalog import extract_native_catalog, main
from gxp_form_editor.common import FormError
from gxp_form_editor.schema import apply_operations, inspect_component
from tests.test_form_schema import binding, operation, page, resources


def native_base(properties=None, *, style=None):
    # Actual createInbizSchema/createBaseSchema/validator hierarchy, from
    # gxp2.components/src/basic/MicoEditor/materials/common/Field/shared.ts.
    # The form/tab/collapse nodes are VOID, while x-component-props is OBJECT.
    return {"type": "object", "properties": {"form": {"type": "void", "x-component": "FormTab", "properties": {
        "attribute": {"type": "void", "x-component": "FormTab.TabPane", "properties": {
            "name": {"type": "string", "x-component": "Input"},
            "base": {"type": "void", "x-component": "CollapseItem", "properties": {
                "title": {"type": "object", "x-component": "StatusInput"},
                "x-pattern": {"type": "string", "enum": ["editable", "readOnly", ""], "x-component": "Select"},
                "x-component-props": {"type": "object", "properties": properties or {}},
            }},
            "validator": {"type": "void", "x-component": "CollapseItem", "properties": {
                "required": {"type": "boolean", "x-component": "Switch"}}},
        }},
        "style": {"type": "void", "x-component": "FormTab.TabPane", "properties": style or {}},
    }}}}


def exported_inputs():
    return {"EformInput": {"designerProps": {"propsSchema": native_base({
        "customValue": {"type": "string", "x-component": "Input", "x-reactions": {
            "dependencies": [".defaultValueType"], "fulfill": {"state": {"hidden": "{{$deps[0] !== 'custom'}}"}}}},
        "defaultValueType": {"type": "string", "enum": ["system", "custom"], "x-component": "Radio.Group"},
        "systemDefaultValue": {"type": "string", "enum": ["System_Var_LoginUserID"], "x-component": "Select"},
        "placeholder": {"type": "string", "x-component": "MultilingualInput"},
    }, style={"component-layout-group": {"type": "void", "x-component": "CollapseItem", "properties": {
        "x-decorator-props": {"type": "object", "properties": {
            "style.gridColumn": {"type": "string", "x-component": "Input"},
        }}}}})}}}


def extract(native=None, platform="web"):
    return extract_native_catalog(native or exported_inputs(), "1.1.65", "source-fingerprint", platform)


class FormCatalogTests(unittest.TestCase):
    def test_real_void_group_shape_maps_to_component_storage_and_is_executable(self):
        native = exported_inputs()
        original = deepcopy(native)
        cat = extract(native)
        props = cat["components"]["EformInput"]["properties"]
        self.assertIn("/required", props)
        self.assertIn("/x-component-props/customValue", props)
        self.assertNotIn("/form/attribute/base/x-component-props/customValue", props)
        self.assertEqual(props["/x-component-props/customValue"]["operation"], "set_default")
        self.assertFalse(props["/name"]["writable"])
        result = apply_operations(page(), [operation(), operation(path="/x-component-props/customValue", old="", value="Hello", kind="set_default")], cat)
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual(native, original)

    def test_formily_dotted_storage_paths_preserve_nested_style(self):
        cat = extract()
        descriptor = cat["components"]["EformInput"]["properties"]["/x-decorator-props/style/gridColumn"]
        self.assertEqual(descriptor["operation"], "set_layout")
        op = operation(path="/x-decorator-props/style/gridColumn", old=None, value="span 6", kind="set_layout", expected_old_exists=False)
        result = apply_operations(page(), [op], cat)
        self.assertEqual(inspect_component(result["page"], "input")["schema"]["x-decorator-props"]["style"]["gridColumn"], "span 6")

    def test_status_input_uses_source_reviewed_closed_value_object(self):
        document = page()
        document["content"]["schema"]["properties"]["grid"]["properties"]["input"]["title"] = {"status": True, "value": "Old"}
        op = operation(path="/title", old={"status": True, "value": "Old"}, value={"status": False, "value": "New"})
        result = apply_operations(document, [op], extract())
        self.assertEqual(result["changes"][0]["value"], {"status": False, "value": "New"})
        op["value"]["extra"] = "not native"
        with self.assertRaises(FormError) as caught:
            apply_operations(document, [op], extract())
        self.assertEqual(caught.exception.code, "property_contract_mismatch")

    def test_types_enums_limits_and_platform_come_from_native_metadata(self):
        native = {"FormGrid.GridColumn": native_base({"gridSpan": {"type": "number", "x-component": "NumberPicker",
                        "x-component-props": {"min": 1, "max": 12, "precision": 0}, "platform": "web"}})}
        cat = extract(native)
        prop = cat["components"]["FormGrid.GridColumn"]["properties"]["/x-component-props/gridSpan"]
        self.assertEqual((prop["type"], prop["minimum"], prop["maximum"], prop["platforms"]), ("integer", 1, 12, ["web"]))
        self.assertFalse(extract(native, "wap")["components"]["FormGrid.GridColumn"]["properties"]["/x-component-props/gridSpan"]["writable"])
        self.assertEqual(extract()["components"]["EformInput"]["properties"]["/x-pattern"]["enum"], ["editable", "readOnly", ""])

    def test_unknown_setter_function_loss_constraints_and_reaction_remain_readonly(self):
        definitions = {
            "unknown": {"type": "string", "x-component": "MysterySetter"},
            "lost_function": {"type": "object"},
            "pattern": {"type": "string", "x-component": "Input", "pattern": "[A-Z]+"},
            "mutating": {"type": "string", "x-component": "Input", "x-reactions": {"fulfill": {"run": "$form.setValuesIn('elsewhere', 1)"}}},
            "missing_type": {"x-component": "Input"},
        }
        props = extract({"EformInput": native_base(definitions)})["components"]["EformInput"]["properties"]
        for name in definitions:
            with self.subTest(name=name):
                self.assertFalse(props["/x-component-props/" + name]["writable"])

    def test_arbitrary_object_named_group_is_never_assumed_to_be_void(self):
        native = {"EformInput": {"type": "object", "properties": {"form": {"type": "object", "properties": {
                    "required": {"type": "boolean", "x-component": "Switch"}}}}}}
        props = extract(native)["components"]["EformInput"]["properties"]
        self.assertNotIn("/required", props)
        self.assertFalse(props["/form/required"]["writable"])

    def test_scalar_sources_of_native_value_reactions_are_also_readonly(self):
        native = {"EformInput": native_base({
            "autoComplete": {"type": "boolean", "x-component": "Switch"},
            "type": {"type": "string", "enum": ["text", "password"], "x-component": "Select", "x-reactions": {
                "dependencies": [".autoComplete"], "fulfill": {"state": {"value": '{{$deps[0] == true?"text":$self.value}}'}}}},
        })}
        props = extract(native)["components"]["EformInput"]["properties"]
        self.assertFalse(props["/x-component-props/autoComplete"]["writable"])
        self.assertEqual(props["/x-component-props/autoComplete"]["readonly_reason"], "unsupported_dependent_native_reaction")

    def test_captured_native_dynamic_list_setter_enables_complete_binding(self):
        native = {"EformDynamicList": native_base({
            "dataCenter": {"type": "object", "x-component": {"nativeSetter": "DataCenter"}},
            "modelkey": {"type": "string", "x-component": "Input"},
            "modelname": {"type": "string", "x-component": "Input"},
            "showconfig": {"type": "object", "x-component": {"nativeSetter": "DataCenterSetting"}},
        })}
        cat = extract(native)
        prop = cat["components"]["EformDynamicList"]["properties"]["/x-component-props/dataCenter"]
        self.assertTrue(prop["writable"])
        result = apply_operations(page(), [binding()], cat, resources())
        self.assertEqual(len(result["changes"]), 4)
        config = inspect_component(result["page"], "select")["schema"]["x-component-props"]["showconfig"]
        self.assertTrue(all(len(row["id"]) == 8 for row in config["allList"]))
        self.assertEqual(result, apply_operations(page(), [binding()], cat, resources()))
        del native["EformDynamicList"]["properties"]["form"]["properties"]["attribute"]["properties"]["base"]["properties"]["x-component-props"]["properties"]["dataCenter"]["x-component"]
        self.assertFalse(extract(native)["components"]["EformDynamicList"]["properties"]["/x-component-props/dataCenter"]["writable"])

    def test_custom_binding_never_overrides_unknown_native_reactions(self):
        native = {"EformDynamicList": native_base({
            "dataCenter": {"type": "object", "x-component": {"nativeSetter": "DataCenter"}, "x-reactions": {"fulfill": {"run": "arbitrary()"}}},
            "modelkey": {"type": "string", "x-component": "Input"},
            "modelname": {"type": "string", "x-component": "Input"},
            "showconfig": {"type": "object", "x-component": {"nativeSetter": "DataCenterSetting"}},
        })}
        prop = extract(native)["components"]["EformDynamicList"]["properties"]["/x-component-props/dataCenter"]
        self.assertFalse(prop["writable"])

    def test_dictionary_adapter_obeys_native_system_mode_precondition(self):
        native = {"EformStaticList": native_base({
            "dataType": {"type": "string", "enum": ["system", "custom"], "x-component": "Select"},
            "dataGroupId": {"type": "string", "x-component": {"nativeSetter": "SelectGroup"}},
            "staticDataList": {"type": "array", "x-component": "Select", "x-hidden": True},
            "defaultValue": {"type": "array", "x-component": {"nativeSetter": "DictDataSelect"}},
        })}
        cat = extract(native)
        document = page()
        props = document["content"]["schema"]["properties"]["dictionary"]["x-component-props"]
        props["dataType"] = "custom"
        op = operation("dictionary", "EformStaticList", "/x-component-props/dataGroupId", "group-old", {"id": "group-1", "version": "v1"}, "set_binding")
        with self.assertRaises(FormError) as caught:
            apply_operations(document, [op], cat, resources())
        self.assertEqual(caught.exception.code, "native_setter_precondition")
        # The source-reviewed SelectGroup effect writes the native group Key,
        # although the settings metadata calls staticDataList an array.
        props["dataType"] = "system"
        result = apply_operations(document, [op], cat, resources())
        static_data = cat["components"]["EformStaticList"]["properties"]["/x-component-props/staticDataList"]
        self.assertEqual((static_data["native_declared_type"], static_data["type"]), ("array", "string"))
        self.assertEqual(result["changes"][1]["value"], "group-1")

    def test_cli_binds_raw_native_export_hash_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.json"
            output = Path(directory) / "catalog.json"
            raw = json.dumps(exported_inputs(), ensure_ascii=False, indent=2).encode("utf-8")
            source.write_bytes(raw)
            self.assertEqual(main([str(source), str(output), "--component-version", "1.1.65", "--platform", "web"]), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["source_fingerprint"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(source.read_bytes(), raw)
            self.assertTrue(result["available"])

    def test_catalog_digest_changes_with_native_export_and_requires_version(self):
        original = extract()
        native = exported_inputs()
        native["Other"] = native_base()
        self.assertNotEqual(original["native_export_sha256"], extract(native)["native_export_sha256"])
        with self.assertRaises(FormError):
            extract_native_catalog(exported_inputs(), "", "hash", "web")


if __name__ == "__main__":
    unittest.main()
