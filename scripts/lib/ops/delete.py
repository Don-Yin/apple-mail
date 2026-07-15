"""Email and draft deletion operations.

`delete-email` deletes by MOVING the message to its own account's Trash / Deleted Items
mailbox (a same-account move via JXA property assignment; see MailCore.moveMessage). This
was proven in a live canary to (a) sync to the server on Exchange and (b) never trigger the
Mail crash that the AppleScript `delete` verb did -- so it is enabled by default. Draft
deletion still uses the AppleScript `delete` verb and stays gated (see mutation_guard).
"""
import textwrap
from ..applescript import validate_id, run_applescript, sync_mail_state
from ..jxa import run_jxa_with_core, JXAError
from .mutation_guard import require_live_mail_mutation, run_guarded_local_mail_mutation


def _remove_from_index(int_id: int):
    """remove email from FTS index after deletion. best-effort by design: the Mail.app
    deletion has already committed, so a failure in this secondary index bookkeeping must
    NEVER propagate and make a completed (irreversible) deletion look like it failed."""
    try:
        from ..search_index.manager import SearchIndexManager
        with SearchIndexManager() as mgr:
            mgr.remove_by_int_id(int_id)
    except Exception:
        pass

def delete_draft(draft_id: str) -> dict:
    """Delete a draft email by its ID."""
    guard = require_live_mail_mutation("delete-draft")
    if guard:
        return guard

    try:
        draft_id = validate_id(draft_id)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundMessage to missing value
            set targetMessageId to ""

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            try
                                set targetMessageId to message id of foundMessage
                            end try
                            exit repeat
                        end if
                    end if
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat

            if foundMessage is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            repeat with deleteAttempt from 1 to 5
                set deletedCount to 0
                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        ignoring case
                            set isDraft to (name of mbox contains "draft")
                        end ignoring
                        if isDraft then
                            if targetMessageId is not "" then
                                set msgList to (messages of mbox whose message id is targetMessageId)
                            else
                                set msgList to (messages of mbox whose id is targetId)
                            end if
                            repeat with msg in msgList
                                try
                                    delete msg
                                    set deletedCount to deletedCount + 1
                                end try
                            end repeat
                        end if
                    end repeat
                end repeat

                delay 0.8

                set remainingCount to 0
                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        ignoring case
                            set isDraft to (name of mbox contains "draft")
                        end ignoring
                        if isDraft then
                            if targetMessageId is not "" then
                                set remainingCount to remainingCount + (count of (messages of mbox whose message id is targetMessageId))
                            else
                                set remainingCount to remainingCount + (count of (messages of mbox whose id is targetId))
                            end if
                        end if
                    end repeat
                end repeat

                if remainingCount is 0 then
                    return "SUCCESS"
                end if
            end repeat

            return "DELETE_NOT_CONFIRMED"
        end tell
        """
    )

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except (TimeoutError, RuntimeError) as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "DRAFT_NOT_FOUND":
            return {"success": False, "message": f"draft with id {draft_id} not found"}
        if output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            return {"success": True, "message": "draft deleted successfully"}
        if output == "DELETE_NOT_CONFIRMED":
            return {"success": False, "message": f"draft with id {draft_id} was deleted but Mail still reports a matching draft"}
        return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("delete-draft", action)


def _build_delete_to_trash_script(email_id: str) -> str:
    """Build JXA to delete one email by moving it to its account's Trash mailbox.

    "delete" == a same-account move into the account's Deleted Items/Trash (property
    assignment in MailCore.moveMessage). This syncs to the server and avoids the
    AppleScript `delete` verb that crashed Mail.
    """
    return f"""
var _r;
var msg = MailCore.findMessageAcrossAccounts({email_id});
if (!msg) {{
    _r = {{success: false, error: "EMAIL_NOT_FOUND"}};
}} else {{
    try {{
        var trash = MailCore.trashMailboxFor(msg.mailbox().account());
        var res = MailCore.moveMessage(msg, trash);
        _r = {{success: res.method !== "failed", method: res.method, trash: trash.name(), error: res.error || null}};
    }} catch(e) {{
        _r = {{success: false, error: "TRASH_NOT_FOUND", detail: e.message}};
    }}
}}
JSON.stringify(_r);"""


def delete_email(identifier: str) -> dict:
    """Delete an email by moving it to its account's Trash (proven safe: syncs, no crash)."""
    guard = require_live_mail_mutation("delete-email")
    if guard:
        return guard

    try:
        identifier = validate_id(identifier)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    script = _build_delete_to_trash_script(identifier)

    def action() -> dict:
        try:
            result = run_jxa_with_core(script)
        except (TimeoutError, JXAError) as e:
            return {"success": False, "message": str(e)}

        if not result.get("success"):
            error = result.get("error", "unknown")
            if error == "EMAIL_NOT_FOUND":
                return {"success": False, "message": f"email with id {identifier} not found"}
            if error == "TRASH_NOT_FOUND":
                return {"success": False, "message": f"could not resolve a Trash mailbox for the message's account: {result.get('detail', '')}"}
            return {"success": False, "message": f"delete failed: {result.get('detail') or error}"}

        sync_mail_state(delay_seconds=1.0, preserve_focus=True)
        _remove_from_index(int(identifier))
        return {"success": True, "message": f"email moved to Trash ({result.get('trash')})", "method": result.get("method")}

    return run_guarded_local_mail_mutation("delete-email", action)


