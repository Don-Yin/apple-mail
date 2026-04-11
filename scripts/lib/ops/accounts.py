"""Account-level operations using batch JXA."""

import json
from ..jxa import run_jxa_with_core, JXAError, enrich_with_content


def list_accounts():
    """Get all logged in mail accounts (~0.15 s)."""
    try:
        return run_jxa_with_core("JSON.stringify(MailCore.listAccounts());")
    except (JXAError, TimeoutError):
        return []


def list_account_folders(account_email: str):
    """Get folder details for a specific account."""
    safe_email = json.dumps(account_email)
    script = f"""
var acct = MailCore.getAccountByEmail({safe_email});
var accName = acct.name();
var data = MailCore.listMailboxesWithCounts(acct);
for (var i = 0; i < data.length; i++) {{
    data[i].folder_path = accName + "/" + data[i].folder_name;
}}
JSON.stringify(data);
"""
    try:
        folders = run_jxa_with_core(script)
        return sorted(folders, key=lambda x: x["folder_name"].lower())
    except JXAError as e:
        if "no account found" in str(e):
            return {"success": False, "message": f"no account found for email '{account_email}'"}
        return []
    except TimeoutError:
        return []


def list_recent_emails(most_recent_n_emails: int = 20, include_content: bool = False):
    """List recent emails from all account inboxes."""
    limit = most_recent_n_emails or 200
    script = f"""
var accounts = Mail.accounts();
var accNames = Mail.accounts.name();
var accEmails = Mail.accounts.emailAddresses();
var results = [];
var limit = {limit};

for (var a = 0; a < accounts.length; a++) {{
    var acct = accounts[a];
    var accEmail = accEmails[a].length > 0 ? accEmails[a][0] : accNames[a];
    var mboxNames = acct.mailboxes.name();
    var mboxes = acct.mailboxes();

    for (var m = 0; m < mboxNames.length; m++) {{
        if (mboxNames[m].toLowerCase() !== "inbox") continue;
        var mbox = mboxes[m];
        var folderName = mboxNames[m];
        var data = MailCore.batchFetch(mbox.messages, [
            "id", "subject", "sender", "dateReceived", "messageId"
        ], limit);
        var count = data.id.length;
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
        break;
    }}
}}
JSON.stringify(results);
"""
    try:
        results = run_jxa_with_core(script, timeout=60)
    except (JXAError, TimeoutError):
        return []

    # deduplicate by rfc message_id (Exchange sync can create multiple int_ids for same email)
    if results:
        seen = set()
        deduped = []
        for r in results:
            mid = r.get("message_id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            deduped.append(r)
        results = deduped

    if results:
        from ..resolve import upsert_listing_hints
        upsert_listing_hints(results)

    if include_content and results:
        return enrich_with_content(results)
    return results
