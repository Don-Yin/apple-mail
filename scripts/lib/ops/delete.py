"""Email and draft deletion operations."""
import textwrap
from ..applescript import validate_id, run_applescript, sync_mail_state


def _remove_from_index(int_id: int):
    """remove email from FTS index after deletion."""
    from ..search_index.manager import SearchIndexManager
    with SearchIndexManager() as mgr:
        mgr.remove_by_int_id(int_id)

def delete_draft(draft_id: str) -> dict:
    """Delete a draft email by its ID."""
    try:
        draft_id = validate_id(draft_id)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundMessage to missing value

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            exit repeat
                        end if
                    end if
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat

            if foundMessage is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            delete foundMessage
            return "SUCCESS"
        end tell
        """
    )

    try:
        result = run_applescript(script)
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()

    if output == "DRAFT_NOT_FOUND":
        return {"success": False, "message": f"draft with id {draft_id} not found"}
    if output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "draft deleted successfully"}
    return {"success": False, "message": f"unexpected output: {output}"}


def _build_delete_by_int_script(email_id: str) -> str:
    """Build AppleScript to delete an email by integer id (inbox first, then all)."""
    return textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {email_id} as integer
            set foundMessage to missing value

            -- phase 1: find by integer id in inbox first
            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isInbox to (name of mbox is "inbox")
                    end ignoring
                    if isInbox then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            exit repeat
                        end if
                    end if
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat

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
    try:
        identifier = validate_id(identifier)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    script = _build_delete_by_int_script(identifier)

    try:
        result = run_applescript(script)
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()

    if output == "EMAIL_NOT_FOUND":
        return {"success": False, "message": f"email with id {identifier} not found"}
    if output == "SUCCESS":
        sync_mail_state()
        _remove_from_index(int(identifier))
        return {"success": True, "message": "email deleted successfully"}
    return {"success": False, "message": f"unexpected output: {output}"}


def _build_batch_delete_by_int_script(email_ids: list[str]) -> str:
    """Build AppleScript to batch-delete emails by integer ids."""
    ids_literal = ", ".join(email_ids)

    return textwrap.dedent(
        f"""
        tell application "Mail"
            set targetIds to {{{ids_literal}}}
            set deletedCount to 0
            set remainingIds to {{}}

            -- phase 1: try inbox integer ID
            repeat with tid in targetIds
                set targetId to tid as integer
                set foundMessage to missing value

                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        ignoring case
                            set isInbox to (name of mbox is "inbox")
                        end ignoring
                        if isInbox then
                            set msgList to (messages of mbox whose id is targetId)
                            if (count of msgList) > 0 then
                                set foundMessage to item 1 of msgList
                                exit repeat
                            end if
                        end if
                    end repeat
                    if foundMessage is not missing value then exit repeat
                end repeat

                if foundMessage is not missing value then
                    delete foundMessage
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


def delete_emails_batch(identifiers: list[str]) -> dict:
    """Delete multiple emails in a single AppleScript call."""
    try:
        validated = [validate_id(eid) for eid in identifiers]
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if not validated:
        return {"success": False, "message": "no email ids provided", "deleted": 0, "requested": 0, "not_found": []}
    script = _build_batch_delete_by_int_script(validated)
    count = len(validated)

    try:
        result = run_applescript(script)
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()
    parts = output.split("|||")
    deleted_count = int(parts[0]) if parts[0].isdigit() else 0
    not_found = [x.strip() for x in parts[1].split(",") if x.strip()] if len(parts) > 1 and parts[1].strip() else []

    if deleted_count > 0:
        sync_mail_state()
        not_found_set = set(not_found)
        for eid in validated:
            if eid not in not_found_set:
                _remove_from_index(int(eid))

    return {
        "success": deleted_count > 0,
        "deleted": deleted_count,
        "requested": count,
        "not_found": not_found,
        "message": f"deleted {deleted_count}/{count} emails" + (f", {len(not_found)} not found" if not_found else ""),
    }
