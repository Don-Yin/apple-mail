from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dev" / "local_mutation_e2e.py"

spec = importlib.util.spec_from_file_location("local_mutation_e2e", SCRIPT)
assert spec is not None and spec.loader is not None
local_mutation_e2e = importlib.util.module_from_spec(spec)
sys.modules["local_mutation_e2e"] = local_mutation_e2e
spec.loader.exec_module(local_mutation_e2e)


class LocalMutationE2ETests(unittest.TestCase):
    def test_live_e2e_requires_all_three_env_gates(self):
        keys = [
            local_mutation_e2e.ALLOW_LIVE_E2E_ENV,
            local_mutation_e2e.ALLOW_UI_MUTATION_ENV,
            local_mutation_e2e.ALLOW_LOCAL_MUTATION_E2E_ENV,
        ]
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(local_mutation_e2e.live_e2e_enabled())
        with patch.dict(os.environ, {keys[0]: "1", keys[1]: "1"}, clear=True):
            self.assertFalse(local_mutation_e2e.live_e2e_enabled())
        with patch.dict(os.environ, {keys[0]: "1", keys[1]: "1", keys[2]: "1"}, clear=True):
            self.assertTrue(local_mutation_e2e.live_e2e_enabled())

    def test_refusal_payload_names_required_gate(self):
        payload = local_mutation_e2e.refusal_payload("full")
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "MAIL_LOCAL_MUTATION_E2E_DISABLED")
        self.assertIn(local_mutation_e2e.ALLOW_LOCAL_MUTATION_E2E_ENV, payload["error"]["message"])

    def test_mail_args_adds_live_mutation_flag_only_when_requested(self):
        no_live = local_mutation_e2e._mail_args("send-draft", "--id", "1")
        live = local_mutation_e2e._mail_args("send-draft", "--id", "1", live_mutation=True)
        self.assertNotIn(local_mutation_e2e.LIVE_MUTATION_FLAG, no_live)
        self.assertEqual(live[-1], local_mutation_e2e.LIVE_MUTATION_FLAG)

    def test_assert_command_ok_requires_safety_metadata(self):
        summary = {}
        entry = {
            "label": "send-draft",
            "success": True,
            "payload": {"success": True, "data": {"success": True}},
            "stderr": "",
        }
        with self.assertRaisesRegex(RuntimeError, "local_mail_safety"):
            local_mutation_e2e._assert_command_ok(entry, summary)

    def test_assert_command_ok_accepts_clean_safety_metadata(self):
        summary = {}
        entry = {
            "label": "send-draft",
            "success": True,
            "payload": {
                "success": True,
                "data": {
                    "success": True,
                    "local_mail_safety": {
                        "post_health": {"message": "ok"},
                        "focus": {"before": {"bundle_id": "x"}, "restored": True},
                        "new_or_changed_crash_reports": [],
                    },
                },
            },
            "stderr": "",
        }
        local_mutation_e2e._assert_command_ok(entry, summary)
        self.assertEqual(summary["safety_checks"][0]["label"], "send-draft")


if __name__ == "__main__":
    unittest.main()
