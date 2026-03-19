"""Forward draft creation."""

import textwrap
from ..applescript import (
    validate_id,
    escape_applescript,
    validate_attachments,
    build_recipients,
    run_applescript,
    build_attachments,
    sync_mail_state,
)


def _build_find_block(identifier: str, by_message_id: bool) -> str:
    """Build AppleScript block to find an email for forwarding."""
    if by_message_id:
        escaped_mid = escape_applescript(identifier)
        return f"""
            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    set msgList to (messages of mbox whose message id is "{escaped_mid}")
                    if (count of msgList) > 0 then
                        set foundEmail to item 1 of msgList
                        exit repeat
                    end if
                end repeat
                if foundEmail is not missing value then exit repeat
            end repeat
"""
    return f"""
            set targetId to {identifier} as integer

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isInbox to (name of mbox is "inbox")
                    end ignoring
                    if isInbox then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundEmail to item 1 of msgList
                            exit repeat
                        end if
                    end if
                end repeat
                if foundEmail is not missing value then exit repeat
            end repeat

            if foundEmail is missing value then
                repeat with acc in accounts
                    repeat with mbox in mailboxes of acc
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundEmail to item 1 of msgList
                            exit repeat
                        end if
                    end repeat
                    if foundEmail is not missing value then exit repeat
                end repeat
            end if
"""


def make_forward_draft(
    email_id: str,
    account: str,
    body: str,
    to: list[str],
    cc: list[str] = None,
    bcc: list[str] = None,
    new_attachments: list[str] = None,
    by_message_id: bool = False,
) -> dict:
    """Create a forward draft from an existing email."""
    if not by_message_id:
        try:
            email_id = validate_id(email_id, "email_id")
        except ValueError as e:
            return {"success": False, "message": str(e)}

    cc = cc or []
    bcc = bcc or []
    new_attachments = new_attachments or []

    if not to:
        return {"success": False, "message": "at least one recipient is required in the 'to' list"}

    attachment_paths, error_msg = validate_attachments(new_attachments)
    if error_msg:
        return {"success": False, "message": error_msg}

    body_escaped = escape_applescript(body)
    account_escaped = escape_applescript(account)

    to_section = build_recipients(to, "to", "newMessage")
    cc_section = build_recipients(cc, "cc", "newMessage")
    bcc_section = build_recipients(bcc, "bcc", "newMessage")
    attachment_section = build_attachments(attachment_paths, "newMessage")

    find_block = _build_find_block(email_id, by_message_id)

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set foundEmail to missing value
{find_block}
            if foundEmail is missing value then
                return "EMAIL_NOT_FOUND"
            end if

            set sendAccount to missing value
            set allAccounts to every account
            repeat with i from 1 to count of allAccounts
                set acc to item i of allAccounts
                set addrs to email addresses of acc
                repeat with j from 1 to count of addrs
                    if item j of addrs is "{account_escaped}" then
                        set sendAccount to acc
                        exit repeat
                    end if
                end repeat
                if sendAccount is missing value then
                    ignoring case
                        if (user name of acc is "{account_escaped}") or (name of acc is "{account_escaped}") then
                            set sendAccount to acc
                        end if
                    end ignoring
                end if
                if sendAccount is not missing value then exit repeat
            end repeat

            if sendAccount is missing value then
                return "ACCOUNT_NOT_FOUND"
            end if

            set originalSubject to subject of foundEmail
            set originalContent to content of foundEmail
            set forwardSubject to "Fwd: " & originalSubject
            set forwardContent to "{body_escaped}" & return & return & "------- Forwarded Message -------" & return & return & originalContent

            set newMessage to make new outgoing message with properties {{sender:"{account_escaped}", subject:forwardSubject, content:forwardContent, visible:false}}
            {to_section}{cc_section}{bcc_section}
            set tmpFolder to (path to temporary items from user domain) as text
            repeat with attach in mail attachments of foundEmail
                try
                    set attachName to name of attach
                    save attach in file (tmpFolder & attachName)
                    set savedPath to POSIX path of (tmpFolder & attachName)
                    tell newMessage to make new attachment with properties {{file name:savedPath}}
                end try
            end repeat
            {attachment_section}
            save newMessage

            delay 0.3
            try
                repeat with w in windows
                    try
                        if name of w contains forwardSubject then
                            close w
                        end if
                    end try
                end repeat
            end try

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

    if output == "EMAIL_NOT_FOUND":
        return {"success": False, "message": f"original email with id {email_id} not found"}
    if output == "ACCOUNT_NOT_FOUND":
        return {"success": False, "message": f"account {account} not found"}
    if output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "forward draft created successfully - query drafts after a delay to get stable id"}
    return {"success": False, "message": f"unexpected output: {output}"}
