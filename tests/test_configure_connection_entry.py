from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_connection.py"
SPEC = importlib.util.spec_from_file_location("write_configure_connection_entry", SCRIPT)
ENTRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRY)


class ConfigureConnectionEntryTests(unittest.TestCase):
    def test_existing_shared_profile_redirects_before_legacy_access_even_when_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "connection.json"
            shared.write_text("invalid", encoding="utf-8")
            ui = ModuleType("gxp_editor.connection_settings_ui")
            ui.main = Mock(return_value=7)
            with patch("gxp_editor.connection_settings.settings_path", return_value=shared), \
                 patch.dict(sys.modules, {ui.__name__: ui}), \
                 patch.object(ENTRY, "load_config", side_effect=AssertionError("legacy load")), \
                 patch.object(ENTRY, "ask", side_effect=AssertionError("legacy prompt")), \
                 patch.object(ENTRY, "save_config", side_effect=AssertionError("legacy save")), \
                 patch.object(ENTRY, "set_secret", side_effect=AssertionError("credential write")):
                self.assertEqual(7, ENTRY.main())
            ui.main.assert_called_once_with()

    def test_absent_shared_profile_preserves_legacy_configuration_entry(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("gxp_editor.connection_settings.settings_path", return_value=Path(directory) / "absent.json"), \
             patch.object(ENTRY, "load_config", side_effect=ENTRY.ConfigurationError("unconfigured")) as load, \
             patch.object(ENTRY, "ask", side_effect=RuntimeError("legacy branch reached")) as ask:
            with self.assertRaisesRegex(RuntimeError, "legacy branch reached"):
                ENTRY.main()
            load.assert_called_once_with()
            ask.assert_called_once_with("Database host", "")


if __name__ == "__main__":
    unittest.main()
