#!/usr/bin/env python3
"""Apple Mail CLI — agent-facing entry point.

All output is JSON to stdout. Exit 0 on success, 1 on error.
Response contract: {success, data, error, warnings, meta}
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = "1.0.0"


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


def _output(result: dict):
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    sys.exit(0 if result.get("success") else 1)


def _infer_error_code(message: str) -> str:
    """Map common operation failure messages to standard error codes."""
    msg = message.lower()
    if "not found" in msg:
        if "draft" in msg:
            return "DRAFT_NOT_FOUND"
        if "account" in msg:
            return "ACCOUNT_NOT_FOUND"
        if "folder" in msg:
            return "FOLDER_NOT_FOUND"
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
        code = _infer_error_code(str(msg))
        _output(_wrap(
            error=_error(code, str(msg)),
            command=command,
            start_time=t0,
        ))
    else:
        _output(_wrap(data=result, command=command, start_time=t0))


# ------------------------------------------------------------------
# ID resolution helpers
# ------------------------------------------------------------------


def _resolve_id_or_message_id(args, command: str, t0) -> tuple:
    """Return (identifier, by_message_id) from --id/--message-id args.

    Emits an error response and returns (None, False) when neither is provided.
    """
    mid = getattr(args, "message_id", None)
    iid = getattr(args, "id", None)
    if mid:
        return mid, True
    if iid:
        return iid, False
    _output(_wrap(
        error=_error("MISSING_ID", "provide either --id or --message-id"),
        command=command,
        start_time=t0,
    ))
    return None, False


def _resolve_ids_or_message_ids(args, command: str, t0) -> tuple:
    """Return (identifiers_list, by_message_id) from --ids/--message-ids args.

    Emits an error response and returns (None, False) when neither is provided.
    """
    mids = getattr(args, "message_ids", None)
    iids = getattr(args, "ids", None)
    if mids:
        return mids, True
    if iids:
        return iids, False
    _output(_wrap(
        error=_error("MISSING_ID", "provide either --ids or --message-ids"),
        command=command,
        start_time=t0,
    ))
    return None, False


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------


def cmd_server_info(args, t0):
    _output(_wrap(
        data={
            "name": "apple-mail-skill",
            "version": VERSION,
            "description": "cursor skill for apple mail on macos",
            "total_commands": 20,
        },
        command="server-info",
        start_time=t0,
    ))


def cmd_check_health(args, t0):
    from lib.ops.health import health_check

    result = health_check()
    _output_op(result, "check-health", t0)


def cmd_list_accounts(args, t0):
    from lib.ops.accounts import list_accounts

    result = list_accounts()
    _output(_wrap(data=result, command="list-accounts", start_time=t0))


def cmd_list_folders(args, t0):
    from lib.ops.accounts import list_account_folders

    result = list_account_folders(args.account)
    _output(_wrap(data=result, command="list-folders", start_time=t0))


def cmd_list_recent(args, t0):
    from lib.ops.accounts import list_recent_emails

    result = list_recent_emails(
        most_recent_n_emails=args.limit,
        include_content=args.include_content,
    )
    _output(_wrap(data=result, command="list-recent", start_time=t0))


def cmd_list_emails(args, t0):
    from lib.ops.folders import list_emails_in_folder

    result = list_emails_in_folder(
        account_email=args.account,
        folder_name=args.folder,
        limit=args.limit,
        include_content=args.include_content,
    )
    _output(_wrap(data=result, command="list-emails", start_time=t0))


def cmd_list_drafts(args, t0):
    from lib.ops.drafts import list_drafts

    result = list_drafts(limit=args.limit, include_content=args.include_content)
    _output(_wrap(data=result, command="list-drafts", start_time=t0))


def cmd_read_email(args, t0):
    from lib.ops.read import read_full_email

    identifier, by_mid = _resolve_id_or_message_id(args, "read-email", t0)
    if identifier is None:
        return

    result = read_full_email(identifier, by_message_id=by_mid)
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


def cmd_compose_draft(args, t0):
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

    identifier, by_mid = _resolve_id_or_message_id(args, "reply-draft", t0)
    if identifier is None:
        return

    result = reply_draft(
        original_email_id=identifier,
        body=args.body,
        reply_all=args.reply_all,
        extra_cc=args.cc,
        extra_bcc=args.bcc,
        extra_attachments=args.attachments,
        by_message_id=by_mid,
    )
    _output_op(result, "reply-draft", t0)


def cmd_forward_draft(args, t0):
    from lib.ops.forward import make_forward_draft

    identifier, by_mid = _resolve_id_or_message_id(args, "forward-draft", t0)
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
        by_message_id=by_mid,
    )
    _output_op(result, "forward-draft", t0)


def cmd_delete_email(args, t0):
    from lib.ops.delete import delete_email, delete_emails_batch

    identifiers, by_mid = _resolve_ids_or_message_ids(args, "delete-email", t0)
    if identifiers is None:
        return

    if len(identifiers) == 1:
        result = delete_email(identifiers[0], by_message_id=by_mid)
    else:
        result = delete_emails_batch(identifiers, by_message_id=by_mid)
    _output_op(result, "delete-email", t0)


def cmd_delete_draft(args, t0):
    from lib.ops.delete import delete_draft

    result = delete_draft(args.id)
    _output_op(result, "delete-draft", t0)


def cmd_move_email(args, t0):
    from lib.ops.move import move_email

    identifier, by_mid = _resolve_id_or_message_id(args, "move-email", t0)
    if identifier is None:
        return

    result = move_email(identifier, args.to, by_message_id=by_mid)
    _output_op(result, "move-email", t0)


def cmd_build_index(args, t0):
    from lib.ops.search import build_search_index

    result = build_search_index()
    _output_op(result, "build-index", t0)


def cmd_index_status(args, t0):
    from lib.search_index.schema import PROGRESS_PATH

    if not PROGRESS_PATH.exists():
        _output(_wrap(
            data={"status": "not_running", "message": "no background indexing in progress"},
            command="index-status",
            start_time=t0,
        ))
        return

    try:
        progress = json.loads(PROGRESS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        _output(_wrap(
            error=_error("PROGRESS_CORRUPTED", "index-progress.json is corrupted or unreadable", {
                "recovery": "delete the file and re-run build-index",
                "path": str(PROGRESS_PATH),
            }),
            command="index-status",
            start_time=t0,
        ))
        return

    STALE_TIMEOUT = 60

    status = progress.get("status", "unknown")
    pid = progress.get("pid")

    if status == "running" and pid:
        pid_alive = False
        try:
            os.kill(pid, 0)
            pid_alive = True
        except (OSError, ProcessLookupError):
            pass

        if not pid_alive:
            heartbeat = progress.get("last_heartbeat", "")
            progress["status"] = "stale"
            progress["stale_reason"] = f"pid {pid} is dead, last heartbeat: {heartbeat}"
            status = "stale"
        elif pid_alive:
            hb = progress.get("last_heartbeat", "")
            if hb:
                try:
                    hb_dt = datetime.fromisoformat(hb)
                    elapsed = (datetime.now() - hb_dt).total_seconds()
                    if elapsed > STALE_TIMEOUT:
                        progress["status"] = "stale"
                        progress["stale_reason"] = f"heartbeat timeout ({elapsed:.0f}s > {STALE_TIMEOUT}s), pid {pid} may be hung"
                        status = "stale"
                except (ValueError, TypeError):
                    pass

    total = progress.get("total", 0)
    completed = progress.get("completed", 0)
    pct = round(completed / total * 100, 1) if total else 0

    data = {
        "status": status,
        "total": total,
        "completed": completed,
        "failed": progress.get("failed", 0),
        "percentage": pct,
        "started": progress.get("started"),
        "last_heartbeat": progress.get("last_heartbeat"),
        "pid": pid,
    }

    if status == "stale":
        data["stale_reason"] = progress.get("stale_reason", "")

    _output(_wrap(data=data, command="index-status", start_time=t0))


def cmd_index_cancel(args, t0):
    import signal
    from lib.search_index.schema import PROGRESS_PATH, LOCK_PATH

    if not PROGRESS_PATH.exists():
        _output(_wrap(
            data={"status": "not_running", "message": "no background indexing to cancel"},
            command="index-cancel",
            start_time=t0,
        ))
        return

    try:
        progress = json.loads(PROGRESS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        _output(_wrap(
            error=_error("PROGRESS_CORRUPTED", "index-progress.json is corrupted"),
            command="index-cancel",
            start_time=t0,
        ))
        return

    pid = progress.get("pid")
    if not pid:
        progress["status"] = "cancelled"
        PROGRESS_PATH.write_text(json.dumps(progress, indent=2))
        _output(_wrap(
            data={"status": "cancelled", "message": "no PID found, marked as cancelled"},
            command="index-cancel",
            start_time=t0,
        ))
        return

    killed = False
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        killed = True
    except (OSError, ProcessLookupError):
        killed = True

    progress["status"] = "cancelled"
    progress["last_updated"] = datetime.now().isoformat()
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2))

    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass

    _output(_wrap(
        data={"status": "cancelled", "pid": pid, "killed": killed},
        command="index-cancel",
        start_time=t0,
    ))


def cmd_background_index(args, t0):
    """Hidden command: fetch content for specific IDs via JXA and cache them.

    Lock-protected, heartbeat-emitting, batched-commit background worker.
    Idempotent: rechecks remaining_ids against DB before processing.
    """
    import fcntl
    from lib.jxa import run_jxa_with_core, _atomic_write_json, _is_pid_alive
    from lib.search_index import SearchIndexManager
    from lib.search_index.schema import PROGRESS_PATH, LOCK_PATH

    HEARTBEAT_INTERVAL = 10
    BATCH_COMMIT_SIZE = 10
    MAX_RETRIES = 3

    msg_ids = [int(i) for i in args.ids]
    pid = os.getpid()

    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = None
    try:
        lock_fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            if PROGRESS_PATH.exists():
                try:
                    existing = json.loads(PROGRESS_PATH.read_text())
                    existing_pid = existing.get("pid")
                    if existing_pid and not _is_pid_alive(existing_pid):
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    else:
                        os.close(lock_fd)
                        _output(_wrap(
                            data={"status": "already_running", "pid": existing_pid},
                            command="_background-index",
                            start_time=t0,
                        ))
                        return
                except Exception:
                    os.close(lock_fd)
                    return
            else:
                os.close(lock_fd)
                return

        progress = {
            "status": "running",
            "started": datetime.now().isoformat(),
            "pid": pid,
            "total": len(msg_ids),
            "completed": 0,
            "failed": 0,
            "remaining_ids": msg_ids,
            "attempted_ids": [],
            "queue_version": 1,
            "last_heartbeat": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        }
        _atomic_write_json(PROGRESS_PATH, progress)

        mgr = SearchIndexManager()
        try:
            already_cached = mgr.batch_content(msg_ids)
            msg_ids = [mid for mid in msg_ids if mid not in already_cached]
            progress["remaining_ids"] = msg_ids
            progress["completed"] = progress["total"] - len(msg_ids)
            _atomic_write_json(PROGRESS_PATH, progress)

            last_heartbeat = time.monotonic()
            uncommitted = 0

            while progress["remaining_ids"]:
                current_progress = None
                try:
                    current_progress = json.loads(PROGRESS_PATH.read_text())
                except Exception:
                    pass

                if current_progress:
                    if current_progress.get("status") == "cancelled":
                        break
                    merged_remaining = set(current_progress.get("remaining_ids", []))
                    progress["remaining_ids"] = sorted(merged_remaining)
                    progress["total"] = len(progress["attempted_ids"]) + len(progress["remaining_ids"])

                if not progress["remaining_ids"]:
                    break

                mid = progress["remaining_ids"][0]
                success = False

                for attempt in range(MAX_RETRIES):
                    try:
                        script = f"""
