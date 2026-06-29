#!/usr/bin/env bash
'''exec' "${MAMBA_ROOT_PREFIX:-$HOME/micromamba}/bin/python" "$0" "$@"
' '''
"""Generated-message-only E2E for the local Mail mutation backend."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from lib.jxa import run_jxa_with_core  # noqa: E402


MAIL_SH = SKILL_ROOT / "scripts" / "mail.sh"
OUT_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "apple-mail-local-mutation-e2e"
ALLOW_LIVE_E2E_ENV = "APPLE_MAIL_ALLOW_LIVE_E2E"
ALLOW_UI_MUTATION_ENV = "APPLE_MAIL_ALLOW_UI_MUTATION"
ALLOW_LOCAL_MUTATION_E2E_ENV = "APPLE_MAIL_ALLOW_LOCAL_MUTATION_E2E"
LIVE_MUTATION_FLAG = "--allow-live-mail-mutation"
SUBJECT_PREFIX = "[apple-mail-local-mutation-e2e"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def live_e2e_enabled() -> bool:
    return (
        _truthy_env(ALLOW_LIVE_E2E_ENV)
        and _truthy_env(ALLOW_UI_MUTATION_ENV)
        and _truthy_env(ALLOW_LOCAL_MUTATION_E2E_ENV)
    )


def refusal_payload(command: str) -> dict:
    return {
        "success": False,
        "error": {
            "code": "MAIL_LOCAL_MUTATION_E2E_DISABLED",
            "message": (
                "local Mail mutation E2E is disabled by default because it creates, amends, "
                "sends, moves, and deletes generated mailbox objects. Set "
                f"{ALLOW_LIVE_E2E_ENV}=1, {ALLOW_UI_MUTATION_ENV}=1, and "
                f"{ALLOW_LOCAL_MUTATION_E2E_ENV}=1 only when generated test mail is acceptable."
            ),
        },
        "meta": {"command": f"local_mutation_e2e {command}"},
    }


def _run_json(args: list[str], timeout: int = 90) -> tuple[subprocess.CompletedProcess, dict | None]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return result, payload


def _mail_args(*args: str, live_mutation: bool = False) -> list[str]:
    command = [str(MAIL_SH), *args]
    if live_mutation:
        command.append(LIVE_MUTATION_FLAG)
    return command


def _run_mail(label: str, args: list[str], timeout: int = 90) -> dict:
    started = time.time()
    result, payload = _run_json(args, timeout=timeout)
    return {
        "label": label,
        "args": args,
        "returncode": result.returncode,
        "success": bool((payload or {}).get("success")),
        "payload": payload,
        "stderr": result.stderr,
        "duration_ms": round((time.time() - started) * 1000, 1),
    }


def _command_data(entry: dict) -> dict:
    return ((entry.get("payload") or {}).get("data") or {})


def _command_error(entry: dict) -> dict:
    return ((entry.get("payload") or {}).get("error") or {})


def _safety(entry: dict) -> dict | None:
    data = _command_data(entry)
    if "local_mail_safety" in data:
        return data["local_mail_safety"]
    details = _command_error(entry).get("details") or {}
    return details.get("local_mail_safety")


def _assert_command_ok(entry: dict, summary: dict) -> None:
    summary.setdefault("safety_checks", []).append({
        "label": entry["label"],
        "success": entry["success"],
        "safety": _safety(entry),
        "error": _command_error(entry),
    })
    if not entry["success"]:
        raise RuntimeError(f"{entry['label']} failed: {_command_error(entry) or entry.get('stderr')}")
    safety = _safety(entry)
    if not safety:
        raise RuntimeError(f"{entry['label']} did not return local_mail_safety metadata")
    if safety.get("new_or_changed_crash_reports"):
        raise RuntimeError(f"{entry['label']} detected Mail crash reports")
    if safety.get("post_health", {}).get("success") is False:
        raise RuntimeError(f"{entry['label']} failed post-health")
    if safety.get("focus", {}).get("before") and not safety.get("focus", {}).get("restored"):
        raise RuntimeError(f"{entry['label']} did not restore focus")


def _find_messages_by_subject(
    subject: str,
    account: str | None = None,
    folder: str | None = None,
    folder_contains: str | None = None,
) -> list[dict]:
    safe_subject = json.dumps(subject)
    safe_account = json.dumps(account)
    safe_folder = json.dumps(folder)
    safe_folder_contains = json.dumps(folder_contains.lower() if folder_contains else None)
    script = f"""
