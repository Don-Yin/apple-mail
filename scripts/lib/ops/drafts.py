"""Draft operations: compose, amend, send, reply, list."""

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
from ..jxa import run_jxa_with_core, JXAError, enrich_with_content


# ------------------------------------------------------------------
# Compose
# ------------------------------------------------------------------


def compose_draft(
    account_email: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] = None,
    bcc: list[str] = None,
    attachments: list[str] = None,
) -> dict:
    """Create a draft email and save it to the draft folder."""
    cc = cc or []
    bcc = bcc or []
    attachments = attachments or []

    if not to:
        return {"success": False, "message": "at least one recipient is required in the 'to' list"}

    attachment_paths, error_msg = validate_attachments(attachments)
    if error_msg:
        return {"success": False, "message": error_msg}

    subject_escaped = escape_applescript(subject)
    body_escaped = escape_applescript(body)
    account_escaped = escape_applescript(account_email)

    to_section = build_recipients(to, "to", "newMessage")
    cc_section = build_recipients(cc, "cc", "newMessage")
    bcc_section = build_recipients(bcc, "bcc", "newMessage")
    attachment_section = build_attachments(attachment_paths, "newMessage")

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set allAccounts to every account
            repeat with i from 1 to count of allAccounts
                set acc to item i of allAccounts
                set matchFound to false
                set addrs to email addresses of acc
                repeat with j from 1 to count of addrs
                    if item j of addrs is "{account_escaped}" then
                        set matchFound to true
                        exit repeat
                    end if
                end repeat
                if not matchFound then
                    ignoring case
                        if (user name of acc is "{account_escaped}") or (name of acc is "{account_escaped}") then
                            set matchFound to true
                        end if
                    end ignoring
                end if
                if matchFound then
                    set newMessage to make new outgoing message with properties {{sender:"{account_escaped}", subject:"{subject_escaped}", content:"{body_escaped}"}}
                    {to_section}{cc_section}{bcc_section}{attachment_section}
                    save newMessage
                    return "SUCCESS"
                end if
            end repeat
            return "ACCOUNT_NOT_FOUND"
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

    if output == "ACCOUNT_NOT_FOUND":
        return {"success": False, "message": f"account {account_email} not found"}
    elif output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "draft created successfully - query drafts after a delay to get stable id"}
    else:
        return {"success": False, "message": f"unexpected output: {output}"}


# ------------------------------------------------------------------
# Amend
# ------------------------------------------------------------------


def amend_draft(
    draft_id: str,
    new_subject: str = None,
    new_body: str = None,
    new_cc: list[str] = None,
    new_bcc: list[str] = None,
    new_attachments: list[str] = None,
) -> dict:
    """Amend a draft; only the provided fields will be amended."""
    try:
        draft_id = validate_id(draft_id, "draft_id")
    except ValueError as e:
        return {"success": False, "message": str(e)}

    new_attachments = new_attachments or []
    attachment_paths, error_msg = validate_attachments(new_attachments)
    if error_msg:
        return {"success": False, "message": error_msg}

    subject_assignment = f'set finalSubject to "{escape_applescript(new_subject)}"' if new_subject else ""
    body_assignment = f'set finalContent to "{escape_applescript(new_body)}"' if new_body else ""

    cc_section = ""
    if new_cc is not None:
        cc_section = build_recipients(new_cc, "cc", "amendedDraft") if new_cc else ""
    else:
        cc_section = (
            "repeat with recip in draftCcRecips\n"
            "                make new cc recipient at amendedDraft with properties {address:address of recip}\n"
            "            end repeat\n            "
        )

    bcc_section = ""
    if new_bcc is not None:
        bcc_section = build_recipients(new_bcc, "bcc", "amendedDraft") if new_bcc else ""
    else:
        bcc_section = (
            "repeat with recip in draftBccRecips\n"
            "                make new bcc recipient at amendedDraft with properties {address:address of recip}\n"
            "            end repeat\n            "
        )

    attachment_section = build_attachments(attachment_paths, "amendedDraft")

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundDraft to missing value

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundDraft to item 1 of msgList
                            exit repeat
                        end if
                    end if
                end repeat
                if foundDraft is not missing value then exit repeat
            end repeat

            if foundDraft is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            set draftSender to sender of foundDraft
            set draftSubject to subject of foundDraft
            set draftContent to content of foundDraft
            set draftToRecips to to recipients of foundDraft
            set draftCcRecips to cc recipients of foundDraft
            set draftBccRecips to bcc recipients of foundDraft

            set finalSubject to draftSubject
            set finalContent to draftContent
            {subject_assignment}
            {body_assignment}

            set amendedDraft to make new outgoing message with properties {{sender:draftSender, subject:finalSubject, content:finalContent, visible:false}}

            repeat with recip in draftToRecips
                make new to recipient at amendedDraft with properties {{address:address of recip}}
            end repeat

            {cc_section}{bcc_section}
            set tmpFolder to (path to temporary items from user domain) as text
            repeat with attach in mail attachments of foundDraft
                try
                    set attachName to name of attach
                    save attach in file (tmpFolder & attachName)
                    set savedPath to POSIX path of (tmpFolder & attachName)
                    make new attachment at amendedDraft with properties {{file name:savedPath}}
                end try
            end repeat
            {attachment_section}
            delete foundDraft
            save amendedDraft

            delay 0.3
            try
                repeat with w in windows
                    try
                        if name of w contains finalSubject then
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

    if output == "DRAFT_NOT_FOUND":
        return {"success": False, "message": f"draft with id {draft_id} not found"}
    elif output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "draft amended successfully"}
    else:
        return {"success": False, "message": f"unexpected output: {output}"}


