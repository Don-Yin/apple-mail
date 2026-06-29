"""Email and draft deletion operations."""
import textwrap
from ..applescript import validate_id, run_applescript, sync_mail_state
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


def _build_delete_by_int_script(email_id: str) -> str:
    """Build AppleScript to delete an email by integer id (inbox first, then all)."""
    return textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {email_id} as integer
            set foundMessage to missing value

            -- phase 1: unified inbox (locale-proof: covers Inbox/INBOX/收件箱 in one query)
            set msgList to (messages of inbox whose id is targetId)
            if (count of msgList) > 0 then
                set foundMessage to item 1 of msgList
            end if

            if foundMessage is missing value then
                -- phase 2: find by integer id in all folders
                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            exit repeat
                        end if
                    end repeat
                    if foundMessage is not missing value then exit repeat
                end repeat
            end if

            if foundMessage is missing value then
                return "EMAIL_NOT_FOUND"
            end if

            delete foundMessage
            return "SUCCESS"
        end tell
        """
    )


def delete_email(identifier: str) -> dict:
    """Delete any email by its ID across all accounts and folders."""
    guard = require_live_mail_mutation("delete-email")
    if guard:
        return guard

    try:
        identifier = validate_id(identifier)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    script = _build_delete_by_int_script(identifier)

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except (TimeoutError, RuntimeError) as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "EMAIL_NOT_FOUND":
            return {"success": False, "message": f"email with id {identifier} not found"}
        if output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            _remove_from_index(int(identifier))
            return {"success": True, "message": "email deleted successfully"}
        return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("delete-email", action)


def _build_batch_delete_by_int_script(email_ids: list[str]) -> str:
    """Build AppleScript to batch-delete emails by integer ids."""
    ids_literal = ", ".join(email_ids)

    return textwrap.dedent(
        f"""
        tell application "Mail"
            set targetIds to {{{ids_literal}}}
            set deletedCount to 0
            set remainingIds to {{}}

            -- phase 1: unified inbox (locale-proof, single query per id)
            repeat with tid in targetIds
                set targetId to tid as integer
                set msgList to (messages of inbox whose id is targetId)
                if (count of msgList) > 0 then
                    delete (item 1 of msgList)
                    set deletedCount to deletedCount + 1
                else
                    set end of remainingIds to tid
                end if
            end repeat

            -- phase 2: remaining by integer ID in all folders
            set notFoundIds to {{}}
            repeat with tid in remainingIds
                set targetId to tid as integer
                set foundMessage to missing value

                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            exit repeat
                        end if
                    end repeat
                    if foundMessage is not missing value then exit repeat
                end repeat

                if foundMessage is not missing value then
                    delete foundMessage
                    set deletedCount to deletedCount + 1
                else
                    set end of notFoundIds to (tid as string)
                end if
            end repeat

            set oldDelimiters to AppleScript's text item delimiters
            set AppleScript's text item delimiters to ","
            set notFoundStr to notFoundIds as string
            set AppleScript's text item delimiters to oldDelimiters

            return (deletedCount as string) & "|||" & notFoundStr
        end tell
        """
    )


# Exchange deletes are server round-trips; an AppleScript scanning all folders for
# >~50 ids exceeds the 120s timeout. 10/call is the empirically-proven-safe size
# (55 timed out; chunks of 10 completed cleanly), leaving headroom under 120s.
_DELETE_CHUNK = 10


def delete_emails_batch(identifiers: list[str]) -> dict:
    """Delete multiple emails, chunked to stay under the AppleScript timeout.

    Surfaces partial progress on error rather than reporting total failure (a
    timeout mid-batch must never silently claim nothing was deleted).
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

        for start in range(0, count, _DELETE_CHUNK):
            chunk = validated[start:start + _DELETE_CHUNK]
            script = _build_batch_delete_by_int_script(chunk)
            try:
                result = run_applescript(script, preserve_focus=True)
            except (TimeoutError, RuntimeError) as e:
                # the errored chunk may have deleted SOME ids before the kill (AppleScript
                # deletes are immediate and its stdout is lost on timeout). never silently
                # claim those are intact: report this chunk + all un-attempted ids as
                # UNKNOWN state so the caller re-verifies rather than trusting the count.
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

            output = result.stdout.strip()
            parts = output.split("|||")
            chunk_deleted = int(parts[0]) if parts[0].isdigit() else 0
            chunk_not_found = [x.strip() for x in parts[1].split(",") if x.strip()] if len(parts) > 1 and parts[1].strip() else []
            deleted_total += chunk_deleted
            not_found_all.extend(chunk_not_found)

            if chunk_deleted > 0:
                sync_mail_state(preserve_focus=True)
                nf = set(chunk_not_found)
                for eid in chunk:
                    if eid not in nf:
                        _remove_from_index(int(eid))

        return {
            "success": deleted_total > 0,
            "deleted": deleted_total,
            "requested": count,
            "not_found": not_found_all,
            "message": f"deleted {deleted_total}/{count} emails" + (f", {len(not_found_all)} not found" if not_found_all else ""),
        }

    return run_guarded_local_mail_mutation("delete-email", action)
