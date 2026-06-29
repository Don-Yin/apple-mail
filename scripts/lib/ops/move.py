"""Email move operations (single and batch) via JXA."""

import json
from ..jxa import run_jxa_with_core, JXAError
from ..applescript import validate_id, sync_mail_state
from .mutation_guard import require_live_mail_mutation, run_guarded_local_mail_mutation


def move_email(identifier: str, to_folder: str, to_account: str = None) -> dict:
    """move an email to a specific folder, optionally across accounts."""
    guard = require_live_mail_mutation("move-email")
    if guard:
        return guard

    try:
        identifier = validate_id(identifier, "email_id")
    except ValueError as e:
        return {"success": False, "message": str(e)}

    safe_folder = json.dumps(to_folder)
    safe_account = json.dumps(to_account) if to_account else "null"

    script = f"""
var _r;
var msg = MailCore.findMessageAcrossAccounts({identifier});
if (!msg) {{
    _r = {{success: false, error: "EMAIL_NOT_FOUND"}};
}} else {{
    try {{
        var destAcc = {safe_account} ? MailCore.getAccountByEmail({safe_account}) : msg.mailbox().account();
        var destMbox = MailCore.getMailbox(destAcc, {safe_folder});
        var moveResult = MailCore.moveMessage(msg, destMbox);
        _r = {{success: moveResult.method !== "failed", method: moveResult.method, error: moveResult.error || null}};
    }} catch(e) {{
        var code = e.message && e.message.indexOf("no account") >= 0 ? "DEST_ACCOUNT_NOT_FOUND" : "FOLDER_NOT_FOUND";
        _r = {{success: false, error: code, detail: e.message}};
    }}
}}
JSON.stringify(_r);"""

    def action() -> dict:
        try:
            result = run_jxa_with_core(script)
        except (TimeoutError, JXAError) as e:
            return {"success": False, "message": str(e)}

        if not result.get("success"):
            error = result.get("error", "unknown")
            detail = result.get("detail", "")
            match error:
                case "EMAIL_NOT_FOUND":
                    return {"success": False, "message": f"email with id {identifier} not found"}
                case "DEST_ACCOUNT_NOT_FOUND":
                    return {"success": False, "message": f"no account found for email '{to_account}'"}
                case "FOLDER_NOT_FOUND":
                    dest = f" (searched {to_account})" if to_account else ""
                    return {"success": False, "message": f"folder '{to_folder}' not found{dest}"}
                case _:
                    return {"success": False, "message": f"move failed: {detail or error}"}

        sync_mail_state(delay_seconds=1.0, preserve_focus=True)
        dest = f"{to_account}/{to_folder}" if to_account else to_folder
        return {"success": True, "message": f"email moved to {dest} successfully", "method": result.get("method")}

    return run_guarded_local_mail_mutation("move-email", action)


def batch_move_emails(identifiers: list[str], to_folder: str, to_account: str = None) -> dict:
    """batch-move emails to a folder via JXA with JSON output."""
    guard = require_live_mail_mutation("batch-move")
    if guard:
        return guard

    try:
        validated = [validate_id(eid) for eid in identifiers]
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if not validated:
        return {"success": False, "message": "no email ids provided", "moved": 0, "requested": 0, "not_found": []}

    ids_js = "[" + ",".join(validated) + "]"
    safe_folder = json.dumps(to_folder)
    safe_account = json.dumps(to_account) if to_account else "null"
    count = len(validated)

    script = f"""
var _r;
try {{
    var destAcc = {safe_account} ? MailCore.getAccountByEmail({safe_account}) : Mail.accounts()[0];
    var destMbox = MailCore.getMailbox(destAcc, {safe_folder});
    var targetIds = {ids_js};
    var moved = 0, notFound = [], failed = [];

    for (var i = 0; i < targetIds.length; i++) {{
        var tid = targetIds[i];
        var msg = MailCore.findMessageAcrossAccounts(tid);
        if (!msg) {{ notFound.push(tid); continue; }}
        var result = MailCore.moveMessage(msg, destMbox);
        if (result.method === "failed") {{
            failed.push({{id: tid, error: result.error}});
        }} else {{
            moved++;
        }}
    }}
    _r = {{moved: moved, not_found: notFound, failed: failed}};
}} catch(e) {{
    var code = e.message && e.message.indexOf("no account") >= 0 ? "DEST_ACCOUNT_NOT_FOUND" : "FOLDER_NOT_FOUND";
    _r = {{error: code, detail: e.message}};
}}
JSON.stringify(_r);"""

    def action() -> dict:
        try:
            result = run_jxa_with_core(script, timeout=max(120, count * 5))
        except (TimeoutError, JXAError) as e:
            return {"success": False, "message": str(e)}

        if result.get("error") == "DEST_ACCOUNT_NOT_FOUND":
            return {"success": False, "message": f"no account found for email '{to_account}'"}
        if result.get("error") == "FOLDER_NOT_FOUND":
            return {"success": False, "message": f"folder '{to_folder}' not found"}

        moved_count = result.get("moved", 0)
        not_found = [str(x) for x in result.get("not_found", [])]
        failed = result.get("failed", [])

        if moved_count > 0:
            sync_mail_state(delay_seconds=1.0, preserve_focus=True)

        dest = f"{to_account}/{to_folder}" if to_account else to_folder
        msg = f"moved {moved_count}/{count} emails to {dest}"
        if not_found:
            msg += f", {len(not_found)} not found"
        if failed:
            msg += f", {len(failed)} failed (MIME/encoding error)"

        out = {
            "success": moved_count > 0,
            "moved": moved_count,
            "requested": count,
            "not_found": not_found,
            "message": msg,
        }
        if failed:
            out["failed"] = failed
        return out

    return run_guarded_local_mail_mutation("batch-move", action)