# ------------------------------------------------------------------
# Send
# ------------------------------------------------------------------


def send_draft(draft_id: str) -> dict:
    """Send a draft email by its ID."""
    try:
        draft_id = validate_id(draft_id)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundDraft to missing value

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundDraft to item 1 of msgList
                            exit repeat
                        end if
                    end if
                end repeat
                if foundDraft is not missing value then exit repeat
            end repeat

            if foundDraft is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            set draftSender to sender of foundDraft
            set draftSubject to subject of foundDraft
            set draftContent to content of foundDraft
            set draftToRecips to to recipients of foundDraft
            set draftCcRecips to cc recipients of foundDraft
            set draftBccRecips to bcc recipients of foundDraft

            set newOutgoing to make new outgoing message with properties {{sender:draftSender, subject:draftSubject, content:draftContent, visible:false}}

            repeat with recip in draftToRecips
                make new to recipient at newOutgoing with properties {{address:address of recip}}
            end repeat

            repeat with recip in draftCcRecips
                make new cc recipient at newOutgoing with properties {{address:address of recip}}
            end repeat

            repeat with recip in draftBccRecips
                make new bcc recipient at newOutgoing with properties {{address:address of recip}}
            end repeat

            set tmpFolder to (path to temporary items from user domain) as text
            repeat with attach in mail attachments of foundDraft
                try
                    set attachName to name of attach
                    save attach in file (tmpFolder & attachName)
                    set savedPath to POSIX path of (tmpFolder & attachName)
                    make new attachment at newOutgoing with properties {{file name:savedPath}}
                end try
            end repeat

            send newOutgoing

            try
                delete foundDraft
            end try

            delay 0.5
            try
                repeat with w in windows
                    try
                        if name of w contains draftSubject then
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

    if output == "DRAFT_NOT_FOUND":
        return {"success": False, "message": f"draft with id {draft_id} not found"}
    elif output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "draft sent successfully"}
    else:
        return {"success": False, "message": f"unexpected output: {output}"}


# ------------------------------------------------------------------
# Reply
# ------------------------------------------------------------------


def _build_find_email_block(email_id: str) -> str:
    """Applescript block to find an email by integer id, inbox-first."""
    return f"""
            set targetId to {email_id} as integer

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isInbox to (name of mbox is "inbox")
                    end ignoring
                    if isInbox then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundEmail to item 1 of msgList
                            set foundAccount to acc
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
                            set foundAccount to acc
                            exit repeat
                        end if
                    end repeat
                    if foundEmail is not missing value then exit repeat
                end repeat
            end if
"""