var targetSubject = {safe_subject};
var accountFilter = {safe_account};
var folderFilter = {safe_folder};
var folderContains = {safe_folder_contains};
var accounts = Mail.accounts();
var accountEmails = Mail.accounts.emailAddresses();
var rows = [];
for (var a = 0; a < accounts.length; a++) {{
    var acct = accounts[a];
    var emails = accountEmails[a] || [];
    var accEmail = emails.length > 0 ? emails[0] : acct.name();
    if (accountFilter) {{
        var accountMatches = false;
        for (var e = 0; e < emails.length; e++) {{
            if (String(emails[e]).toLowerCase() === String(accountFilter).toLowerCase()) {{
                accountMatches = true;
                break;
            }}
        }}
        if (!accountMatches) continue;
    }}
    var names = acct.mailboxes.name();
    var mboxes = acct.mailboxes();
    for (var m = 0; m < mboxes.length; m++) {{
        var folderName = String(names[m] || "");
        var folderLower = folderName.toLowerCase();
        if (folderFilter && folderLower !== String(folderFilter).toLowerCase()) continue;
        if (folderContains && folderLower.indexOf(folderContains) === -1) continue;
        try {{
            var matches = mboxes[m].messages.whose({{subject: targetSubject}})();
            for (var i = 0; i < matches.length; i++) {{
                var msg = matches[i];
                rows.push({{
                    id: String(msg.id()),
                    message_id: msg.messageId() || "",
                    subject: msg.subject() || "",
                    sender: msg.sender() || "",
                    date_received: MailCore.formatDate(msg.dateReceived()) || "",
                    account_email: accEmail,
                    folder_name: folderName
                }});
            }}
        }} catch(e) {{}}
    }}
}}
JSON.stringify(rows);
"""
    return run_jxa_with_core(script, timeout=45)


def _wait_for_subject(
    subject: str,
    account: str,
    folder: str | None = None,
    folder_contains: str | None = None,
    expected_count: int | None = None,
    attempts: int = 20,
    delay: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    history = []
    latest: list[dict] = []
    for attempt in range(1, attempts + 1):
        latest = _find_messages_by_subject(
            subject,
            account=account,
            folder=folder,
            folder_contains=folder_contains,
        )
        history.append({
            "attempt": attempt,
            "count": len(latest),
            "folder": folder,
            "folder_contains": folder_contains,
            "ids": [row.get("id") for row in latest],
            "folders": sorted({row.get("folder_name", "") for row in latest}),
        })
        if expected_count is None:
            if latest:
                break
        elif len(latest) == expected_count:
            break
        time.sleep(delay)
    return latest, history


def _delete_generated_matches(subject: str, account: str) -> list[dict]:
    cleanup = []
    for row in _find_messages_by_subject(subject, account=account):
        folder_lower = row.get("folder_name", "").lower()
        if "draft" in folder_lower:
            command = _mail_args("delete-draft", "--id", str(row["id"]), live_mutation=True)
        else:
            command = _mail_args("delete-email", "--ids", str(row["id"]), "--force-int-ids", live_mutation=True)
        entry = _run_mail(
            "cleanup-delete-generated-message",
            command,
            timeout=120,
        )
        cleanup.append({"message": row, "delete": entry})
    return cleanup


def _draft_read_contains(draft_id: str, expected: list[str]) -> dict:
    entry = _run_mail("read-draft", _mail_args("read-email", "--id", draft_id), timeout=90)
    blob = json.dumps(_command_data(entry), ensure_ascii=False)
    return {
        "command": entry,
        "contains": {item: item in blob for item in expected},
        "content_source": _command_data(entry).get("content_source"),
    }


def run_full(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    subject_base = f"{SUBJECT_PREFIX} {run_id}]"
    draft_subject = f"{subject_base} draft lifecycle"
    amended_subject = f"{subject_base} draft amended"
    send_subject = f"{subject_base} send move delete"
    draft_body = f"Generated Apple Mail local mutation draft body run_id={run_id}."
    amended_body = f"Generated Apple Mail local mutation amended body run_id={run_id}."
    send_body = f"Generated Apple Mail local mutation send body run_id={run_id}."
    summary: dict = {
        "command": "full",
        "run_id": run_id,
        "account": args.account,
        "to": args.to,
        "move_folder": args.move_folder,
        "subjects": {
            "draft": draft_subject,
            "amended": amended_subject,
            "send": send_subject,
        },
        "steps": [],
        "safety_checks": [],
    }

    try:
        preflight = _run_mail("local-mutation-preflight", _mail_args("local-mutation-preflight"), timeout=60)
        summary["steps"].append(preflight)
        _assert_command_ok(preflight, summary)

        pre_existing = {
            draft_subject: _find_messages_by_subject(draft_subject, account=args.account),
            amended_subject: _find_messages_by_subject(amended_subject, account=args.account),
            send_subject: _find_messages_by_subject(send_subject, account=args.account),
        }
        summary["pre_existing"] = pre_existing
        if any(pre_existing.values()):
            raise RuntimeError("generated subject unexpectedly existed before test")

        compose = _run_mail(
            "compose-draft",
            _mail_args(
                "compose-draft",
                "--backend", "mailapp",
                "--account", args.account,
                "--subject", draft_subject,
                "--body", draft_body,
                "--to", args.to,
                live_mutation=True,
            ),
            timeout=120,
        )
        summary["steps"].append(compose)
        _assert_command_ok(compose, summary)

        drafts, draft_history = _wait_for_subject(
            draft_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=1,
            attempts=args.poll_attempts,
        )
        summary["draft_create_poll"] = draft_history
        if len(drafts) != 1:
            raise RuntimeError(f"expected one created draft, found {len(drafts)}")
        draft_id = str(drafts[0]["id"])
        summary["created_draft"] = drafts[0]
        read_created = _draft_read_contains(draft_id, [draft_subject, draft_body, args.to])
        summary["read_created_draft"] = read_created
        if not all(read_created["contains"].values()):
            raise RuntimeError("created draft did not contain expected subject/body/recipient")

        amend = _run_mail(
            "amend-draft",
            _mail_args(
                "amend-draft",
                "--id", draft_id,
                "--subject", amended_subject,
                "--body", amended_body,
                live_mutation=True,
            ),
            timeout=120,
        )
        summary["steps"].append(amend)
        _assert_command_ok(amend, summary)

        amended, amended_history = _wait_for_subject(
            amended_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=1,
            attempts=args.poll_attempts,
        )
        original_after_amend, original_after_amend_history = _wait_for_subject(
            draft_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=0,
            attempts=args.poll_attempts,
        )
        summary["draft_amend_poll"] = amended_history
        summary["original_after_amend_poll"] = original_after_amend_history
        if len(amended) != 1 or original_after_amend:
            raise RuntimeError("amended draft verification failed")
        amended_id = str(amended[0]["id"])
        read_amended = _draft_read_contains(amended_id, [amended_subject, amended_body, args.to])
        summary["read_amended_draft"] = read_amended
        if not all(read_amended["contains"].values()):
            raise RuntimeError("amended draft did not contain expected subject/body/recipient")

        delete_draft = _run_mail(
            "delete-draft",
            _mail_args("delete-draft", "--id", amended_id, live_mutation=True),
            timeout=120,
        )
        summary["steps"].append(delete_draft)
        _assert_command_ok(delete_draft, summary)
        amended_after_delete, amended_after_delete_history = _wait_for_subject(
            amended_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=0,
            attempts=args.poll_attempts,
        )
        summary["draft_delete_poll"] = amended_after_delete_history
        if amended_after_delete:
            raise RuntimeError("deleted draft still appears in Drafts")

        send_compose = _run_mail(
            "compose-send-draft",
            _mail_args(
                "compose-draft",
                "--backend", "mailapp",
                "--account", args.account,
                "--subject", send_subject,
                "--body", send_body,
                "--to", args.to,
                live_mutation=True,
            ),
            timeout=120,
        )
        summary["steps"].append(send_compose)
        _assert_command_ok(send_compose, summary)
        send_drafts, send_draft_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=1,
            attempts=args.poll_attempts,
        )
        summary["send_draft_poll"] = send_draft_history
        if len(send_drafts) != 1:
            raise RuntimeError(f"expected one send draft, found {len(send_drafts)}")

        send = _run_mail(
            "send-draft",
            _mail_args("send-draft", "--id", str(send_drafts[0]["id"]), live_mutation=True),
            timeout=120,
        )
        summary["steps"].append(send)
        _assert_command_ok(send, summary)
        send_drafts_after, send_drafts_after_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=0,
            attempts=args.poll_attempts,
        )
        summary["send_draft_gone_poll"] = send_drafts_after_history
        if send_drafts_after:
            raise RuntimeError("sent draft still appears in Drafts")

        sent_messages, sent_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder_contains="sent",
            expected_count=None,
            attempts=max(args.poll_attempts, 30),
            delay=2.0,
        )
        summary["sent_poll"] = sent_history
        if not sent_messages:
            raise RuntimeError("sent message did not appear in a Sent folder")
        sent_id = str(sent_messages[0]["id"])
        summary["sent_message"] = sent_messages[0]
        send_drafts_late, send_drafts_late_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder_contains="draft",
            expected_count=0,
            attempts=args.poll_attempts,
        )
        summary["send_draft_late_gone_poll"] = send_drafts_late_history
        if send_drafts_late:
            raise RuntimeError("sent draft reappeared in Drafts after Sent copy arrived")

        move = _run_mail(
            "move-email",
            _mail_args(
                "move-email",
                "--id", sent_id,
                "--to", args.move_folder,
                live_mutation=True,
            ),
            timeout=120,
        )
        summary["steps"].append(move)
        _assert_command_ok(move, summary)
        moved_messages, moved_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder=args.move_folder,
            expected_count=None,
            attempts=args.poll_attempts,
        )
        summary["move_poll"] = moved_history
        if not moved_messages:
            raise RuntimeError(f"message did not appear in move folder {args.move_folder!r}")

        delete_message = _run_mail(
            "delete-email",
            _mail_args(
                "delete-email",
                "--ids", str(moved_messages[0]["id"]),
                "--force-int-ids",
                live_mutation=True,
            ),
            timeout=120,
        )
        summary["steps"].append(delete_message)
        _assert_command_ok(delete_message, summary)
        moved_after_delete, moved_after_delete_history = _wait_for_subject(
            send_subject,
            account=args.account,
            folder=args.move_folder,
            expected_count=0,
            attempts=args.poll_attempts,
        )
        summary["delete_email_poll"] = moved_after_delete_history
        if moved_after_delete:
            raise RuntimeError("deleted generated message still appears in move folder")

        if args.expect_inbox_copy:
            inbox_matches, inbox_history = _wait_for_subject(
                send_subject,
                account=args.account,
                folder_contains="inbox",
                expected_count=None,
                attempts=args.poll_attempts,
            )
            summary["inbox_copy_poll"] = inbox_history
            if not inbox_matches:
                raise RuntimeError("expected self-send inbox copy did not appear")

        summary["pass"] = True
    except Exception as exc:  # noqa: BLE001 - persisted as E2E evidence.
        summary["pass"] = False
        summary["test_exception"] = str(exc)
    finally:
        pre_cleanup_generated_messages = {
            subject: _find_messages_by_subject(subject, account=args.account)
            for subject in (draft_subject, amended_subject, send_subject)
        }
        summary["pre_cleanup_generated_messages"] = pre_cleanup_generated_messages
        pre_cleanup_blocking_residuals = []
        for subject, rows in pre_cleanup_generated_messages.items():
            for row in rows:
                folder_lower = row.get("folder_name", "").lower()
                if "draft" in folder_lower:
                    pre_cleanup_blocking_residuals.append({
                        "reason": "generated_draft_residual_before_cleanup",
                        "subject": subject,
                        **row,
                    })
        summary["pre_cleanup_blocking_residuals"] = pre_cleanup_blocking_residuals
        if pre_cleanup_blocking_residuals:
            summary["pass"] = False

        cleanup = []
        if args.cleanup:
            for subject in (draft_subject, amended_subject, send_subject):
                cleanup.extend(_delete_generated_matches(subject, args.account))
        summary["cleanup"] = cleanup
        summary["residual_generated_messages"] = {
            subject: _find_messages_by_subject(subject, account=args.account)
            for subject in (draft_subject, amended_subject, send_subject)
        }
        if args.cleanup:
            # Deletion normally moves messages to Trash, so residual generated messages
            # can still be a valid delete result. Treat Drafts and move-folder residues
            # as cleanup failures; Trash residues are reported for manual audit.
            blocking_residuals = []
            for subject, rows in summary["residual_generated_messages"].items():
                for row in rows:
                    folder_lower = row.get("folder_name", "").lower()
                    if "trash" not in folder_lower and "deleted" not in folder_lower:
                        blocking_residuals.append({"subject": subject, **row})
            summary["blocking_residuals"] = blocking_residuals
            if blocking_residuals:
                summary["pass"] = False

        log_path = OUT_DIR / f"{run_id}-full.json"
        summary["log_path"] = str(log_path)
        log_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary.get("pass") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    full = sub.add_parser("full", help="run generated-message local mutation E2E")
    full.add_argument("--account", required=True, help="Mail account email to test")
    full.add_argument("--to", required=True, help="generated test recipient, usually the same account")
    full.add_argument("--move-folder", required=True, help="existing destination folder for move-email verification")
    full.add_argument("--poll-attempts", type=int, default=20, help="poll attempts per verification step")
    full.add_argument("--expect-inbox-copy", action="store_true", help="require self-send inbox receipt verification")
    full.add_argument("--no-cleanup", dest="cleanup", action="store_false", help="leave generated messages for inspection")
    full.set_defaults(func=run_full, cleanup=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not live_e2e_enabled():
        print(json.dumps(refusal_payload(args.command), indent=2))
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
