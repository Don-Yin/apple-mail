"""Email move operation."""

import textwrap
from ..applescript import validate_id, escape_applescript, run_applescript, sync_mail_state, build_find_by_int_block


def move_email(identifier: str, to_folder: str, to_account: str = None) -> dict:
    """move an email to a specific folder, optionally across accounts."""
    try:
        identifier = validate_id(identifier, "email_id")
    except ValueError as e:
        return {"success": False, "message": str(e)}

    folder_escaped = escape_applescript(to_folder)
    find_block = build_find_by_int_block(identifier)

    if to_account:
        dest_escaped = escape_applescript(to_account)
        dest_account_block = f"""
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
                return "DEST_ACCOUNT_NOT_FOUND"
            end if"""
    else:
        dest_account_block = "set destAccount to sourceAccount"

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

            {dest_account_block}

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
                return "FOLDER_NOT_FOUND"
            end if

            move foundMessage to targetMailbox
            return "SUCCESS"
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
