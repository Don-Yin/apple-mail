"""Folder-level email listing using batch JXA."""

import json
from ..jxa import run_jxa_with_core, JXAError, enrich_with_content


def list_emails_in_folder(
    account_email: str,
    folder_name: str,
    limit: int = 12,
    include_content: bool = False,
) -> list[dict] | dict:
    """List emails in a specific folder from a specific account."""
    safe_email = json.dumps(account_email)
    safe_folder = json.dumps(folder_name)
    effective_limit = limit if limit is not None else 200

    script = f"""
var acct = MailCore.getAccountByEmail({safe_email});
var mbox = MailCore.getMailbox(acct, {safe_folder});
var folderName = mbox.name();
var totalCount = mbox.messages.id().length;
var data = MailCore.batchFetch(mbox.messages, [
    "id", "subject", "sender", "dateReceived", "messageId"
], {effective_limit});
var results = [];
var count = data.id.length;
for (var i = 0; i < count; i++) {{
    results.push({{
        id: String(data.id[i]),
        message_id: data.messageId[i] || "",
        subject: data.subject[i] || "",
        sender: data.sender[i] || "",
        date_received: MailCore.formatDate(data.dateReceived[i]) || "",
        account_email: {safe_email},
        folder_name: folderName
    }});
}}
JSON.stringify({{emails: results, total: totalCount}});
"""
    try:
        raw = run_jxa_with_core(script, timeout=60)
    except JXAError as e:
        if "no account found" in str(e):
            return {"success": False, "message": f"no account found for email '{account_email}'"}
        if "mailbox not found" in str(e):
            return {"success": False, "message": f"folder '{folder_name}' not found in account '{account_email}'"}
        return {"emails": [], "total": 0, "showing": 0}
    except TimeoutError:
        return {"emails": [], "total": 0, "showing": 0}

    results = raw.get("emails", []) if isinstance(raw, dict) else raw
    total = raw.get("total", len(results)) if isinstance(raw, dict) else len(results)

    if results:
        from ..resolve import upsert_listing_hints
        upsert_listing_hints(results)

    if include_content and results:
        enriched = enrich_with_content(results)
        if isinstance(enriched, dict):
            enriched["total"] = total
            enriched["showing"] = len(results)
        return enriched

    output = {"emails": results, "total": total, "showing": len(results)}
    if len(results) < total:
        output["note"] = f"showing {len(results)} of {total} emails. use --limit 0 to fetch all."
    return output
