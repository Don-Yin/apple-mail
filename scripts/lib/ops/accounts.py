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
        folders = sorted(folders, key=lambda x: x["folder_name"].lower())
        # hide empty system folders by default
        folders = [f for f in folders if f.get("email_count", 0) > 0]
        return folders
    except JXAError as e:
        if "no account found" in str(e):
            return {"success": False, "message": f"no account found for email '{account_email}'"}
        return []
    except TimeoutError:
        return []


def list_recent_emails(most_recent_n_emails: int = 128, include_content: bool = False):
    """List recent emails from all account inboxes."""
    limit = most_recent_n_emails if most_recent_n_emails is not None else 200
    script = f"""
// enumerate the unified inbox's per-account children. this is language-proof
// (covers localized names like 收件箱, which the old `name == "inbox"` filter
// silently skipped) and is exactly the inbox set Mail.app's UI shows.
var inboxes = Mail.inbox.mailboxes();
var results = [];
var totalInbox = 0;
var truncated = false;
var limit = {limit};

for (var a = 0; a < inboxes.length; a++) {{
    var mbox = inboxes[a];
    var acct = mbox.account();
    var accEmails = acct.emailAddresses();
    var accEmail = accEmails.length > 0 ? accEmails[0] : acct.name();
    var folderName = mbox.name();
    var inboxCount = mbox.messages.id().length;
    totalInbox += inboxCount;
    var data = MailCore.batchFetch(mbox.messages, [
        "id", "subject", "sender", "dateReceived", "messageId"
    ], limit);
    var count = data.id.length;
    if (inboxCount > count) truncated = true;  // honest even when limit == 0
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
JSON.stringify({{emails: results, total_inbox: totalInbox, truncated: truncated}});
"""
    try:
        raw = run_jxa_with_core(script, timeout=60)
    except (JXAError, TimeoutError):
        return []

    results = raw.get("emails", []) if isinstance(raw, dict) else raw
    total_inbox = raw.get("total_inbox", len(results)) if isinstance(raw, dict) else len(results)
    truncated = raw.get("truncated", False) if isinstance(raw, dict) else False

    # normalize folder names for consistency
    if results:
        for r in results:
            fn = r.get("folder_name", "")
            if fn.upper() == "INBOX":
                r["folder_name"] = "Inbox"

    # classify email types
    if results:
        from ..classify import classify_email
        for r in results:
            r["email_type"] = classify_email(r.get("subject", ""), r.get("sender", ""))

    # deduplicate by rfc message_id (Exchange sync can create multiple int_ids for same email)
    pre_dedup_count = len(results)
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

    showing = len(results)
    deduped_removed = pre_dedup_count - showing

    meta = {"total_inbox": total_inbox, "showing": showing}
    if deduped_removed > 0:
        meta["duplicates_removed"] = deduped_removed

    # coverage invariant: assert every inbox message was actually fetched. compare
    # rows fetched (pre-dedup) against the true inbox total, in python, independent
    # of the JXA flag. an explicit small --limit is an expected note; a shortfall at
    # --limit 0 means we hit the 2000-per-mailbox batch ceiling and is a hard warning
    # (the response contract's warnings[] field), so coverage loss can never be silent.
    fetched = pre_dedup_count
    dropped = total_inbox - fetched
    if dropped > 0:
        if limit and limit > 0:
            meta["note"] = f"showing {showing} of {total_inbox} inbox emails (limit={limit}). use --limit 0 to fetch all."
        else:
            meta["coverage_warnings"] = [
                f"INCOMPLETE COVERAGE: fetched {fetched} of {total_inbox} inbox messages; "
                f"{dropped} dropped at the 2000-per-mailbox batch ceiling. results are NOT complete "
                f"-- narrow by folder/account or paginate."
            ]

    if include_content and results:
        enriched = enrich_with_content(results)
        if isinstance(enriched, dict):
            enriched.update(meta)
        return enriched

    return {"emails": results, **meta}
