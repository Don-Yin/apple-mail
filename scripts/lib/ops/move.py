"""Email move operation."""

import textwrap
from ..applescript import validate_id, escape_applescript, run_applescript, sync_mail_state


def _build_find_by_mid_block(message_id: str) -> str:
    """Build AppleScript block to find email by RFC 2822 message-id, inbox first."""
    escaped_mid = escape_applescript(message_id)
    return f"""
            -- inbox first (fast path)
            set msgList to (messages of inbox whose message id is "{escaped_mid}")
            if (count of msgList) > 0 then
                set foundMessage to item 1 of msgList
                repeat with acc in accounts
                    if (inbox of acc) is (mailbox of foundMessage) then
                        set sourceAccount to acc
                        exit repeat
                    end if
                end repeat
            end if

            if foundMessage is missing value then
                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        set msgList to (messages of mbox whose message id is "{escaped_mid}")
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            set sourceAccount to acc
                            exit repeat
                        end if
                    end repeat
                    if foundMessage is not missing value then exit repeat
                end repeat
            end if
"""


def _build_find_by_int_block(email_id: str) -> str:
    """Build AppleScript block to find email by integer id."""
    return f"""
            set targetId to {email_id} as integer
            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    set msgList to (messages of mbox whose id is targetId)
                    if (count of msgList) > 0 then
                        set foundMessage to item 1 of msgList
                        set sourceAccount to acc
                        exit repeat
                    end if
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat
"""


def move_email(identifier: str, to_folder: str, by_message_id: bool = False, delay_seconds: int = 3) -> dict:
    """Move an email to a specific folder, searching recursively across accounts."""
    if not by_message_id:
        try:
            identifier = validate_id(identifier, "email_id")
        except ValueError as e:
            return {"success": False, "message": str(e)}

    folder_escaped = escape_applescript(to_folder)

    if by_message_id:
        find_block = _build_find_by_mid_block(identifier)
    else:
        find_block = _build_find_by_int_block(identifier)

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set foundMessage to missing value
            set sourceAccount to missing value
            set targetMailbox to missing value
{find_block}
            if foundMessage is missing value then
                return "EMAIL_NOT_FOUND"
            end if

            set targetMailbox to missing value
            set mailboxQueue to mailboxes of sourceAccount

            repeat while (count of mailboxQueue) > 0
                set currentMbox to item 1 of mailboxQueue

                ignoring case
                    set folderMatch to ((name of currentMbox) is "{folder_escaped}")
                end ignoring
                if folderMatch then
                    set targetMailbox to currentMbox
                    exit repeat
                end if

                try
                    set subMailboxes to mailboxes of currentMbox
                    repeat with subMbox in subMailboxes
                        set end of mailboxQueue to subMbox
                    end repeat
                end try

                set mailboxQueue to rest of mailboxQueue
            end repeat

            if targetMailbox is missing value then
                return "FOLDER_NOT_FOUND"
            end if

            set sourceMbox to mailbox of foundMessage
            set movedId to id of foundMessage
            move foundMessage to targetMailbox
            delay {delay_seconds}

            set stillInSource to (messages of sourceMbox whose id is movedId)
            if (count of stillInSource) is 0 then
                return "SUCCESS"
            else
                return "MOVE_FAILED"
            end if
        end tell
        """
    )

    try:
        result = run_applescript(script)
    except TimeoutError as e:
        return {"success": False, "message": str(e)}
    except RuntimeError as e:
        return {"success": False, "message": str(e)}

    output = result.stdout.strip()

    match output:
        case "EMAIL_NOT_FOUND":
            return {"success": False, "message": f"email with id {identifier} not found"}
        case "FOLDER_NOT_FOUND":
            return {"success": False, "message": f"folder '{to_folder}' not found. note: custom folders must be synced with exchange server"}
        case "MOVE_FAILED":
            return {"success": False, "message": f"move command executed but email did not appear in '{to_folder}'. folder may not be synced with exchange server"}
        case "SUCCESS":
            sync_mail_state(delay_seconds=1.0)
            return {"success": True, "message": f"email moved to {to_folder} successfully"}
        case _:
            return {"success": False, "message": f"unexpected output: {output}"}
