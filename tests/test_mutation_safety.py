from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib.ops import mutation_guard  # noqa: E402
from lib import diagnostics  # noqa: E402
from lib import applescript  # noqa: E402


class LocalMutationSafetyTests(unittest.TestCase):
    def _patch_common(self):
        return patch.multiple(
            mutation_guard,
            capture_frontmost_app=Mock(return_value=("com.example.app", "Example")),
            frontmost_app_fast=Mock(return_value=("com.example.app", "Example")),
            restore_frontmost_app=Mock(),
            mail_crash_report_snapshot=Mock(return_value={}),
            changed_mail_crash_reports=Mock(return_value=[]),
        )

    def test_successful_action_gets_safety_metadata(self):
        with self._patch_common(), patch.object(
            mutation_guard,
            "health_check",
            Mock(side_effect=[{"message": "ok"}, {"message": "ok"}]),
        ):
            result = mutation_guard.run_guarded_local_mail_mutation(
                "unit-test",
                lambda: {"success": True, "message": "done"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["local_mail_safety"]["operation"], "unit-test")
        self.assertEqual(result["local_mail_safety"]["backend"], "mailapp-local")
        self.assertTrue(result["local_mail_safety"]["focus"]["restored"])
        self.assertEqual(result["local_mail_safety"]["new_or_changed_crash_reports"], [])

    def test_precheck_failure_skips_action(self):
        action = Mock(return_value={"success": True})
        with self._patch_common(), patch.object(
            mutation_guard,
            "health_check",
            Mock(return_value={"success": False, "message": "mail down"}),
        ):
            result = mutation_guard.run_guarded_local_mail_mutation("unit-test", action)

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "MAIL_HEALTH_PRECHECK_FAILED")
        action.assert_not_called()

    def test_crash_delta_forces_failure(self):
        with patch.multiple(
            mutation_guard,
            capture_frontmost_app=Mock(return_value=("com.example.app", "Example")),
            frontmost_app_fast=Mock(return_value=("com.example.app", "Example")),
            restore_frontmost_app=Mock(),
            mail_crash_report_snapshot=Mock(return_value={}),
            changed_mail_crash_reports=Mock(return_value=[{"path": "/tmp/Mail-test.ips"}]),
        ), patch.object(
            mutation_guard,
            "health_check",
            Mock(side_effect=[{"message": "ok"}, {"message": "ok"}]),
        ):
            result = mutation_guard.run_guarded_local_mail_mutation(
                "unit-test",
                lambda: {"success": True, "message": "done"},
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "MAIL_CRASH_DETECTED")
        self.assertEqual(
            result["local_mail_safety"]["new_or_changed_crash_reports"],
            [{"path": "/tmp/Mail-test.ips"}],
        )

    def test_stale_touched_crash_report_is_not_actionable_delta(self):
        before = {
            "/tmp/Mail-old.ips": {
                "mtime_ns": 1,
                "size": 10,
                "incident_id": "old",
            }
        }
        stale_after = {
            "/tmp/Mail-old.ips": {
                "path": "/tmp/Mail-old.ips",
                "mtime_ns": 2,
                "mtime": 2000.0,
                "size": 10,
                "incident_id": "old",
                "timestamp_epoch": 100.0,
            }
        }
        with patch.object(diagnostics, "mail_crash_report_snapshot", Mock(return_value=stale_after)):
            changed = diagnostics.changed_mail_crash_reports(before, started_at=1000.0)

        self.assertEqual(changed, [])

    def test_frontmost_fast_falls_back_when_lsappinfo_reports_loginwindow(self):
        run_result = Mock()
        run_result.stdout = "ASN:0x0-0x1001:"
        info_result = Mock()
        info_result.stdout = '"LSDisplayName"="loginwindow"\n"CFBundleIdentifier"="com.apple.loginwindow"'
        with patch.object(applescript.subprocess, "run", Mock(side_effect=[run_result, info_result])), patch.object(
            applescript,
            "_capture_frontmost_app",
            Mock(return_value=("net.kovidgoyal.kitty", "kitty")),
        ):
            self.assertEqual(applescript.frontmost_app_fast(), ("net.kovidgoyal.kitty", "kitty"))

    def test_mail_crash_prone_deleted_status_setter_is_not_used(self):
        source_root = ROOT / "scripts"
        offenders = []
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "set deleted status" in text:
                offenders.append(str(path.relative_to(ROOT)))

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
