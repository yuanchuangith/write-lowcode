from __future__ import annotations

import copy
import unittest

from gxp_editor.native_catalog import NativeCatalogError, validated_catalog
from gxp_editor.node_discovery import NodeDiscoveryError, find_baseline_nodes


class NativeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"elements": [{"key": "Noop", "input_params": [], "output_params": []}]}
        self.design = {"actionData": [{"key": "g", "actionType": "js", "data": [
            {"key": f"n{i}", "elementKey": "Noop"} for i in range(120)]}], "globalVariables": []}

    def test_only_selected_locations_are_retained_after_full_validation(self):
        elements, groups, locations = validated_catalog(self.design, self.registry, selected={("g", "n119")})
        self.assertEqual(list(locations), [("g", "n119")])
        self.assertEqual(locations["g", "n119"][1], 119)
        self.assertIs(groups["g"], self.design["actionData"][0])
        self.assertEqual(set(elements), {"Noop"})

    def test_first_page_still_rejects_hidden_unknown_element_or_bad_depth(self):
        for mutation in ({"elementKey": "Unknown"}, {"depth": ["future"]}):
            design = copy.deepcopy(self.design)
            design["actionData"][0]["data"][-1].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(NodeDiscoveryError):
                find_baseline_nodes(design, self.registry, offset=0, limit=1)

    def test_full_validation_happens_when_selection_is_empty(self):
        self.design["actionData"].append({"key": "other", "actionType": "js", "data": [
            {"key": "n0", "elementKey": "Noop"}]})
        with self.assertRaises(NativeCatalogError) as caught:
            validated_catalog(self.design, self.registry)
        self.assertEqual(caught.exception.reason_code, "ambiguous_target")

    def test_late_page_counts_and_filter_order_are_exact(self):
        page = find_baseline_nodes(self.design, self.registry, offset=117, limit=2)
        self.assertEqual([item["node_key"] for item in page["items"]], ["n117", "n118"])
        self.assertEqual(page["total"], 120)
        self.assertEqual(page["next_offset"], 119)
        groups = find_baseline_nodes(self.design, self.registry, kind="groups", element_key="Noop")
        self.assertEqual(groups["items"][0]["node_count"], 120)


if __name__ == "__main__":
    unittest.main()
