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
from .mutation_guard import require_live_mail_mutation, run_guarded_local_mail_mutation


# ------------------------------------------------------------------
# Compose
# ------------------------------------------------------------------


def _build_recipient_commit_guard(recipients: list[str], recipient_type: str, target: str = "newMessage") -> str:
    if not recipients:
        return ""

    var_name = f"{recipient_type}Addresses"
    lines = [
        f"set {var_name} to {{}}",
        f"repeat with recip in {recipient_type} recipients of {target}",
        "    try",
        f"        set end of {var_name} to address of recip",
        "    end try",
        "end repeat",
    ]
    for addr in recipients:
        escaped = escape_applescript(addr)
        lines.extend([
            f'if {var_name} does not contain "{escaped}" then',
            f'    tell {target} to make new {recipient_type} recipient with properties {{address:"{escaped}"}}',
            "end if",
        ])
    return "\n            ".join(lines) + "\n            "


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
    recipient_commit_guard = (
        _build_recipient_commit_guard(to, "to", "newMessage")
        + _build_recipient_commit_guard(cc, "cc", "newMessage")
        + _build_recipient_commit_guard(bcc, "bcc", "newMessage")
    )
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
                    set newMessage to make new outgoing message with properties {{sender:"{account_escaped}", subject:"{subject_escaped}", content:"{body_escaped}", visible:false}}
                    {to_section}{cc_section}{bcc_section}{attachment_section}
                    delay 0.25
                    {recipient_commit_guard}
                    delay 0.1
                    save newMessage
                    delay 0.2
                    return "SUCCESS"
                end if
            end repeat
            return "ACCOUNT_NOT_FOUND"
        end tell
        """
    )

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except TimeoutError as e:
            return {"success": False, "message": str(e)}
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "ACCOUNT_NOT_FOUND":
            return {"success": False, "message": f"account {account_email} not found"}
        elif output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            return {
                "success": True,
                "message": "hidden Mail draft created successfully - query drafts after a delay to get stable id",
                "backend": "mailapp",
                "mail_app_written": True,
                "visible": False,
            }
        else:
            return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("compose-draft", action)


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
    guard = require_live_mail_mutation("amend-draft")
    if guard:
        return guard

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
            "repeat with recipAddr in draftCcAddresses\n"
            "                tell amendedDraft to make new cc recipient with properties {address:recipAddr}\n"
            "            end repeat\n            "
        )

    bcc_section = ""
    if new_bcc is not None:
        bcc_section = build_recipients(new_bcc, "bcc", "amendedDraft") if new_bcc else ""
    else:
        bcc_section = (
            "repeat with recipAddr in draftBccAddresses\n"
            "                tell amendedDraft to make new bcc recipient with properties {address:recipAddr}\n"
            "            end repeat\n            "
        )

    attachment_section = build_attachments(attachment_paths, "amendedDraft")

    script = textwrap.dedent(
        f"""
        using terms from application "Mail"
            on _mailSkillAddresses(recipientObjects)
                set addressList to {{}}
                repeat with recip in recipientObjects
                    try
                        set end of addressList to address of recip as text
                    end try
                end repeat
                return addressList
            end _mailSkillAddresses

            on _mailSkillMatchesDraft(candidate, finalSubject, finalContent, cleanupLowerBound)
                try
                    if subject of candidate is not finalSubject then return false
                on error
                    return false
                end try

                set candidateDate to missing value
                try
                    set candidateDate to date received of candidate
                end try
                if candidateDate is missing value then
                    try
                        set candidateDate to date sent of candidate
                    end try
                end if
                if candidateDate is not missing value then
                    if candidateDate is less than cleanupLowerBound then return false
                end if

                try
                    if content of candidate is not finalContent then return false
                on error
                    return false
                end try

                return true
            end _mailSkillMatchesDraft
        end using terms from

        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundDraft to missing value
            set sourceAccount to missing value

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundDraft to item 1 of msgList
                            set sourceAccount to acc
                            exit repeat
                        end if
                    end if
                end repeat
                if foundDraft is not missing value then exit repeat
            end repeat

            if foundDraft is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            set senderEmail to ""
            try
                set accountAddresses to email addresses of sourceAccount
                if (count of accountAddresses) > 0 then set senderEmail to item 1 of accountAddresses
            end try
            if senderEmail is "" then
                try
                    set senderEmail to user name of sourceAccount
                end try
            end if
            if senderEmail is "" then set senderEmail to sender of foundDraft

            set draftSubject to subject of foundDraft
            set draftContent to content of foundDraft
            set finalSubject to draftSubject
            set finalContent to draftContent
            {subject_assignment}
            {body_assignment}

            set draftToAddresses to {{}}
            repeat with recip in to recipients of foundDraft
                try
                    set end of draftToAddresses to address of recip
                end try
            end repeat
            set draftCcAddresses to {{}}
            repeat with recip in cc recipients of foundDraft
                try
                    set end of draftCcAddresses to address of recip
                end try
            end repeat
            set draftBccAddresses to {{}}
            repeat with recip in bcc recipients of foundDraft
                try
                    set end of draftBccAddresses to address of recip
                end try
            end repeat

            set amendedDraft to make new outgoing message with properties {{sender:senderEmail, subject:finalSubject, content:finalContent, visible:false}}
            repeat with recipAddr in draftToAddresses
                tell amendedDraft to make new to recipient with properties {{address:recipAddr}}
            end repeat
            {cc_section}{bcc_section}
            set amendedToAddresses to my _mailSkillAddresses(to recipients of amendedDraft)
            set amendedCcAddresses to my _mailSkillAddresses(cc recipients of amendedDraft)
            set amendedBccAddresses to my _mailSkillAddresses(bcc recipients of amendedDraft)
            delay 0.25
            set tmpFolder to (path to temporary items from user domain) as text
            repeat with attach in mail attachments of foundDraft
                try
                    set attachName to name of attach
                    save attach in file (tmpFolder & attachName)
                    set savedPath to POSIX path of (tmpFolder & attachName)
                    tell content of amendedDraft
                        make new attachment with properties {{file name:POSIX file savedPath}} at after the last paragraph
                    end tell
                end try
            end repeat
            {attachment_section}
            delay 0.1
            set cleanupLowerBound to (current date) - 60
            save amendedDraft
            delay 0.5
            delete foundDraft

            set remainingMatches to 0
            repeat with cleanupAttempt from 1 to 12
                delay 0.75
                set matchedDrafts to {{}}
                repeat with mbox in mailboxes of sourceAccount
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        repeat with candidate in messages of mbox
                            if my _mailSkillMatchesDraft(candidate, finalSubject, finalContent, cleanupLowerBound) then
                                set end of matchedDrafts to candidate
                            end if
                        end repeat
                    end if
                end repeat

                if (count of matchedDrafts) > 1 then
                    set preferredId to -1
                    repeat with candidate in matchedDrafts
                        try
                            if my _mailSkillAddresses(to recipients of candidate) is amendedToAddresses and my _mailSkillAddresses(cc recipients of candidate) is amendedCcAddresses and my _mailSkillAddresses(bcc recipients of candidate) is amendedBccAddresses then
                                set preferredId to id of candidate
                                exit repeat
                            end if
                        end try
                    end repeat
                    if preferredId is -1 then
                        try
                            set preferredId to id of item 1 of matchedDrafts
                        end try
                    end if
                    repeat with candidate in matchedDrafts
                        set candidateId to -2
                        try
                            set candidateId to id of candidate
                        end try
                        if candidateId is not preferredId then
                            try
                                delete candidate
                            end try
                        end if
                    end repeat
                end if

                delay 0.15
                set remainingMatches to 0
                repeat with mbox in mailboxes of sourceAccount
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        repeat with candidate in messages of mbox
                            if my _mailSkillMatchesDraft(candidate, finalSubject, finalContent, cleanupLowerBound) then
                                set remainingMatches to remainingMatches + 1
                            end if
                        end repeat
                    end if
                end repeat
                if remainingMatches is less than or equal to 1 then exit repeat
            end repeat

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

            if remainingMatches is greater than 1 then return "DRAFT_DUPLICATES"
            return "SUCCESS"
        end tell
        """
    )

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except TimeoutError as e:
            return {"success": False, "message": str(e)}
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "DRAFT_NOT_FOUND":
            return {"success": False, "message": f"draft with id {draft_id} not found"}
        elif output == "DRAFT_DUPLICATES":
            return {"success": False, "message": f"draft with id {draft_id} was amended but Mail still reports duplicate amended drafts"}
        elif output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            return {"success": True, "message": "draft amended successfully - query drafts after a delay to get stable id"}
        else:
            return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("amend-draft", action)


# ------------------------------------------------------------------
# Send
# ------------------------------------------------------------------


def send_draft(draft_id: str) -> dict:
    """Send a draft email by its ID."""
    guard = require_live_mail_mutation("send-draft")
    if guard:
        return guard

    try:
        draft_id = validate_id(draft_id)
    except ValueError as e:
        return {"success": False, "message": str(e)}

    script = textwrap.dedent(
        f"""
        using terms from application "Mail"
            on _mailSkillAddresses(recipientObjects)
                set addressList to {{}}
                repeat with recip in recipientObjects
                    try
                        set end of addressList to address of recip as text
                    end try
                end repeat
                return addressList
            end _mailSkillAddresses

            on _mailSkillResidualDraft(candidate, targetId, originalMessageId, draftSubject, draftContent, draftToAddresses, draftCcAddresses, draftBccAddresses, cleanupLowerBound)
                try
                    if id of candidate is targetId then return true
                end try
                try
                    if originalMessageId is not "" and message id of candidate is originalMessageId then return true
                end try
                try
                    if subject of candidate is not draftSubject then return false
                on error
                    return false
                end try

                set candidateDate to missing value
                try
                    set candidateDate to date received of candidate
                end try
                if candidateDate is missing value then
                    try
                        set candidateDate to date sent of candidate
                    end try
                end if
                if candidateDate is missing value then return false
                if candidateDate is less than cleanupLowerBound then return false
                return true
            end _mailSkillResidualDraft
        end using terms from

        tell application "Mail"
            set targetId to {draft_id} as integer
            set foundDraft to missing value
            set sourceAccount to missing value

            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundDraft to item 1 of msgList
                            set sourceAccount to acc
                            exit repeat
                        end if
                    end if
                end repeat
                if foundDraft is not missing value then exit repeat
            end repeat

            if foundDraft is missing value then
                return "DRAFT_NOT_FOUND"
            end if

            set draftSubject to subject of foundDraft
            set originalMessageId to ""
            try
                set originalMessageId to message id of foundDraft
            end try
            set senderEmail to ""
            try
                set accountAddresses to email addresses of sourceAccount
                if (count of accountAddresses) > 0 then set senderEmail to item 1 of accountAddresses
            end try
            if senderEmail is "" then
                try
                    set senderEmail to user name of sourceAccount
                end try
            end if
            if senderEmail is "" then set senderEmail to sender of foundDraft

            set draftContent to content of foundDraft
            set draftToAddresses to {{}}
            repeat with recip in to recipients of foundDraft
                try
                    set end of draftToAddresses to address of recip
                end try
            end repeat
            set draftCcAddresses to {{}}
            repeat with recip in cc recipients of foundDraft
                try
                    set end of draftCcAddresses to address of recip
                end try
            end repeat
            set draftBccAddresses to {{}}
            repeat with recip in bcc recipients of foundDraft
                try
                    set end of draftBccAddresses to address of recip
                end try
            end repeat

            set newOutgoing to make new outgoing message with properties {{sender:senderEmail, subject:draftSubject, content:draftContent, visible:false}}
            repeat with recipAddr in draftToAddresses
                tell newOutgoing to make new to recipient with properties {{address:recipAddr}}
            end repeat
            repeat with recipAddr in draftCcAddresses
                tell newOutgoing to make new cc recipient with properties {{address:recipAddr}}
            end repeat
            repeat with recipAddr in draftBccAddresses
                tell newOutgoing to make new bcc recipient with properties {{address:recipAddr}}
            end repeat

            set tmpFolder to (path to temporary items from user domain) as text
            repeat with attach in mail attachments of foundDraft
                try
                    set attachName to name of attach
                    save attach in file (tmpFolder & attachName)
                    set savedPath to POSIX path of (tmpFolder & attachName)
                    tell content of newOutgoing
                        make new attachment with properties {{file name:POSIX file savedPath}} at after the last paragraph
                    end tell
                end try
            end repeat

            set cleanupLowerBound to (current date) - 5
            send newOutgoing

            try
                delete foundDraft
            end try

            set remainingDrafts to 0
            set stableZeroCount to 0
            repeat with cleanupAttempt from 1 to 45
                delay 0.75
                repeat with mbox in mailboxes of sourceAccount
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        set residuals to {{}}
                        repeat with candidate in messages of mbox
                            if my _mailSkillResidualDraft(candidate, targetId, originalMessageId, draftSubject, draftContent, draftToAddresses, draftCcAddresses, draftBccAddresses, cleanupLowerBound) then
                                set end of residuals to candidate
                            end if
                        end repeat
                        repeat with residual in residuals
                            try
                                delete residual
                            end try
                        end repeat
                    end if
                end repeat

                delay 0.15
                set remainingDrafts to 0
                repeat with mbox in mailboxes of sourceAccount
                    ignoring case
                        set isDraft to (name of mbox contains "draft")
                    end ignoring
                    if isDraft then
                        repeat with candidate in messages of mbox
                            if my _mailSkillResidualDraft(candidate, targetId, originalMessageId, draftSubject, draftContent, draftToAddresses, draftCcAddresses, draftBccAddresses, cleanupLowerBound) then
                                set remainingDrafts to remainingDrafts + 1
                            end if
                        end repeat
                    end if
                end repeat
                if remainingDrafts is 0 then
                    set stableZeroCount to stableZeroCount + 1
                else
                    set stableZeroCount to 0
                end if
                if stableZeroCount is greater than or equal to 24 then exit repeat
            end repeat

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

            if remainingDrafts is not 0 or stableZeroCount is less than 24 then return "DRAFT_RESIDUALS"
            return "SUCCESS"
        end tell
        """
    )

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except TimeoutError as e:
            return {"success": False, "message": str(e)}
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "DRAFT_NOT_FOUND":
            return {"success": False, "message": f"draft with id {draft_id} not found"}
        elif output == "DRAFT_RESIDUALS":
            return {"success": False, "message": f"draft with id {draft_id} was sent but Mail still reports a matching draft residual"}
        elif output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            return {"success": True, "message": "draft sent successfully"}
        else:
            return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("send-draft", action)


# ------------------------------------------------------------------
# Reply
# ------------------------------------------------------------------


def _build_find_email_block(email_id: str) -> str:
    """Applescript block to find an email by integer id, inbox-first."""
    return f"""
            set targetId to {email_id} as integer

            -- phase 1: unified inbox (locale-proof: covers Inbox/INBOX/收件箱)
            set msgList to (messages of inbox whose id is targetId)
            if (count of msgList) > 0 then
                set foundEmail to item 1 of msgList
                set foundAccount to account of (mailbox of foundEmail)
            end if

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