var msg = MailCore.findMessageAcrossAccounts({mid});
if (msg) {{
    var content = "";
    try {{ content = msg.content() || ""; }} catch(e) {{}}
    JSON.stringify({{id: {mid}, content: content}});
}} else {{
    JSON.stringify({{id: {mid}, content: ""}});
}}"""
                        result = run_jxa_with_core(script, timeout=15)
                        if result and result.get("content"):
                            mgr.cache_content(
                                message_id=mid,
                                subject="",
                                sender="",
                                content=result["content"],
                                date_received="",
                            )
                            uncommitted += 1
                        success = True
                        break
                    except Exception:
                        if attempt < MAX_RETRIES - 1:
                            delay = min(30, (2 ** attempt) + (time.monotonic() % 1))
                            time.sleep(delay)

                progress["attempted_ids"].append(mid)
                progress["remaining_ids"] = [i for i in progress["remaining_ids"] if i != mid]
                if success:
                    progress["completed"] += 1
                else:
                    progress["failed"] += 1

                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL or not progress["remaining_ids"]:
                    progress["last_heartbeat"] = datetime.now().isoformat()
                    progress["last_updated"] = datetime.now().isoformat()
                    _atomic_write_json(PROGRESS_PATH, progress)
                    last_heartbeat = now

            final_status = "done" if progress["failed"] == 0 else "failed"
            if progress.get("status") == "cancelled" or (
                current_progress and current_progress.get("status") == "cancelled"
            ):
                final_status = "cancelled"

            progress["status"] = final_status
            progress["last_updated"] = datetime.now().isoformat()
            progress["last_heartbeat"] = datetime.now().isoformat()
            _atomic_write_json(PROGRESS_PATH, progress)

        except Exception as e:
            progress["status"] = "failed"
            progress["error"] = str(e)
            progress["last_updated"] = datetime.now().isoformat()
            _atomic_write_json(PROGRESS_PATH, progress)
        finally:
            mgr.close()

        _output(_wrap(data=progress, command="_background-index", start_time=t0))

    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


# ------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mail.py",
        description="Apple Mail CLI — agent-facing tool for reading, writing, and managing emails on macOS.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", help="available commands")

    # server-info
    sub.add_parser("server-info", help="show server/skill info")

    # check-health
    sub.add_parser("check-health", help="verify Mail.app is responding")

    # list-accounts
    sub.add_parser("list-accounts", help="list all mail accounts")

    # list-folders
    p = sub.add_parser("list-folders", help="list folders for an account")
    p.add_argument("--account", required=True, help="account email address")

    # list-recent
    p = sub.add_parser("list-recent", help="list recent emails from all inboxes")
    p.add_argument("--limit", type=int, default=20, help="max emails per inbox (default: 20)")
    p.add_argument("--include-content", action="store_true", help="add preview from search index")

    # list-emails
    p = sub.add_parser("list-emails", help="list emails in a specific folder")
    p.add_argument("--account", required=True, help="account email address")
    p.add_argument("--folder", required=True, help="folder name (case-insensitive)")
    p.add_argument("--limit", type=int, default=50, help="max emails (default: 50)")
    p.add_argument("--include-content", action="store_true", help="add preview from search index")

    # list-drafts
    p = sub.add_parser("list-drafts", help="list drafts across all accounts")
    p.add_argument("--limit", type=int, default=50, help="max drafts (default: 50)")
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
    p.add_argument("--limit", type=int, default=20, help="max results (default: 20)")

    # compose-draft
    p = sub.add_parser("compose-draft", help="create a new draft email")
    p.add_argument("--account", required=True, help="sending account email")
    p.add_argument("--subject", required=True, help="email subject")
    p.add_argument("--body", required=True, help="email body")
    p.add_argument("--to", nargs="+", required=True, help="recipient addresses")
    p.add_argument("--cc", nargs="+", help="CC addresses")
    p.add_argument("--bcc", nargs="+", help="BCC addresses")
    p.add_argument("--attachments", nargs="+", help="file paths to attach")

    # amend-draft
    p = sub.add_parser("amend-draft", help="amend an existing draft")
    p.add_argument("--id", required=True, help="draft ID")
    p.add_argument("--subject", help="new subject")
    p.add_argument("--body", help="new body")
    p.add_argument("--cc", nargs="+", help="new CC list (replaces existing)")
    p.add_argument("--bcc", nargs="+", help="new BCC list (replaces existing)")
    p.add_argument("--attachments", nargs="+", help="additional attachment paths")

    # send-draft
    p = sub.add_parser("send-draft", help="send a draft by ID")
    p.add_argument("--id", required=True, help="draft ID")

    # reply-draft
    p = sub.add_parser("reply-draft", help="create a reply draft")
    p.add_argument("--id", default=None, help="original email integer ID")
    p.add_argument("--message-id", default=None, help="original email RFC 2822 message-id (preferred)")
    p.add_argument("--body", required=True, help="reply body")
    p.add_argument("--reply-all", action="store_true", help="reply to all recipients")
    p.add_argument("--cc", nargs="+", help="additional CC addresses")
    p.add_argument("--bcc", nargs="+", help="additional BCC addresses")
    p.add_argument("--attachments", nargs="+", help="file paths to attach")

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

    # delete-email (single and batch)
    p = sub.add_parser("delete-email", help="delete email(s) by ID")
    p.add_argument("--ids", nargs="+", default=None, help="email integer ID(s) to delete")
    p.add_argument("--message-ids", nargs="+", default=None, help="RFC 2822 message-id(s) to delete (preferred)")

    # delete-draft
    p = sub.add_parser("delete-draft", help="delete a draft by ID")
    p.add_argument("--id", required=True, help="draft ID")

    # move-email
    p = sub.add_parser("move-email", help="move an email to a folder")
    p.add_argument("--id", default=None, help="email integer ID")
    p.add_argument("--message-id", default=None, help="RFC 2822 message-id (preferred)")
    p.add_argument("--to", required=True, help="destination folder name")

    # build-index
    sub.add_parser("build-index", help="build/rebuild FTS5 search index from disk")

    # index-status
    sub.add_parser("index-status", help="check background indexing progress")

    # index-cancel
    sub.add_parser("index-cancel", help="cancel background indexing")

    # _background-index (hidden)
    p = sub.add_parser("_background-index")
    p.add_argument("--ids", nargs="+", required=True, help=argparse.SUPPRESS)

    return parser


COMMAND_MAP = {
    "server-info": cmd_server_info,
    "check-health": cmd_check_health,
    "list-accounts": cmd_list_accounts,
    "list-folders": cmd_list_folders,
    "list-recent": cmd_list_recent,
    "list-emails": cmd_list_emails,
    "list-drafts": cmd_list_drafts,
    "read-email": cmd_read_email,
    "search": cmd_search,
    "compose-draft": cmd_compose_draft,
    "amend-draft": cmd_amend_draft,
    "send-draft": cmd_send_draft,
    "reply-draft": cmd_reply_draft,
    "forward-draft": cmd_forward_draft,
    "delete-email": cmd_delete_email,
    "delete-draft": cmd_delete_draft,
    "move-email": cmd_move_email,
    "build-index": cmd_build_index,
    "index-status": cmd_index_status,
    "index-cancel": cmd_index_cancel,
    "_background-index": cmd_background_index,
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
        try:
            handler(args, t0)
        except Exception as e:
            _output(_wrap(
                error=_error("INTERNAL_ERROR", str(e)),
                command=args.command,
                start_time=t0,
            ))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
