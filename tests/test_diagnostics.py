import json
import unittest
from unittest.mock import Mock, patch

from school_mcp.diagnostics import check_service, diagnose


class DiagnosticTests(unittest.TestCase):
    def test_report_does_not_export_personal_payload_or_exception(self):
        module = Mock()
        module.BBClient.return_value.check.return_value = {"connected": True, "name": "synthetic-private-name", "courses": ["synthetic-private-course"]}
        with patch("school_mcp.diagnostics.import_module", return_value=module):
            report = check_service("bb")
            self.assertTrue(report["connected"])
            self.assertNotIn("synthetic-private", json.dumps(report))
            module.BBClient.return_value.check.side_effect = RuntimeError("synthetic-private-url")
            report = check_service("bb")
            self.assertFalse(report["connected"])
            self.assertEqual(report["reconnect_tool"], "school_bb_reconnect")
            self.assertNotIn("synthetic-private", json.dumps(report))

    def test_restricted_response_is_not_connected(self):
        module = Mock()
        module.TeachClient.return_value.check.return_value = {"connected": False, "access": "restricted"}
        with patch("school_mcp.diagnostics.import_module", return_value=module):
            self.assertFalse(check_service("teach")["connected"])

    def test_each_requested_service_checked_once_and_failure_is_visible(self):
        with patch("school_mcp.diagnostics.check_service", side_effect=lambda name: {"service": name, "connected": name == "mail"}) as check:
            result = diagnose(["mail", "jw", "mail"])
        self.assertEqual(check.call_count, 2)
        self.assertFalse(result["all_connected"])
        self.assertEqual([item["service"] for item in result["checks"]], ["mail", "jw"])


if __name__ == "__main__":
    unittest.main()