def _build_batch_delete_to_trash_script(email_ids: list[str]) -> str:
    """Build JXA to batch-delete emails by moving each to its account's Trash."""
    ids_js = "[" + ", ".join(email_ids) + "]"

    return f"""
var _r;
var targetIds = {ids_js};
var deleted = 0;
var notFound = [];
var failed = [];

for (var i = 0; i < targetIds.length; i++) {{
    var tid = targetIds[i];
    var msg = MailCore.findMessageAcrossAccounts(tid);
    if (!msg) {{ notFound.push(tid); continue; }}
    try {{
        var trash = MailCore.trashMailboxFor(msg.mailbox().account());
        var res = MailCore.moveMessage(msg, trash);
        if (res.method === "failed") {{ failed.push({{id: tid, error: res.error}}); }}
        else {{ deleted++; }}
    }} catch(e) {{
        failed.push({{id: tid, error: e.message}});
    }}
}}

_r = {{deleted: deleted, not_found: notFound, failed: failed}};
JSON.stringify(_r);"""


# delete == move-to-Trash; chunk batches to keep each JXA round-trip well under the timeout.
_DELETE_CHUNK = 25


def delete_emails_batch(identifiers: list[str]) -> dict:
    """Delete multiple emails by moving each to its account's Trash, chunked + crash-guarded.

    Surfaces partial progress on error rather than reporting total failure (a timeout
    mid-batch must never silently claim nothing was deleted).
    """
    guard = require_live_mail_mutation("delete-email")
    if guard:
        return guard

    try:
        validated = [validate_id(eid) for eid in identifiers]
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if not validated:
        return {"success": False, "message": "no email ids provided", "deleted": 0, "requested": 0, "not_found": []}

    def action() -> dict:
        count = len(validated)
        deleted_total = 0
        not_found_all: list[str] = []
        failed_all: list = []

        for start in range(0, count, _DELETE_CHUNK):
            chunk = validated[start:start + _DELETE_CHUNK]
            script = _build_batch_delete_to_trash_script(chunk)
            try:
                result = run_jxa_with_core(script, timeout=max(120, len(chunk) * 5))
            except (TimeoutError, JXAError) as e:
                # the errored chunk may have moved SOME ids before the kill. never silently
                # claim those are intact: report this chunk + all un-attempted ids as
                # UNKNOWN so the caller re-verifies rather than trusting the count.
                unknown = validated[start:]
                return {
                    "success": deleted_total > 0,
                    "deleted": deleted_total,
                    "requested": count,
                    "not_found": not_found_all,
                    "unknown_state": unknown,
                    "message": (f"deleted {deleted_total}/{count}; the chunk at #{start} errored ({e}). "
                                f"{len(unknown)} id(s) are in UNKNOWN state (this chunk may be partially "
                                f"deleted) -- re-verify with list-emails before retrying."),
                    "error": str(e),
                }

            chunk_deleted = int(result.get("deleted", 0))
            chunk_not_found = [str(x) for x in result.get("not_found", [])]
            failed_all.extend(result.get("failed", []))
            deleted_total += chunk_deleted
            not_found_all.extend(chunk_not_found)

            if chunk_deleted > 0:
                sync_mail_state(delay_seconds=1.0, preserve_focus=True)
                nf = set(chunk_not_found)
                for eid in chunk:
                    if eid not in nf:
                        _remove_from_index(int(eid))

        msg = f"deleted {deleted_total}/{count} emails (moved to Trash)"
        if not_found_all:
            msg += f", {len(not_found_all)} not found"
        if failed_all:
            msg += f", {len(failed_all)} failed"
        out = {
            "success": deleted_total > 0,
            "deleted": deleted_total,
            "requested": count,
            "not_found": not_found_all,
            "message": msg,
        }
        if failed_all:
            out["failed"] = failed_all
        return out

    return run_guarded_local_mail_mutation("delete-email", action)
