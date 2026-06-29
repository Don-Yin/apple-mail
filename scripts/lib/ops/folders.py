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

    # classify email types
    if results:
        from ..classify import classify_email
        for r in results:
            r["email_type"] = classify_email(r.get("subject", ""), r.get("sender", ""))

    # deduplicate by rfc message_id (exchange sync can create multiple int_ids)
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
    deduped_removed = pre_dedup_count - len(results)

    if results:
        from ..resolve import upsert_listing_hints
        upsert_listing_hints(results)

    # coverage invariant (mirror of list-recent): never truncate silently. an
    # explicit small --limit is an expected note; a shortfall at --limit 0 means we
    # hit the 2000-per-mailbox batch ceiling and is a hard warnings[] entry.
    fetched = pre_dedup_count
    dropped = total - fetched
    note = None
    coverage_warnings = []
    if dropped > 0:
        if limit and limit > 0:
            note = f"showing {len(results)} of {total} emails (limit={limit}). use --limit 0 to fetch all."
        else:
            coverage_warnings = [
                f"INCOMPLETE COVERAGE: fetched {fetched} of {total} messages in '{folder_name}'; "
                f"{dropped} dropped at the 2000-per-mailbox batch ceiling."
            ]

    if include_content and results:
        enriched = enrich_with_content(results)
        if isinstance(enriched, dict):
            enriched["total"] = total
            enriched["showing"] = len(results)
            if note:
                enriched["note"] = note
            if coverage_warnings:
                enriched["coverage_warnings"] = coverage_warnings
        return enriched

    output = {"emails": results, "total": total, "showing": len(results)}
    if deduped_removed > 0:
        output["duplicates_removed"] = deduped_removed
    if note:
        output["note"] = note
    if coverage_warnings:
        output["coverage_warnings"] = coverage_warnings
    return output