def reply_draft(
    original_email_id: str,
    body: str,
    reply_all: bool = False,
    extra_cc: list[str] = None,
    extra_bcc: list[str] = None,
    extra_attachments: list[str] = None,
) -> dict:
    """Draft a reply to an original email and leave it as a draft."""
    try:
        original_email_id = validate_id(original_email_id, "email_id")
    except ValueError as e:
        return {"success": False, "message": str(e)}

    extra_cc = extra_cc or []
    extra_bcc = extra_bcc or []
    extra_attachments = extra_attachments or []

    attachment_paths, error_msg = validate_attachments(extra_attachments)
    if error_msg:
        return {"success": False, "message": error_msg}

    body_escaped = escape_applescript(body)
    cc_section = build_recipients(extra_cc, "cc", "newMessage")
    bcc_section = build_recipients(extra_bcc, "bcc", "newMessage")
    attachment_section = build_attachments(attachment_paths, "newMessage")

    reply_all_section = ""
    if reply_all:
        reply_all_section = """
            repeat with recip in originalToRecips
                set recipAddr to address of recip
                if recipAddr is not accountEmail then
                    tell newMessage to make new to recipient with properties {address:recipAddr}
                end if
            end repeat
            repeat with recip in originalCcRecips
                set recipAddr to address of recip
                if recipAddr is not accountEmail then
                    tell newMessage to make new cc recipient with properties {address:recipAddr}
                end if
            end repeat
            """

    find_block = _build_find_email_block(original_email_id)

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set foundEmail to missing value
            set foundAccount to missing value
{find_block}
            if foundEmail is missing value then
                return "EMAIL_NOT_FOUND"
            end if

            set originalSender to sender of foundEmail
            set originalSubject to subject of foundEmail
            set originalContent to content of foundEmail
            set originalToRecips to to recipients of foundEmail
            set originalCcRecips to cc recipients of foundEmail
            set accountEmail to ""
            set accEmails to email addresses of foundAccount
            if (count of accEmails) > 0 then
                set accountEmail to (item 1 of accEmails) as string
            else
                set accountEmail to user name of foundAccount
            end if

            set replySubject to originalSubject
            if replySubject does not start with "Re: " then
                set replySubject to "Re: " & originalSubject
            end if

            set replyContent to "{body_escaped}" & return & return & "------- Original Message -------" & return & return & originalContent

            set newMessage to make new outgoing message with properties {{sender:accountEmail, subject:replySubject, content:replyContent}}

            tell newMessage to make new to recipient with properties {{address:originalSender}}

            {reply_all_section}{cc_section}{bcc_section}{attachment_section}
            save newMessage
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
        return {"success": False, "message": f"original email with id {original_email_id} not found"}
    if output == "SUCCESS":
        sync_mail_state()
        return {"success": True, "message": "reply draft created successfully - query drafts after a delay to get stable id"}
    return {"success": False, "message": f"unexpected output: {output}"}


# ------------------------------------------------------------------
# List
# ------------------------------------------------------------------


def list_drafts(limit: int = 50, include_content: bool = False) -> list[dict] | dict:
    """List existing draft emails across all mail accounts."""
    effective_limit = limit if limit else 999999

    script = f"""
var accounts = Mail.accounts();
var accNames = Mail.accounts.name();
var accEmails = Mail.accounts.emailAddresses();
var results = [];
var limit = {effective_limit};

for (var a = 0; a < accounts.length && results.length < limit; a++) {{
    var acct = accounts[a];
    var accEmail = accEmails[a].length > 0 ? accEmails[a][0] : accNames[a];
    var mboxNames = acct.mailboxes.name();
    var mboxes = acct.mailboxes();

    for (var m = 0; m < mboxNames.length && results.length < limit; m++) {{
        if (mboxNames[m].toLowerCase().indexOf("draft") === -1) continue;
        var mbox = mboxes[m];
        var folderName = mboxNames[m];
        var data = MailCore.batchFetch(mbox.messages, [
            "id", "subject", "sender", "dateReceived", "messageId"
        ]);
        var count = Math.min(data.id.length, limit - results.length);
        for (var i = 0; i < count; i++) {{
            results.push({{
                id: String(data.id[i]),
                message_id: data.messageId[i] || "",
                subject: data.subject[i] || "",
                sender: data.sender[i] || "",
                date_received: MailCore.formatDate(data.dateReceived[i]) || "",
                account_email: accEmail,
                folder_name: folderName
            }});
        }}
    }}
}}
JSON.stringify(results);
"""
    try:
        results = run_jxa_with_core(script, timeout=30)
    except (JXAError, TimeoutError):
        return []

    if include_content and results:
        return enrich_with_content(results)
    return results
