#!/usr/bin/env python3
"""Apple Mail CLI — agent-facing entry point.

All output is JSON to stdout. Exit 0 on success, 1 on error.
Response contract: {success, data, error, warnings, meta}
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = "1.2.0"
_MAIL_LOCK_PATH = Path("/tmp/apple-mail-skill.lock")
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_ALLOW_UI_MUTATION_ENV = "APPLE_MAIL_ALLOW_UI_MUTATION"
_ALLOW_UI_MUTATION_COMMAND_ENV = "APPLE_MAIL_ALLOW_UI_MUTATION_COMMAND"
_DRAFT_BACKEND_ENV = "APPLE_MAIL_DRAFT_BACKEND"
_DRAFT_FONT_ENV = "APPLE_MAIL_DRAFT_FONT"
_DEFAULT_DRAFT_BACKEND = "auto"
_DEFAULT_DRAFT_FONT = "provider-default"
_MAIL_UI_MUTATION_COMMANDS = {
    "amend-draft",
    "send-draft",
    "reply-draft",
    "forward-draft",
    "delete-email",
    "delete-draft",
    "move-email",
    "batch-move",
}

# Mutations permitted by DEFAULT (no override flag/env needed). delete-email deletes by
# MOVING the message into its own account's Trash -- a same-account move proven by live
# canary to sync server-side AND never crash Mail (unlike the AppleScript `delete` verb).
# It still runs inside the safety envelope (pre/post health, focus restore, crash-delta).
_DEFAULT_ALLOWED_MUTATION_COMMANDS = {
    "delete-email",
}


def _wrap(data=None, error=None, warnings=None, command="", start_time=None):
    execution_time_ms = round((time.monotonic() - start_time) * 1000, 1) if start_time else 0
    return {
        "success": error is None,
        "data": data,
        "error": error,
        "warnings": warnings or [],
        "meta": {
            "command": command,
            "execution_time_ms": execution_time_ms,
            "timestamp": datetime.now().isoformat(),
        },
    }


def _error(code: str, message: str, details: dict = None):
    return {"code": code, "message": message, "details": details or {}}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _compose_backend(args) -> str:
    backend = getattr(args, "backend", None) or os.environ.get(_DRAFT_BACKEND_ENV, _DEFAULT_DRAFT_BACKEND)
    backend = backend.strip().lower()
    if backend != "auto":
        return backend
    account = getattr(args, "account", None)
    if account:
        from lib.ops.exchange_rest import account_uses_exchange_rest
        if account_uses_exchange_rest(account):
            return "exchange-rest"
    return "mailapp"


def _requires_mail_ui_mutation(args) -> bool:
    return args.command in _MAIL_UI_MUTATION_COMMANDS


def _requires_mail_lock(args) -> bool:
    if args.command in {"server-info", "exchange-auth-status", "exchange-auth-login"}:
        return False
    return args.command != "server-info" and not (
        args.command == "compose-draft" and _compose_backend(args) in {"artifact", "exchange-rest"}
    )


def _mutation_command_key(args) -> str:
    return args.command


def _mutation_commands_from_env() -> set[str]:
    raw = os.environ.get(_ALLOW_UI_MUTATION_COMMAND_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _mail_ui_mutation_allowed(args) -> bool:
    if _mutation_command_key(args).lower() in _DEFAULT_ALLOWED_MUTATION_COMMANDS:
        return True
    if getattr(args, "allow_live_mail_mutation", False):
        return True
    return (
        _truthy_env(_ALLOW_UI_MUTATION_ENV)
        and _mutation_command_key(args).lower() in _mutation_commands_from_env()
    )


def _authorize_live_mail_mutation_for_ops(args) -> None:
    if getattr(args, "allow_live_mail_mutation", False):
        os.environ[_ALLOW_UI_MUTATION_ENV] = "1"
        os.environ[_ALLOW_UI_MUTATION_COMMAND_ENV] = _mutation_command_key(args)


def _mutation_disabled_result(command: str, start_time):
    return _wrap(
        error=_error(
            "MAIL_UI_MUTATION_DISABLED",
            "live Apple Mail send/delete/move/amend/reply/forward operations are disabled by default because those paths "
            "have crashed Mail during testing. Use compose-draft for auto-routed verified drafts, "
            "compose-draft --backend artifact for a no-Mail draft artifact, "
            "or pass --allow-live-mail-mutation for an explicit development-only one-command override.",
            {
                "command": command,
                "override_flag": "--allow-live-mail-mutation",
                "override_env": _ALLOW_UI_MUTATION_ENV,
                "override_command_env": _ALLOW_UI_MUTATION_COMMAND_ENV,
                "draft_backend_env": _DRAFT_BACKEND_ENV,
                "default_compose_backend": _DEFAULT_DRAFT_BACKEND,
                "artifact_compose_backend": "artifact",
                "exchange_rest_compose_backend": "exchange-rest",
                "direct_call_override": f"{_ALLOW_UI_MUTATION_ENV}=1 {_ALLOW_UI_MUTATION_COMMAND_ENV}=<command>",
            },
        ),
        command=command,
        start_time=start_time,
    )


def _output(result: dict):
    """write json to stdout with 10 MB cap to prevent downstream memory explosion."""
    raw = json.dumps(result, indent=2, default=str)
    if len(raw) > _MAX_OUTPUT_BYTES:
        result = _wrap(
            error=_error("OUTPUT_TOO_LARGE", f"response would be {len(raw)/1e6:.1f} MB (cap: {_MAX_OUTPUT_BYTES/1e6:.0f} MB). use a smaller --limit"),
            command=result.get("meta", {}).get("command", ""),
        )
        raw = json.dumps(result, indent=2, default=str)
    sys.stdout.write(raw)
    sys.stdout.write("\n")
    sys.exit(0 if result.get("success") else 1)


def _infer_error_code(message: str) -> str:
    """Map common operation failure messages to standard error codes."""
    msg = message.lower()
    if "no account found" in msg or ("account" in msg and "not found" in msg):
        return "ACCOUNT_NOT_FOUND"
    if "folder" in msg and "not found" in msg:
        return "FOLDER_NOT_FOUND"
    if "draft" in msg and "not found" in msg:
        return "DRAFT_NOT_FOUND"
    if "not found" in msg:
        return "EMAIL_NOT_FOUND"
    if "invalid" in msg and "id" in msg:
        return "INVALID_ID"
    if "timed out" in msg or "timeout" in msg:
        return "JXA_TIMEOUT"
    return "OPERATION_FAILED"


def _output_op(result: dict, command: str, t0):
    """Output handler for operations that return {success, message, ...}.

    Detects inner success=False and converts to a proper error response so the
    CLI response contract is consistent (failures always in 'error', exit code 1).
    """
    if isinstance(result, dict) and result.get("success") is False:
        msg = result.get("message") or result.get("error") or "operation failed"
        code = result.get("code") or _infer_error_code(str(msg))
        # preserve diagnostic fields (e.g. delete's unknown_state / partial counts) so
        # they survive the success=False path instead of being dropped from the error.
        details = {
            k: result[k]
            for k in (
                "unknown_state", "deleted", "requested", "not_found", "backend", "host", "port", "mailbox",
                "local_mail_safety", "method", "moved", "failed", "verification", "hidden_outgoing",
                "server_written", "dry_run", "message_id", "eml_path", "manifest_path", "target",
                "exchange_id", "verified", "draft", "web_exchange", "supported_accounts",
                "reason", "focus_safe", "recommended_command", "account_email", "auth", "status",
            )
            if k in result
        }
        _output(_wrap(
            error=_error(code, str(msg), details),
            command=command,
            start_time=t0,
        ))
    else:
        _output(_wrap(data=result, command=command, start_time=t0))


# ------------------------------------------------------------------
# ID resolution helpers
# ------------------------------------------------------------------


def _resolve_mid(message_id: str) -> dict | None:
    """resolve rfc message-id to current int_id via 3-tier cache."""
    from lib.resolve import resolve_message
    return resolve_message(message_id)


def _resolve_id_or_message_id(args, command: str, t0) -> str | None:
    """return a resolved integer id from --id or --message-id, or None on failure."""
    mid = getattr(args, "message_id", None)
    iid = getattr(args, "id", None)
    if mid:
        resolved = _resolve_mid(mid)
        if resolved:
            return resolved["id"]
        _output(_wrap(
            error=_error("EMAIL_NOT_FOUND", f"no email found with message-id '{mid[:50]}...'"),
            command=command, start_time=t0,
        ))
        return None
    if iid:
        return iid
    _output(_wrap(
        error=_error("MISSING_ID", "provide either --id or --message-id"),
        command=command, start_time=t0,
    ))
    return None


def _resolve_ids_or_message_ids(args, command: str, t0) -> list[str] | None:
    """return resolved integer id list from --ids or --message-ids, or None on failure."""
    mids = getattr(args, "message_ids", None)
    iids = getattr(args, "ids", None)
    if mids:
        resolved = []
        for mid in mids:
            r = _resolve_mid(mid)
            if r:
                resolved.append(r["id"])
            else:
                _output(_wrap(
                    error=_error("EMAIL_NOT_FOUND", f"no email found with message-id '{mid[:50]}...'"),
                    command=command, start_time=t0,
                ))
                return None
        return resolved
    if iids:
        return iids
    _output(_wrap(
        error=_error("MISSING_ID", "provide either --ids or --message-ids"),
        command=command, start_time=t0,
    ))
    return None


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------


def cmd_server_info(args, t0):
    from lib.ops.exchange_rest import exchange_adapter_metadata

    _output(_wrap(
        data={
            "name": "apple-mail-skill",
            "version": VERSION,
            "description": "cursor skill for apple mail on macos",
            "default_draft_backend": _DEFAULT_DRAFT_BACKEND,
            "draft_backend_env": _DRAFT_BACKEND_ENV,
            "default_draft_font": os.environ.get(_DRAFT_FONT_ENV, _DEFAULT_DRAFT_FONT),
            "draft_font_env": _DRAFT_FONT_ENV,
            "exchange_adapter": exchange_adapter_metadata(),
            "local_mail_mutation_safety_envelope": True,
            "total_commands": len([name for name in COMMAND_MAP if not name.startswith("_")]),
        },
        command="server-info",
        start_time=t0,
    ))


def cmd_check_health(args, t0):
    from lib.ops.health import health_check

    result = health_check()
    _output_op(result, "check-health", t0)


def cmd_local_mutation_preflight(args, t0):
    from lib.diagnostics import newest_mail_crash_report
    from lib.ops.mutation_guard import run_guarded_local_mail_mutation

    def action():
        return {
            "success": True,
            "message": "local Mail mutation safety envelope preflight passed without mutating mailbox state",
            "newest_mail_crash_report": newest_mail_crash_report(),
        }

    result = run_guarded_local_mail_mutation("local-mutation-preflight", action)
    _output_op(result, "local-mutation-preflight", t0)


def cmd_list_accounts(args, t0):
    from lib.ops.accounts import list_accounts

    result = list_accounts()
    _output(_wrap(data=result, command="list-accounts", start_time=t0))


def cmd_list_folders(args, t0):
    from lib.ops.accounts import list_account_folders

    result = list_account_folders(args.account)
    if isinstance(result, dict) and result.get("success") is False:
        _output_op(result, "list-folders", t0)
        return
    _output(_wrap(data=result, command="list-folders", start_time=t0))


def cmd_list_recent(args, t0):
    from lib.ops.accounts import list_recent_emails

    result = list_recent_emails(
        most_recent_n_emails=args.limit,
        include_content=args.include_content,
    )
    warnings = result.pop("coverage_warnings", []) if isinstance(result, dict) else []
    _output(_wrap(data=result, warnings=warnings, command="list-recent", start_time=t0))


def cmd_list_emails(args, t0):
    from lib.ops.folders import list_emails_in_folder

    result = list_emails_in_folder(
        account_email=args.account,
        folder_name=args.folder,
        limit=args.limit,
        include_content=args.include_content,
    )
    if isinstance(result, dict) and result.get("success") is False:
        _output_op(result, "list-emails", t0)
        return
    warnings = result.pop("coverage_warnings", []) if isinstance(result, dict) else []
    _output(_wrap(data=result, warnings=warnings, command="list-emails", start_time=t0))


def cmd_list_drafts(args, t0):
    from lib.ops.drafts import list_drafts

    result = list_drafts(limit=args.limit, include_content=args.include_content)
    warnings = result.pop("coverage_warnings", []) if isinstance(result, dict) else []
    _output(_wrap(data=result, warnings=warnings, command="list-drafts", start_time=t0))


def cmd_read_email(args, t0):
    from lib.ops.read import read_full_email

    identifier = _resolve_id_or_message_id(args, "read-email", t0)
    if identifier is None:
        return

    result = read_full_email(identifier)
    if isinstance(result, dict) and result.get("success") is False:
        mail_sh = str(Path(__file__).resolve().parent / "mail.sh")
        msg = result.get("message", "email not found")
        code = _infer_error_code(msg)
        _output(_wrap(
            error=_error(
                code,
                msg,
                {"recovery": f"re-list the folder for current IDs: {mail_sh} list-emails --account <EMAIL> --folder <FOLDER>"},
            ),
            command="read-email",
            start_time=t0,
        ))
    else:
        _output(_wrap(data=result, command="read-email", start_time=t0))


def cmd_search(args, t0):
    from lib.ops.search import search_emails

    warnings = []
    if args.scope == "all" and args.account:
        warnings.append(
            "account filtering is not yet reliable with scope=all (disk UUIDs vs email addresses). "
            "results may include emails from other accounts."
        )

    result = search_emails(
        query=args.query,
        scope=args.scope,
        account_email=args.account,
        limit=args.limit,
    )

    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict) and "error" in result[0]:
        mail_sh = str(Path(__file__).resolve().parent / "mail.sh")
        _output(_wrap(
            error=_error("INDEX_NOT_FOUND", result[0]["error"], {
                "recovery": f"build the index first: {mail_sh} build-index",
            }),
            command="search",
            start_time=t0,
        ))
    else:
        _output(_wrap(data=result, warnings=warnings, command="search", start_time=t0))


def cmd_exchange_auth_status(args, t0):
    from lib.ops.exchange_rest import exchange_auth_status

    result = exchange_auth_status(account_email=args.account)
    _output_op(result, "exchange-auth-status", t0)


def cmd_exchange_auth_login(args, t0):
    from lib.ops.exchange_rest import exchange_auth_login

    result = exchange_auth_login(account_email=args.account)
    _output_op(result, "exchange-auth-login", t0)


def cmd_compose_draft(args, t0):
    backend = _compose_backend(args)
    explicit_font = args.font
    requested_font = (
        explicit_font or os.environ.get(_DRAFT_FONT_ENV, _DEFAULT_DRAFT_FONT)
        if backend == "exchange-rest"
        else _DEFAULT_DRAFT_FONT
    )
    if explicit_font not in {None, "arial", "provider-default"} or requested_font not in {"arial", "provider-default"}:
        _output_op({
            "success": False,
            "code": "UNSUPPORTED_DRAFT_FONT",
            "message": f"unsupported draft font: {explicit_font or requested_font}",
        }, "compose-draft", t0)
        return
    if explicit_font is not None and backend != "exchange-rest":
        _output_op({
            "success": False,
            "code": "DRAFT_FONT_BACKEND_UNSUPPORTED",
            "message": "font selection is supported only by the configured exchange-rest adapter backend",
            "backend": backend,
        }, "compose-draft", t0)
        return

    if backend == "artifact":
        from lib.ops.draft_artifacts import create_draft_artifact

        result = create_draft_artifact(
            account_email=args.account,
            subject=args.subject,
            body=args.body,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            attachments=args.attachments,
            output_dir=args.output_dir,
        )
    elif backend == "exchange-rest":
        from lib.ops.exchange_rest import compose_exchange_rest_draft

        result = compose_exchange_rest_draft(
            account_email=args.account,
            subject=args.subject,
            body=args.body,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            attachments=args.attachments,
            font=requested_font,
        )
    elif backend == "mailapp":
        from lib.ops.drafts import compose_draft

        result = compose_draft(
            account_email=args.account,
            subject=args.subject,
            body=args.body,
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            attachments=args.attachments,
        )
    else:
        result = {
            "success": False,
            "message": f"unknown compose backend {backend!r}; use 'auto', 'artifact', 'exchange-rest', or 'mailapp'",
        }
    _output_op(result, "compose-draft", t0)


def cmd_amend_draft(args, t0):
    from lib.ops.drafts import amend_draft

    result = amend_draft(
        draft_id=args.id,
        new_subject=args.subject,
        new_body=args.body,
        new_cc=args.cc,
        new_bcc=args.bcc,
        new_attachments=args.attachments,
    )
    _output_op(result, "amend-draft", t0)


def cmd_send_draft(args, t0):
    from lib.ops.drafts import send_draft

    result = send_draft(args.id)
    _output_op(result, "send-draft", t0)


def cmd_reply_draft(args, t0):
    from lib.ops.drafts import reply_draft

    identifier = _resolve_id_or_message_id(args, "reply-draft", t0)
    if identifier is None:
        return

    result = reply_draft(
        original_email_id=identifier,
        body=args.body,
        reply_all=args.reply_all,
        extra_cc=args.cc,
        extra_bcc=args.bcc,
        extra_attachments=args.attachments,
    )
    _output_op(result, "reply-draft", t0)


def cmd_forward_draft(args, t0):
    from lib.ops.forward import make_forward_draft

    identifier = _resolve_id_or_message_id(args, "forward-draft", t0)
    if identifier is None:
        return

    result = make_forward_draft(
        email_id=identifier,
        account=args.account,
        body=args.body,
        to=args.to,
        cc=args.cc,
        bcc=args.bcc,
        new_attachments=args.attachments,
    )
    _output_op(result, "forward-draft", t0)


def cmd_delete_email(args, t0):
    from lib.ops.delete import delete_email, delete_emails_batch

    # destructive-safety gate: integer ids shift across syncs and are easy to collide
    # with (small/sequential), which caused a real mis-delete. require the stable
    # --message-ids, or an explicit --force-int-ids opt-in, before deleting by int id.
    if getattr(args, "ids", None) and not getattr(args, "message_ids", None) and not getattr(args, "force_int_ids", False):
        _output(_wrap(
            error=_error(
                "UNSAFE_INT_IDS",
                "refusing to delete by integer --ids: they shift across Exchange syncs and are "
                "easy to collide with. use --message-ids (the stable RFC ids from any list "
                "command), or pass --force-int-ids to override.",
            ),
            command="delete-email", start_time=t0,
        ))
        return

    identifiers = _resolve_ids_or_message_ids(args, "delete-email", t0)
    if identifiers is None:
        return

    if len(identifiers) == 1:
        result = delete_email(identifiers[0])
    else:
        result = delete_emails_batch(identifiers)
    _output_op(result, "delete-email", t0)


def cmd_delete_draft(args, t0):
    from lib.ops.delete import delete_draft

    result = delete_draft(args.id)
    _output_op(result, "delete-draft", t0)


def cmd_move_email(args, t0):
    from lib.ops.move import move_email

    identifier = _resolve_id_or_message_id(args, "move-email", t0)
    if identifier is None:
        return

    result = move_email(identifier, args.to, to_account=args.to_account)
    _output_op(result, "move-email", t0)


def cmd_batch_move(args, t0):
    from lib.ops.move import batch_move_emails
    identifiers = _resolve_ids_or_message_ids(args, "batch-move", t0)
    if identifiers is None:
        return
    result = batch_move_emails(identifiers, args.to, to_account=args.to_account)
    _output_op(result, "batch-move", t0)


def cmd_fix_spotlight(args, t0):
    """disable Spotlight indexing for ~/Library/Mail to prevent mds_stores CPU drain."""
    mail_dir = Path.home() / "Library" / "Mail"
    if not mail_dir.exists():
        _output(_wrap(data={"status": "skipped", "reason": "~/Library/Mail not found"}, command="fix-spotlight", start_time=t0))
        return

    marker = mail_dir / ".metadata_never_index"
    plist = Path("/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist")
    actions = []

    if not marker.exists():
        try:
            marker.touch()
            actions.append("created .metadata_never_index marker")
        except PermissionError:
            actions.append("cannot create .metadata_never_index — grant Full Disk Access to Terminal, or run: touch ~/Library/Mail/.metadata_never_index")
    else:
        actions.append(".metadata_never_index marker already exists")

    plist_excluded = False
    try:
        import subprocess
        r = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print :Exclusions", str(plist)],
            capture_output=True, text=True, timeout=5,
        )
        plist_excluded = str(mail_dir) in r.stdout
    except Exception:
        pass

    if not plist_excluded:
        try:
            r = subprocess.run(
                ["sudo", "-n", "/usr/libexec/PlistBuddy", "-c",
                 f"Add :Exclusions:0 string {mail_dir}", str(plist)],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                actions.append("added to Spotlight exclusions in VolumeConfiguration.plist")
            else:
                actions.append("plist update needs sudo — run manually: "
                               f"sudo /usr/libexec/PlistBuddy -c 'Add :Exclusions:0 string {mail_dir}' {plist}")
        except Exception:
            actions.append(f"plist update needs sudo — run manually: "
                           f"sudo /usr/libexec/PlistBuddy -c 'Add :Exclusions:0 string {mail_dir}' {plist}")
    else:
        actions.append("already in Spotlight exclusions plist")

    _output(_wrap(data={"status": "done", "actions": actions}, command="fix-spotlight", start_time=t0))


def cmd_build_index(args, t0):
    from lib.ops.search import build_search_index

    warnings = []
    mail_dir = Path.home() / "Library" / "Mail"
    if mail_dir.exists() and not (mail_dir / ".metadata_never_index").exists():
        warnings.append(
            "spotlight indexing is enabled on ~/Library/Mail — this can cause mds_stores to consume 100%+ CPU. "
            "run: mail.sh fix-spotlight"
        )

    result = build_search_index()
    if isinstance(result, dict) and result.get("success") is False:
        _output(_wrap(error=_error("BUILD_FAILED", result.get("error") or result.get("message") or "index build failed (no detail returned)"), warnings=warnings, command="build-index", start_time=t0))
    else:
        _output(_wrap(data=result, warnings=warnings, command="build-index", start_time=t0))


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        import json, sys
        json.dump({"success": False, "data": None, "error": {"code": "INVALID_COMMAND", "message": message, "details": {}}, "warnings": [], "meta": {"command": "parse", "execution_time_ms": 0, "timestamp": ""}}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(1)


def build_parser():
    parser = _JsonArgumentParser(
        prog="mail.py",
        description="Apple Mail CLI — agent-facing tool for reading, writing, and managing emails on macOS.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", help="available commands")

    def add_live_mutation_override(p):
        p.add_argument(
            "--allow-live-mail-mutation",
            action="store_true",
            help="development-only: allow this command to mutate live Mail.app state",
        )

    # server-info
    sub.add_parser("server-info", help="show server/skill info")

    # check-health
    sub.add_parser("check-health", help="verify Mail.app is responding")

    # local-mutation-preflight
    sub.add_parser(
        "local-mutation-preflight",
        help="non-destructive preflight for local Mail mutation safety checks",
    )

    # list-accounts
    sub.add_parser("list-accounts", help="list all mail accounts")

    # list-folders
    p = sub.add_parser("list-folders", help="list folders for an account")
    p.add_argument("--account", required=True, help="account email address")

    # list-recent
    p = sub.add_parser("list-recent", help="list recent emails from all inboxes")
    p.add_argument("--limit", type=int, default=128, help="max emails per inbox (default: 128)")
    p.add_argument("--include-content", action="store_true", help="add preview from search index")

    # list-emails
    p = sub.add_parser("list-emails", help="list emails in a specific folder")
    p.add_argument("--account", required=True, help="account email address")
    p.add_argument("--folder", required=True, help="folder name (case-insensitive)")
    p.add_argument("--limit", type=int, default=128, help="max emails (default: 128)")
    p.add_argument("--include-content", action="store_true", help="add preview from search index")

    # list-drafts
    p = sub.add_parser("list-drafts", help="list drafts across all accounts")
    p.add_argument("--limit", type=int, default=128, help="max drafts (default: 128)")
    p.add_argument("--include-content", action="store_true", help="add preview from search index")

    # read-email
    p = sub.add_parser("read-email", help="read full email content by ID")
    p.add_argument("--id", default=None, help="email integer ID")
    p.add_argument("--message-id", default=None, help="RFC 2822 message-id (preferred, stable across syncs)")

    # search
    p = sub.add_parser("search", help="search emails by content, subject, or sender")
    p.add_argument("--query", required=True, help="search query")
    p.add_argument("--scope", default="all", choices=["all", "subject", "sender"], help="search scope (default: all)")
    p.add_argument("--account", help="limit to specific account")
    p.add_argument("--limit", type=int, default=64, help="max results (default: 64)")

    # exchange-auth-status
    p = sub.add_parser("exchange-auth-status", help="check whether Exchange background auth is ready without focusing Chrome")
    p.add_argument("--account", required=True, help="Exchange account email")

    # exchange-auth-login
    p = sub.add_parser("exchange-auth-login", help="explicitly open/focus Outlook Web auth for Exchange drafting")
    p.add_argument("--account", required=True, help="Exchange account email")

    # compose-draft
    p = sub.add_parser("compose-draft", help="create a new draft email")
    p.add_argument("--account", required=True, help="sending account email")
    p.add_argument("--subject", required=True, help="email subject")
    p.add_argument("--body", required=True, help="email body")
    p.add_argument("--to", nargs="+", required=True, help="recipient addresses")
    p.add_argument("--cc", nargs="+", help="CC addresses")
    p.add_argument("--bcc", nargs="+", help="BCC addresses")
    p.add_argument("--attachments", nargs="+", help="file paths to attach")
    p.add_argument("--font", choices=["arial", "provider-default"], default=None, help="Exchange adapter draft font (default: APPLE_MAIL_DRAFT_FONT or provider-default)")
    p.add_argument("--backend", choices=["auto", "artifact", "exchange-rest", "mailapp"], default=None, help="draft backend: auto uses exchange-rest only for explicitly configured adapter accounts, otherwise mailapp")
    p.add_argument("--output-dir", default=None, help="artifact backend output directory (default: ~/Documents/apple-mail-draft-artifacts)")
    add_live_mutation_override(p)

    # amend-draft
    p = sub.add_parser("amend-draft", help="amend an existing draft")
    p.add_argument("--id", required=True, help="draft ID")
    p.add_argument("--subject", help="new subject")
    p.add_argument("--body", help="new body")
    p.add_argument("--cc", nargs="+", help="new CC list (replaces existing)")
    p.add_argument("--bcc", nargs="+", help="new BCC list (replaces existing)")
    p.add_argument("--attachments", nargs="+", help="additional attachment paths")
    add_live_mutation_override(p)

    # send-draft
    p = sub.add_parser("send-draft", help="send a draft by ID")
    p.add_argument("--id", required=True, help="draft ID")
    add_live_mutation_override(p)

    # reply-draft
    p = sub.add_parser("reply-draft", help="create a reply draft")
    p.add_argument("--id", default=None, help="original email integer ID")
    p.add_argument("--message-id", default=None, help="original email RFC 2822 message-id (preferred)")
    p.add_argument("--body", required=True, help="reply body")
    p.add_argument("--reply-all", action="store_true", help="reply to all recipients")
    p.add_argument("--cc", nargs="+", help="additional CC addresses")
    p.add_argument("--bcc", nargs="+", help="additional BCC addresses")
    p.add_argument("--attachments", nargs="+", help="file paths to attach")
    add_live_mutation_override(p)

    # forward-draft
    p = sub.add_parser("forward-draft", help="create a forward draft")
    p.add_argument("--id", default=None, help="original email integer ID")
    p.add_argument("--message-id", default=None, help="original email RFC 2822 message-id (preferred)")
    p.add_argument("--account", required=True, help="sending account email")
    p.add_argument("--body", required=True, help="forward body text")
    p.add_argument("--to", nargs="+", required=True, help="recipient addresses")
    p.add_argument("--cc", nargs="+", help="CC addresses")
    p.add_argument("--bcc", nargs="+", help="BCC addresses")
    p.add_argument("--attachments", nargs="+", help="file paths to attach")
    add_live_mutation_override(p)

    # delete-email (single and batch)
    p = sub.add_parser("delete-email", help="delete email(s) by ID")
    p.add_argument("--ids", nargs="+", default=None, help="email integer ID(s) to delete (unsafe; ids shift/collide -- prefer --message-ids)")
    p.add_argument("--message-ids", nargs="+", default=None, help="RFC 2822 message-id(s) to delete (preferred, stable)")
    p.add_argument("--force-int-ids", action="store_true", help="allow deletion by integer --ids despite the collision risk")
    add_live_mutation_override(p)

    # delete-draft
    p = sub.add_parser("delete-draft", help="delete a draft by ID")
    p.add_argument("--id", required=True, help="draft ID")
    add_live_mutation_override(p)

    # move-email
    p = sub.add_parser("move-email", help="move an email to a folder")
    p.add_argument("--id", default=None, help="email integer ID")
    p.add_argument("--message-id", default=None, help="RFC 2822 message-id (preferred)")
    p.add_argument("--to", required=True, help="destination folder name")
    p.add_argument("--to-account", default=None, help="destination account email (for cross-account moves)")
    add_live_mutation_override(p)

    # batch-move
    p = sub.add_parser("batch-move", help="batch-move emails to a folder")
    p.add_argument("--ids", nargs="+", default=None, help="email integer ID(s)")
    p.add_argument("--message-ids", nargs="+", default=None, help="RFC 2822 message-id(s) (preferred)")
    p.add_argument("--to", required=True, help="destination folder name")
    p.add_argument("--to-account", default=None, help="destination account email (cross-account)")
    add_live_mutation_override(p)

    # fix-spotlight
    sub.add_parser("fix-spotlight", help="disable Spotlight indexing for ~/Library/Mail (prevents mds_stores CPU drain)")

    # build-index
    sub.add_parser("build-index", help="build/rebuild FTS5 search index from disk")

    return parser


COMMAND_MAP = {
    "server-info": cmd_server_info,
    "check-health": cmd_check_health,
    "local-mutation-preflight": cmd_local_mutation_preflight,
    "list-accounts": cmd_list_accounts,
    "list-folders": cmd_list_folders,
    "list-recent": cmd_list_recent,
    "list-emails": cmd_list_emails,
    "list-drafts": cmd_list_drafts,
    "read-email": cmd_read_email,
    "search": cmd_search,
    "exchange-auth-status": cmd_exchange_auth_status,
    "exchange-auth-login": cmd_exchange_auth_login,
    "compose-draft": cmd_compose_draft,
    "amend-draft": cmd_amend_draft,
    "send-draft": cmd_send_draft,
    "reply-draft": cmd_reply_draft,
    "forward-draft": cmd_forward_draft,
    "delete-email": cmd_delete_email,
    "delete-draft": cmd_delete_draft,
    "move-email": cmd_move_email,
    "batch-move": cmd_batch_move,
    "fix-spotlight": cmd_fix_spotlight,
    "build-index": cmd_build_index,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    t0 = time.monotonic()
    handler = COMMAND_MAP.get(args.command)

    if handler:
        if _requires_mail_ui_mutation(args):
            if not _mail_ui_mutation_allowed(args):
                _output(_mutation_disabled_result(_mutation_command_key(args), t0))
                return
            _authorize_live_mail_mutation_for_ops(args)
        skip_lock = not _requires_mail_lock(args)
        lock_fd = None
        if not skip_lock:
            lock_fd = os.open(str(_MAIL_LOCK_PATH), os.O_RDWR | os.O_CREAT)
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() > deadline:
                        os.close(lock_fd)
                        _output(_wrap(
                            error=_error("LOCK_TIMEOUT", "another mail command has held the lock for >30s"),
                            command=args.command, start_time=t0,
                        ))
                        return
                    time.sleep(0.5)
        try:
            handler(args, t0)
        except Exception as e:
            _output(_wrap(
                error=_error("INTERNAL_ERROR", str(e)),
                command=args.command,
                start_time=t0,
            ))
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