def _applescript_text_expr(text: str) -> str:
    """Build an AppleScript expression that preserves paragraph breaks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return " & return & ".join(f'"{escape_applescript(line)}"' for line in normalized.split("\n"))


def reply_draft(
    original_email_id: str,
    body: str,
    reply_all: bool = False,
    extra_cc: list[str] = None,
    extra_bcc: list[str] = None,
    extra_attachments: list[str] = None,
) -> dict:
    """Draft a reply to an original email and leave it as a draft."""
    guard = require_live_mail_mutation("reply-draft")
    if guard:
        return guard

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

    body_expr = _applescript_text_expr(body)
    cc_section = build_recipients(extra_cc, "cc", "newMessage")
    bcc_section = build_recipients(extra_bcc, "bcc", "newMessage")
    attachment_section = build_attachments(attachment_paths, "newMessage")
    find_block = _build_find_email_block(original_email_id)

    reply_all_section = ""
    if reply_all:
        reply_all_section = """
                repeat with recip in originalToRecips
                    set recipAddr to address of recip
                    if recipAddr is not accountEmail and recipAddr is not originalSenderEmail then
                        tell newMessage to make new cc recipient with properties {address:recipAddr}
                    end if
                end repeat
                repeat with recip in originalCcRecips
                    set recipAddr to address of recip
                    if recipAddr is not accountEmail then
                        tell newMessage to make new cc recipient with properties {address:recipAddr}
                    end if
                end repeat
            """

    script = textwrap.dedent(
        f"""
        tell application "Mail"
            set foundEmail to missing value
            set foundAccount to missing value
{find_block}
            if foundEmail is missing value then
                return "EMAIL_NOT_FOUND"
            end if

            set originalSenderRaw to sender of foundEmail
            set originalSubject to subject of foundEmail
            set originalToRecips to to recipients of foundEmail
            set originalCcRecips to cc recipients of foundEmail
            set accountEmail to ""
            set accEmails to email addresses of foundAccount
            if (count of accEmails) > 0 then
                set accountEmail to (item 1 of accEmails) as string
            else
                set accountEmail to user name of foundAccount
            end if

            set originalSenderEmail to originalSenderRaw
            try
                if reply to of foundEmail is not "" then set originalSenderEmail to reply to of foundEmail
            end try
            if originalSenderEmail contains "<" then
                set savedTID to AppleScript's text item delimiters
                set AppleScript's text item delimiters to "<"
                set angleParts to text items of originalSenderEmail
                set AppleScript's text item delimiters to ">"
                set originalSenderEmail to item 1 of (text items of (item 2 of angleParts))
                set AppleScript's text item delimiters to savedTID
            end if

            set replySubject to originalSubject
            if replySubject starts with "FW: " then set replySubject to text 5 thru -1 of replySubject
            if replySubject starts with "Fwd: " then set replySubject to text 6 thru -1 of replySubject
            if replySubject does not start with "Re: " then set replySubject to "Re: " & replySubject

            set replyBody to {body_expr}
            set newMessage to reply foundEmail opening window no
            delay 2
            set visible of newMessage to false
            set subject of newMessage to replySubject

            set originalText to content of foundEmail
            set quotedHistory to "------- Original Message -------" & return & "From: " & originalSenderRaw & return & "Subject: " & originalSubject & return & return & originalText
            set content of newMessage to replyBody & return & return & quotedHistory

            {reply_all_section}{cc_section}{bcc_section}{attachment_section}
            save newMessage
            return "SUCCESS"
        end tell
        """
    )

    def action() -> dict:
        try:
            result = run_applescript(script, preserve_focus=True)
        except TimeoutError as e:
            return {"success": False, "message": str(e)}
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

        output = result.stdout.strip()

        if output == "EMAIL_NOT_FOUND":
            return {"success": False, "message": f"original email with id {original_email_id} not found"}
        if output == "SUCCESS":
            sync_mail_state(preserve_focus=True)
            return {"success": True, "message": "reply draft created successfully - query drafts after a delay to get stable id"}
        return {"success": False, "message": f"unexpected output: {output}"}

    return run_guarded_local_mail_mutation("reply-draft", action)


# ------------------------------------------------------------------
# List
# ------------------------------------------------------------------


def list_drafts(limit: int = 128, include_content: bool = False) -> dict:
    """List existing draft emails across all mail accounts."""
    effective_limit = limit if limit is not None else 200

    script = f"""
