import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gxp_editor.runtime_package import assert_runtime_resources
from gxp_project_context import session_launcher


class SessionLauncherTests(unittest.TestCase):
    def test_restored_handler_launches_without_changing_a_flow(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder, patch.object(
            session_launcher, "_record_path", return_value=Path(folder) / "handler.json"
        ), patch.object(session_launcher.subprocess, "Popen", return_value=Mock(pid=123)) as launch:
            result = session_launcher.open_session_manager()
            self.assertTrue(result["opened"])
            self.assertEqual(assert_runtime_resources(root)["manifest_sha256"],
                             launch.call_args.kwargs["env"]["GXP_EDITOR_RUNTIME_MANIFEST_SHA256"])
            self.assertEqual(root / "scripts/manage_project_sessions.py", Path(launch.call_args.args[0][-1]))
            self.assertIn("尚未释放", result["message"])


if __name__ == "__main__":
    unittest.main()
