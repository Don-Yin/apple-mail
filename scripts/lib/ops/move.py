"""Email move operations (single and batch)."""

import textwrap
from ..applescript import validate_id, escape_applescript, run_applescript, sync_mail_state, build_find_by_int_block


def _dest_account_block(account_email: str = None) -> str:
    """build applescript fragment to resolve the destination account."""
    if account_email:
        escaped = escape_applescript(account_email)
        return f'''
            set destAccount to missing value
            set targetEmail to "{escaped}"
            repeat with acc in accounts
                set accEmails to email addresses of acc
                repeat with addr in accEmails
                    if (contents of addr) is targetEmail then
                        set destAccount to acc
                        exit repeat
                    end if
                end repeat
                if destAccount is not missing value then exit repeat
            end repeat
            if destAccount is missing value then
                return "DEST_ACCOUNT_NOT_FOUND"
            end if'''
    return "set destAccount to sourceAccount"


def _folder_search_block(folder: str) -> str:
    """build applescript BFS fragment to find a mailbox by name under destAccount."""
    escaped = escape_applescript(folder)
    return f'''
            set targetMailbox to missing value
            set mailboxQueue to mailboxes of destAccount
            repeat while (count of mailboxQueue) > 0
                set currentMbox to item 1 of mailboxQueue
                try
                    ignoring case
                        set folderMatch to ((name of currentMbox) is "{escaped}")
                    end ignoring
                    if folderMatch then
                        set targetMailbox to currentMbox
                        exit repeat
                    end if
                    set subMailboxes to mailboxes of currentMbox
                    repeat with subMbox in subMailboxes
                        set end of mailboxQueue to subMbox
                    end repeat
                end try
                set mailboxQueue to rest of mailboxQueue
            end repeat
            if targetMailbox is missing value then
                return "FOLDER_NOT_FOUND"
            end if'''


def move_email(identifier: str, to_folder: str, to_account: str = None) -> dict:
    """move an email to a specific folder, optionally across accounts."""
    try:
        identifier = validate_id(identifier, "email_id")
    except ValueError as e:
        return {"success": False, "message": str(e)}

    find_block = build_find_by_int_block(identifier)
    dest_block = _dest_account_block(to_account)
    folder_block = _folder_search_block(to_folder)

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set foundMessage to missing value
            set sourceAccount to missing value
{find_block}
            if foundMessage is missing value then
                return "EMAIL_NOT_FOUND"
            end if
            {dest_block}
            {folder_block}
            move foundMessage to targetMailbox
            return "SUCCESS"
        end tell
        """
    )

    try:
        result = run_applescript(script)
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()
    match output:
        case "EMAIL_NOT_FOUND":
            return {"success": False, "message": f"email with id {identifier} not found"}
        case "DEST_ACCOUNT_NOT_FOUND":
            return {"success": False, "message": f"no account found for email '{to_account}'"}
        case "FOLDER_NOT_FOUND":
            dest = f" (searched {to_account})" if to_account else ""
            return {"success": False, "message": f"folder '{to_folder}' not found{dest}"}
        case "SUCCESS":
            sync_mail_state(delay_seconds=1.0)
            dest = f"{to_account}/{to_folder}" if to_account else to_folder
            return {"success": True, "message": f"email moved to {dest} successfully"}
        case _:
            return {"success": False, "message": f"unexpected output: {output}"}


def _build_batch_move_script(email_ids: list[str], folder: str, account: str = None) -> str:
    """build applescript to batch-move emails by integer ids to a folder."""
    ids_literal = ", ".join(email_ids)
    folder_escaped = escape_applescript(folder)

    if account:
        dest_escaped = escape_applescript(account)
        dest_block = f"""
            set destAccount to missing value
            set targetEmail to "{dest_escaped}"
            repeat with acc in accounts
                set accEmails to email addresses of acc
                repeat with addr in accEmails
                    if (contents of addr) is targetEmail then
                        set destAccount to acc
                        exit repeat
                    end if
                end repeat
                if destAccount is not missing value then exit repeat
            end repeat
            if destAccount is missing value then
                return "DEST_ACCOUNT_NOT_FOUND|||"
            end if"""
    else:
        dest_block = "set destAccount to first account"

    return textwrap.dedent(
        f"""
        tell application "Mail"
            set targetIds to {{{ids_literal}}}
            set movedCount to 0
            set notFoundIds to {{}}
            {dest_block}

            -- find destination folder via BFS
            set targetMailbox to missing value
            set mailboxQueue to mailboxes of destAccount
            repeat while (count of mailboxQueue) > 0
                set currentMbox to item 1 of mailboxQueue
                try
                    ignoring case
                        set folderMatch to ((name of currentMbox) is "{folder_escaped}")
                    end ignoring
                    if folderMatch then
                        set targetMailbox to currentMbox
                        exit repeat
                    end if
                    set subMailboxes to mailboxes of currentMbox
                    repeat with subMbox in subMailboxes
                        set end of mailboxQueue to subMbox
                    end repeat
                end try
                set mailboxQueue to rest of mailboxQueue
            end repeat
            if targetMailbox is missing value then
                return "FOLDER_NOT_FOUND|||"
            end if

            -- move each email
            repeat with tid in targetIds
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
                    move foundMessage to targetMailbox
                    set movedCount to movedCount + 1
                else
                    set end of notFoundIds to (tid as string)
                end if
            end repeat

            set oldDelimiters to AppleScript's text item delimiters
            set AppleScript's text item delimiters to ","
            set notFoundStr to notFoundIds as string
            set AppleScript's text item delimiters to oldDelimiters
            return (movedCount as string) & "|||" & notFoundStr
        end tell
        """
    )


def batch_move_emails(identifiers: list[str], to_folder: str, to_account: str = None) -> dict:
    """batch-move emails to a folder in a single applescript call."""
    try:
        validated = [validate_id(eid) for eid in identifiers]
    except ValueError as e:
        return {"success": False, "message": str(e)}
    if not validated:
        return {"success": False, "message": "no email ids provided", "moved": 0, "requested": 0, "not_found": []}

    script = _build_batch_move_script(validated, to_folder, account=to_account)
    count = len(validated)

    try:
        result = run_applescript(script)
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()
    parts = output.split("|||")
    first = parts[0].strip()

    if first == "DEST_ACCOUNT_NOT_FOUND":
        return {"success": False, "message": f"no account found for email '{to_account}'"}
    if first == "FOLDER_NOT_FOUND":
        return {"success": False, "message": f"folder '{to_folder}' not found"}

    moved_count = int(first) if first.isdigit() else 0
    not_found = [x.strip() for x in parts[1].split(",") if x.strip()] if len(parts) > 1 and parts[1].strip() else []

    if moved_count > 0:
        sync_mail_state(delay_seconds=1.0)

    dest = f"{to_account}/{to_folder}" if to_account else to_folder
    return {
        "success": moved_count > 0,
        "moved": moved_count,
        "requested": count,
        "not_found": not_found,
        "message": f"moved {moved_count}/{count} emails to {dest}" + (f", {len(not_found)} not found" if not_found else ""),
    }