var accounts = Mail.accounts();
var accNames = Mail.accounts.name();
var accEmails = Mail.accounts.emailAddresses();
var results = [];
var totalCount = 0;
var limit = {effective_limit};

for (var a = 0; a < accounts.length; a++) {{
    var acct = accounts[a];
    var accEmail = accEmails[a].length > 0 ? accEmails[a][0] : accNames[a];
    var mboxNames = acct.mailboxes.name();
    var mboxes = acct.mailboxes();

    for (var m = 0; m < mboxNames.length; m++) {{
        if (mboxNames[m].toLowerCase().indexOf("draft") === -1) continue;
        var mbox = mboxes[m];
        var folderName = mboxNames[m];
        totalCount += mbox.messages.id().length;
        var data = MailCore.batchFetch(mbox.messages, [
            "id", "subject", "sender", "dateReceived", "messageId"
        ], limit);
        var count = limit > 0 ? Math.min(data.id.length, limit - results.length) : data.id.length;
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
JSON.stringify({{drafts: results, total: totalCount}});
"""
    try:
        raw = run_jxa_with_core(script, timeout=30)
    except (JXAError, TimeoutError):
        return {"drafts": [], "total": 0, "showing": 0}

    results = raw.get("drafts", []) if isinstance(raw, dict) else raw
    total = raw.get("total", len(results)) if isinstance(raw, dict) else len(results)

    # coverage invariant (mirror of list-recent/list-emails): never truncate silently.
    # an explicit small --limit is an expected note; a shortfall at --limit 0 means the
    # 2000-per-mailbox ceiling was hit and is a hard coverage_warnings[] entry.
    fetched = len(results)
    note = None
    coverage_warnings = []
    if fetched < total:
        if limit and limit > 0:
            note = f"showing {fetched} of {total} drafts (limit={limit}). use --limit 0 to fetch all."
        else:
            coverage_warnings = [
                f"INCOMPLETE COVERAGE: fetched {fetched} of {total} drafts; "
                f"{total - fetched} dropped at the 2000-per-mailbox batch ceiling."
            ]

    if include_content and results:
        enriched = enrich_with_content(results)
        if isinstance(enriched, dict):
            enriched["total"] = total
            enriched["showing"] = fetched
            if note:
                enriched["note"] = note
            if coverage_warnings:
                enriched["coverage_warnings"] = coverage_warnings
        return enriched

    output = {"drafts": results, "total": total, "showing": fetched}
    if note:
        output["note"] = note
    if coverage_warnings:
        output["coverage_warnings"] = coverage_warnings
    return output
