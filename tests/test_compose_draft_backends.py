from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mail  # noqa: E402
from lib.ops import drafts, exchange_rest  # noqa: E402


ACCOUNT = "exchange-user@example.com"
OTHER_ACCOUNT = "other@example.com"
TO = ["recipient@example.com"]


class ComposeDraftBackendTests(unittest.TestCase):
    def _patch_guard(self):
        return patch.object(
            drafts,
            "run_guarded_local_mail_mutation",
            Mock(side_effect=lambda _operation, action: action()),
        )

    def _configured_env(self, cli: str = "/tmp/exchange-adapter"):
        return patch.dict(
            os.environ,
            {
                exchange_rest.WEB_EXCHANGE_CLI_ENV: cli,
                exchange_rest.EXCHANGE_REST_ACCOUNTS_ENV: ACCOUNT,
            },
            clear=False,
        )

    def _success_response(self, data: dict) -> dict:
        return {
            "protocol_version": exchange_rest.ADAPTER_PROTOCOL_VERSION,
            "success": True,
            "data": {"account_email": ACCOUNT, **data},
        }

    def _draft(self, *, subject: str = "Subject", content: str = "Body") -> dict:
        return {
            "id": "EXCHANGE_ID",
            "message_id": "<message@example.com>",
            "subject": subject,
            "content": content,
            "to_recipients": TO,
            "cc_recipients": [],
            "bcc_recipients": [],
            "is_draft": True,
            "account_email": ACCOUNT,
        }

    def test_mailapp_compose_fails_when_no_durable_draft_is_verified(self):
        run_result = Mock(stdout="SUCCESS")
        with self._patch_guard(), patch.object(
            drafts,
            "_draft_rows_for_account",
            Mock(return_value=[]),
        ), patch.object(
            drafts,
            "run_applescript",
            Mock(return_value=run_result),
        ), patch.object(
            drafts,
            "sync_mail_state",
            Mock(),
        ), patch.object(
            drafts,
            "_verify_new_durable_draft",
            Mock(return_value=None),
        ), patch.object(
            drafts,
            "_matching_hidden_outgoing",
            Mock(return_value=[{"id": "26", "visible": False}]),
        ):
            result = drafts.compose_draft(
                account_email=OTHER_ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "DRAFT_NOT_DURABLE")
        self.assertEqual(result["backend"], "mailapp")
        self.assertEqual(result["hidden_outgoing"]["count"], 1)
        self.assertFalse(result["verification"]["matched_live_draft"])

    def test_mailapp_compose_succeeds_only_with_verified_draft_metadata(self):
        run_result = Mock(stdout="SUCCESS")
        verified = {
            "id": "123",
            "message_id": "abc@example.com",
            "account_email": OTHER_ACCOUNT,
            "folder_name": "Drafts",
            "verified": True,
        }
        with self._patch_guard(), patch.object(
            drafts,
            "_draft_rows_for_account",
            Mock(return_value=[]),
        ), patch.object(
            drafts,
            "run_applescript",
            Mock(return_value=run_result),
        ), patch.object(
            drafts,
            "sync_mail_state",
            Mock(),
        ), patch.object(
            drafts,
            "_verify_new_durable_draft",
            Mock(return_value=verified),
        ):
            result = drafts.compose_draft(
                account_email=OTHER_ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["draft_id"], "123")
        self.assertEqual(result["message_id"], "abc@example.com")

    def test_auto_uses_mailapp_when_adapter_is_not_configured(self):
        args = mail.build_parser().parse_args([
            "compose-draft",
            "--account",
            ACCOUNT,
            "--subject",
            "Subject",
            "--body",
            "Body",
            "--to",
            TO[0],
        ])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mail._compose_backend(args), "mailapp")
            self.assertTrue(mail._requires_mail_lock(args))

    def test_auto_routes_only_explicitly_configured_adapter_account(self):
        args = mail.build_parser().parse_args([
            "compose-draft",
            "--account",
            ACCOUNT,
            "--subject",
            "Subject",
            "--body",
            "Body",
            "--to",
            TO[0],
        ])
        with self._configured_env(), patch.object(Path, "is_file", return_value=True), patch.object(
            os, "access", return_value=True
        ):
            self.assertEqual(mail._compose_backend(args), "exchange-rest")
            self.assertFalse(mail._requires_mail_lock(args))

    def test_unlisted_account_stays_on_mailapp(self):
        args = mail.build_parser().parse_args([
            "compose-draft",
            "--account",
            OTHER_ACCOUNT,
            "--subject",
            "Subject",
            "--body",
            "Body",
            "--to",
            TO[0],
        ])
        with self._configured_env(), patch.object(Path, "is_file", return_value=True), patch.object(
            os, "access", return_value=True
        ):
            self.assertEqual(mail._compose_backend(args), "mailapp")

    def test_explicit_exchange_without_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            result = exchange_rest.compose_exchange_rest_draft(
                account_email=ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_NOT_CONFIGURED")

    def test_configured_missing_adapter_has_stable_error(self):
        with self._configured_env("/path/that/does/not/exist"):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_NOT_FOUND")

    def test_configured_non_executable_adapter_has_stable_error(self):
        with tempfile.NamedTemporaryFile() as handle, self._configured_env(handle.name):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_NOT_EXECUTABLE")

    def test_unlisted_explicit_account_is_rejected(self):
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True):
            result = exchange_rest.exchange_auth_status(account_email=OTHER_ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_ACCOUNT_NOT_CONFIGURED")

    def test_adapter_receives_private_content_only_over_stdin_json(self):
        captured = Mock()
        captured.returncode = 0
        captured.stdout = json.dumps(self._success_response({"ready": True}))
        captured.stderr = ""
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True), patch.object(
            exchange_rest.subprocess, "run", return_value=captured
        ) as run:
            result = exchange_rest._run_exchange_adapter(
                operation="compose-draft",
                account_email=ACCOUNT,
                auth_mode="background",
                payload={
                    "subject": "Private subject",
                    "body": "Private body",
                    "to": TO,
                    "attachments": ["/private/file.pdf"],
                },
            )

        self.assertTrue(result["success"])
        self.assertEqual(run.call_args.args[0], ["/tmp/exchange-adapter"])
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["protocol_version"], 1)
        self.assertEqual(request["operation"], "compose-draft")
        self.assertEqual(request["account"], ACCOUNT)
        self.assertEqual(request["auth_mode"], "background")
        self.assertEqual(request["payload"]["body"], "Private body")
        self.assertNotIn("Private subject", run.call_args.args[0])
        self.assertNotIn("Private body", run.call_args.args[0])
        self.assertNotIn(TO[0], run.call_args.args[0])

    def test_real_adapter_process_reads_stdin_without_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            record_path = tmp_path / "record.json"
            adapter_path = tmp_path / "adapter.py"
            adapter_path.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "request = json.load(sys.stdin)\n"
                "with open(os.environ['ADAPTER_RECORD'], 'w') as handle:\n"
                "    json.dump({'argv': sys.argv, 'request': request}, handle)\n"
                "print(json.dumps({'protocol_version': 1, 'success': True, "
                "'data': {'account_email': request['account'], 'ready': True}}))\n",
                encoding="utf-8",
            )
            adapter_path.chmod(adapter_path.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                os.environ,
                {
                    exchange_rest.WEB_EXCHANGE_CLI_ENV: str(adapter_path),
                    exchange_rest.EXCHANGE_REST_ACCOUNTS_ENV: ACCOUNT,
                    "ADAPTER_RECORD": str(record_path),
                },
                clear=False,
            ):
                result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)

            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertEqual(record["argv"], [str(adapter_path)])
            self.assertEqual(record["request"]["operation"], "status")
            self.assertEqual(record["request"]["account"], ACCOUNT)

    def test_adapter_timeout_is_structured(self):
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True), patch.object(
            exchange_rest.subprocess,
            "run",
            side_effect=exchange_rest.subprocess.TimeoutExpired("adapter", 1),
        ):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT, timeout=1)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_TIMEOUT")

    def test_adapter_malformed_json_is_structured_without_output_echo(self):
        proc = Mock(returncode=0, stdout="not json and possibly private", stderr="private error")
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True), patch.object(
            exchange_rest.subprocess, "run", return_value=proc
        ):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_BAD_OUTPUT")
        self.assertNotIn("stdout_tail", result)
        self.assertNotIn("stderr_tail", result)
        self.assertNotIn("private", json.dumps(result).lower())

    def test_adapter_protocol_mismatch_is_rejected(self):
        proc = Mock(
            returncode=0,
            stdout=json.dumps({
                "protocol_version": 2,
                "success": True,
                "data": {"account_email": ACCOUNT, "ready": True},
            }),
            stderr="",
        )
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True), patch.object(
            exchange_rest.subprocess, "run", return_value=proc
        ):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_PROTOCOL_MISMATCH")

    def test_adapter_authenticated_account_mismatch_is_rejected(self):
        proc = Mock(
            returncode=0,
            stdout=json.dumps({
                "protocol_version": 1,
                "success": True,
                "data": {"account_email": OTHER_ACCOUNT, "ready": True},
            }),
            stderr="",
        )
        with self._configured_env(), patch.object(Path, "exists", return_value=True), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(os, "access", return_value=True), patch.object(
            exchange_rest.subprocess, "run", return_value=proc
        ):
            result = exchange_rest.exchange_auth_status(account_email=ACCOUNT)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_ADAPTER_ACCOUNT_MISMATCH")

    def test_exchange_compose_verifies_server_readback(self):
        with self._configured_env(), patch.object(
            exchange_rest,
            "_configuration_error",
            return_value=None,
        ), patch.object(
            exchange_rest,
            "_run_exchange_adapter",
            Mock(side_effect=[
                self._success_response({"id": "EXCHANGE_ID"}),
                self._success_response(self._draft()),
            ]),
        ):
            result = exchange_rest.compose_exchange_rest_draft(
                account_email=ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["exchange_id"], "EXCHANGE_ID")
        self.assertEqual(result["message_id"], "<message@example.com>")

    def test_exchange_compose_fails_on_readback_mismatch(self):
        with patch.object(exchange_rest, "_configuration_error", return_value=None), patch.object(
            exchange_rest,
            "_run_exchange_adapter",
            Mock(side_effect=[
                self._success_response({"id": "EXCHANGE_ID"}),
                self._success_response(self._draft(subject="Different")),
            ]),
        ):
            result = exchange_rest.compose_exchange_rest_draft(
                account_email=ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_REST_VERIFY_FAILED")
        self.assertIn("subject", result["verification"]["mismatches"])

    def test_exchange_compose_maps_background_auth_required(self):
        response = {
            "protocol_version": 1,
            "success": False,
            "error": {
                "code": "AUTH_REQUIRED",
                "message": "background auth is not ready",
                "details": {"reason": "login_required"},
            },
        }
        with patch.object(exchange_rest, "_configuration_error", return_value=None), patch.object(
            exchange_rest, "_run_exchange_adapter", return_value=response
        ):
            result = exchange_rest.compose_exchange_rest_draft(
                account_email=ACCOUNT,
                subject="Subject",
                body="Body",
                to=TO,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXCHANGE_AUTH_REQUIRED")
        self.assertEqual(result["reason"], "login_required")
        self.assertTrue(result["focus_safe"])

    def test_exchange_auth_commands_skip_mail_lock(self):
        status_args = mail.build_parser().parse_args([
            "exchange-auth-status",
            "--account",
            ACCOUNT,
        ])
        login_args = mail.build_parser().parse_args([
            "exchange-auth-login",
            "--account",
            ACCOUNT,
        ])
        self.assertFalse(mail._requires_mail_lock(status_args))
        self.assertFalse(mail._requires_mail_lock(login_args))

    def _arial_mismatches(self, content: str, requested_text: str = "Body") -> list[str]:
        return exchange_rest._verification_mismatches(
            draft={
                "is_draft": True,
                "account_email": ACCOUNT,
                "subject": "Subject",
                "content": content,
                "message_id": "<message@example.com>",
                "to_recipients": TO,
                "cc_recipients": [],
                "bcc_recipients": [],
            },
            account_email=ACCOUNT,
            subject="Subject",
            body=content,
            requested_text=requested_text,
            font="arial",
            to=TO,
            cc=[],
            bcc=[],
        )

    def test_arial_compose_accepts_outlook_normalized_readback(self):
        plain_body = "First & <tag>\n\n\nSecond paragraph with https://example.com/."
        normalized = (
            '<html><head><meta charset="utf-8"></head><body>'
            '<div class="PlainText" style="color:#000; white-space: pre-wrap; '
            "font-size: 11pt; font-family: 'Arial', sans-serif\">"
            'First &amp; &lt;tag&gt;<br><br><br>Second paragraph with '
            '<a href="https://example.com/" data-outlook-id="abc123">https://example.com/</a>.'
            '</div></body></html>'
        )
        runner = Mock(side_effect=[
            self._success_response({"id": "EXCHANGE_ID"}),
            self._success_response(self._draft(content=normalized)),
        ])
        with patch.object(exchange_rest, "_configuration_error", return_value=None), patch.object(
            exchange_rest, "_run_exchange_adapter", runner
        ):
            result = exchange_rest.compose_exchange_rest_draft(
                account_email=ACCOUNT,
                subject="Subject",
                body=plain_body,
                to=TO,
                font="arial",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["font"], "arial")
        create_payload = runner.call_args_list[0].kwargs["payload"]
        self.assertIn("font-family:Arial,sans-serif", create_payload["body"])
        self.assertEqual("".join(exchange_rest._parse_draft_html(create_payload["body"]).visible_parts), plain_body)

    def test_arial_html_preserves_blank_lines_edges_and_empty_body(self):
        for body in ("one\n\n\n\nthree", "  leading\n\ntrailing  \n", ""):
            with self.subTest(body=body):
                parsed = exchange_rest._parse_draft_html(exchange_rest._arial_html_body(body))
                self.assertIsNotNone(parsed)
                self.assertEqual("".join(parsed.visible_parts), body)

    def test_arial_verification_rejects_unsafe_or_overriding_html(self):
        cases = {
            "literal-css-text": (
                "<html><body><div>Body says font-family:Arial,sans-serif; font-size:11pt;</div></body></html>",
                "Body says font-family:Arial,sans-serif; font-size:11pt;",
            ),
            "unrelated-styled-div": (
                '<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap"></div>'
                '<div>Body</div></body></html>',
                "Body",
            ),
            "descendant-font": (
                '<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">'
                '<span style="font-family:Calibri;font-size:12pt">Body</span></div></body></html>',
                "Body",
            ),
            "transparent": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap;color:transparent">Body</div></body></html>', "Body"),
            "white": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap;color:#fff">Body</div></body></html>', "Body"),
            "all-shorthand": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap;all:initial">Body</div></body></html>', "Body"),
            "duplicate-font-size": ('<html><body><div style="font-family:Arial,sans-serif;font-size:12pt;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "important": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt!important;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "multiple-style": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap" style="font-size:12pt">Body</div></body></html>', "Body"),
            "missing-pre-wrap": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt">Body</div></body></html>', "Body"),
            "meta-refresh": ('<html><head><meta http-equiv="refresh" content="0"></head><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "unknown-class": ('<html><body><div class="hidden-content" style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "invalid-direction": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap;direction:sideways">Body</div></body></html>', "Body"),
            "style-text-in-class": ("<html><body><div class='style=\"font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap\"'>Body</div></body></html>", "Body"),
            "repeated-body": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body><body>Extra</body></html>', "Body"),
            "outside-text": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div>Extra</body></html>', "Body"),
            "second-div": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div><div>Extra</div></body></html>', "Body"),
            "bad-nesting": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap"><a href="https://example.com">Body</div></a></body></html>', "Body"),
            "hidden-div": ('<html><body><div hidden style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "hidden-body": ('<html><body style="display:none"><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "hidden-link": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap"><a href="https://example.com" hidden>Body</a></div></body></html>', "Body"),
            "script": ('<html><head><script>document.body.style.fontFamily="Calibri"</script></head><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
            "stylesheet": ('<html><head><style>.wrong{font:12pt Calibri}</style></head><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap"><span class="wrong">Body</span></div></body></html>', "Body"),
            "span": ('<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap"><span>Body</span></div></body></html>', "Body"),
            "font-shorthand": ('<html><body><div style="font:11pt Arial">Body</div></body></html>', "Body"),
            "wrong-size": ('<html><body><div style="font-family:Arial,sans-serif;font-size:12pt;white-space:pre-wrap">Body</div></body></html>', "Body"),
        }
        for name, (content, text) in cases.items():
            with self.subTest(name=name):
                self.assertIn("font", self._arial_mismatches(content, text))

    def test_public_default_font_is_provider_default(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "exchange-rest", "--account", ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0],
        ])
        with patch.dict(os.environ, {}, clear=True), patch.object(
            exchange_rest, "compose_exchange_rest_draft", return_value={"success": True}
        ) as compose, patch.object(mail, "_output_op"):
            mail.cmd_compose_draft(args, 0.0)
        self.assertEqual(compose.call_args.kwargs["font"], "provider-default")

    def test_local_font_environment_can_default_exchange_to_arial(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "exchange-rest", "--account", ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0],
        ])
        with patch.dict(os.environ, {"APPLE_MAIL_DRAFT_FONT": "arial"}, clear=False), patch.object(
            exchange_rest, "compose_exchange_rest_draft", return_value={"success": True}
        ) as compose, patch.object(mail, "_output_op"):
            mail.cmd_compose_draft(args, 0.0)
        self.assertEqual(compose.call_args.kwargs["font"], "arial")

    def test_explicit_font_overrides_local_default(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "exchange-rest", "--account", ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0],
            "--font", "provider-default",
        ])
        with patch.dict(os.environ, {"APPLE_MAIL_DRAFT_FONT": "arial"}, clear=False), patch.object(
            exchange_rest, "compose_exchange_rest_draft", return_value={"success": True}
        ) as compose, patch.object(mail, "_output_op"):
            mail.cmd_compose_draft(args, 0.0)
        self.assertEqual(compose.call_args.kwargs["font"], "provider-default")

    def test_non_exchange_backend_rejects_arial_font(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "artifact", "--account", OTHER_ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0], "--font", "arial",
        ])
        with patch.object(mail, "_output_op") as output:
            mail.cmd_compose_draft(args, 0.0)
        result = output.call_args.args[0]
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "DRAFT_FONT_BACKEND_UNSUPPORTED")

    def test_non_exchange_backend_rejects_explicit_provider_default_font(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "mailapp", "--account", OTHER_ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0],
            "--font", "provider-default",
        ])
        with patch.object(mail, "_output_op") as output:
            mail.cmd_compose_draft(args, 0.0)
        self.assertEqual(output.call_args.args[0]["code"], "DRAFT_FONT_BACKEND_UNSUPPORTED")

    def test_local_arial_preference_does_not_break_mailapp_accounts(self):
        args = mail.build_parser().parse_args([
            "compose-draft", "--backend", "mailapp", "--account", OTHER_ACCOUNT,
            "--subject", "Subject", "--body", "Body", "--to", TO[0],
        ])
        with patch.dict(os.environ, {"APPLE_MAIL_DRAFT_FONT": "arial"}, clear=False), patch.object(
            drafts, "compose_draft", return_value={"success": True}
        ) as compose, patch.object(mail, "_output_op"):
            mail.cmd_compose_draft(args, 0.0)
        compose.assert_called_once()

    def test_adapter_auth_reason_does_not_echo_untrusted_details(self):
        response = {
            "success": False,
            "error": {"code": "AUTH_REQUIRED", "details": {"reason": "Private body text"}},
        }
        result = exchange_rest._map_auth_failure(
            ACCOUNT,
            response,
            default_message="Authentication is required",
        )
        self.assertEqual(result["reason"], "token_unavailable")
        self.assertNotIn("Private body text", json.dumps(result))

    def test_server_info_reports_optional_adapter_without_path(self):
        with patch.dict(os.environ, {}, clear=True):
            metadata = exchange_rest.exchange_adapter_metadata()
        self.assertFalse(metadata["configured"])
        self.assertEqual(metadata["path_state"], "not-configured")
        self.assertEqual(metadata["configured_accounts"], [])
        self.assertNotIn("path", metadata)

    def test_public_exchange_configuration_has_no_default_accounts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(exchange_rest.exchange_rest_accounts(), set())
            self.assertFalse(exchange_rest.exchange_adapter_configured())
        self.assertFalse(hasattr(exchange_rest, "DEFAULT_EXCHANGE_REST_ACCOUNTS"))


if __name__ == "__main__":
    unittest.main()
