import unittest
from unittest.mock import patch

from gx_core.errors import ScopeDenied
from gx_core.hostobs.runtime import runtime_adapter
from gx_core.hostobs.source_host_adapter import DEFAULT, NAME, launch_argv, select_backend


class SourceHostAdapterTest(unittest.TestCase):
    def test_resume_backend_pin(self):
        with patch.dict("os.environ", {"GX_RUNTIME_BACKEND": NAME}):
            self.assertEqual(select_backend(None), NAME)
            with self.assertRaises(ScopeDenied):
                select_backend({"root_thread_id": "old-app-server-thread"})
        with patch.dict("os.environ", {"GX_RUNTIME_BACKEND": ""}):
            self.assertEqual(select_backend({"runtime_backend": NAME}), NAME)
            self.assertEqual(select_backend(None), DEFAULT)
            with self.assertRaises(ScopeDenied):
                select_backend({"runtime_backend": NAME}, DEFAULT)

    def test_preserves_launch_values(self):
        argv = ["codex", "--disable", "apps", "--enable", "multi_agent_v2", "-c",
                'model="pinned model"', "app-server", "--listen", "stdio://"]
        self.assertEqual(launch_argv("host", argv, "/receipts"),
                         ["host", "--outcome-directory", "/receipts", "-c", "features.apps=false",
                          "-c", "features.multi_agent_v2=true", "-c", 'model="pinned model"'])
        profile = runtime_adapter(NAME).capabilities("test-store")
        self.assertEqual(profile["runtime"]["runtime_kind"], NAME)
        self.assertEqual(profile["runtime_ownership"], "controller")
