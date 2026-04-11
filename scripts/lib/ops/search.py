"""Email search using FTS5 index and JXA."""

import json
from ..search_index import SearchIndexManager
from ..jxa import run_jxa_with_core, JXAError


def search_emails(query: str, scope: str = "all", account_email: str = None, limit: int = 20) -> list[dict]:
    """Search emails by content, subject, or sender.

    scope="all" uses FTS5 index (~1 ms), "subject"/"sender" use JXA (~200 ms).
    """
    if scope == "all":
        return _search_fts(query, account_email, limit)
    elif scope == "subject":
        return _search_jxa_field(query, "subject", account_email, limit)
    elif scope == "sender":
        return _search_jxa_field(query, "sender", account_email, limit)
    else:
        return [{"error": f"unknown scope: {scope}. use 'all', 'subject', or 'sender'"}]


def build_search_index() -> dict:
    """Build or rebuild the FTS5 search index from email files on disk.

    Requires Full Disk Access for Terminal.
    """
    mgr = SearchIndexManager()
    try:
        result = mgr.build_from_disk()
        stats = mgr.get_stats()
        return {
            "success": True,
            **result,
            "db_size_mb": stats["db_size_mb"],
        }
    except PermissionError as e:
        return {"success": False, "error": str(e)}
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"index build failed: {e}"}
    finally:
        mgr.close()


def _search_fts(query: str, account_email: str | None, limit: int) -> list[dict]:
    mgr = SearchIndexManager()
    if not mgr.has_index():
        mgr.close()
        return [{"error": "no search index found. run build-index to create one."}]

    try:
        results = mgr.search(query, account=account_email, limit=limit)
    finally:
        mgr.close()

    return results


def _search_jxa_field(query: str, field: str, account_email: str | None, limit: int) -> list[dict]:
    """Search inbox by subject or sender using JXA whose-clause filtering."""
    safe_query = json.dumps(query.lower())

    if account_email:
        safe_email = json.dumps(account_email)
        acct_line = f"var acct = MailCore.getAccountByEmail({safe_email});\nvar accounts = [acct];"
    else:
        acct_line = "var accounts = Mail.accounts();"

    script = f"""
{acct_line}
var accEmails = Mail.accounts.emailAddresses();
var accNames = Mail.accounts.name();
var results = [];
var limit = {limit};
var needle = {safe_query};

for (var a = 0; a < accounts.length && results.length < limit; a++) {{
    var acct = accounts[a];
    var accEmail = "";
    try {{
        var addrs = acct.emailAddresses();
        accEmail = addrs.length > 0 ? addrs[0] : acct.name();
    }} catch(e) {{ accEmail = acct.name(); }}
    var mboxNames = acct.mailboxes.name();
    var mboxes = acct.mailboxes();

    for (var m = 0; m < mboxNames.length && results.length < limit; m++) {{
        if (mboxNames[m].toLowerCase() !== "inbox") continue;
        var mbox = mboxes[m];
        var folderName = mboxNames[m];
        var data = MailCore.batchFetch(mbox.messages, ["id", "subject", "sender", "dateReceived", "messageId"]);
        for (var i = 0; i < data.id.length && results.length < limit; i++) {{
            var val = (data.{field}[i] || "").toLowerCase();
            if (val.indexOf(needle) !== -1) {{
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
}}
JSON.stringify(results);
"""
    try:
        return run_jxa_with_core(script, timeout=30)
    except (JXAError, TimeoutError):
        return []
