"""message-id resolution: sqlite hint -> scoped whose -> full scan."""

import json
from .jxa import run_jxa_with_core, JXAError
from .search_index.manager import SearchIndexManager


def resolve_message(rfc_message_id: str) -> dict | None:
    """resolve an rfc message-id to a live message via cached location hint."""
    with SearchIndexManager() as mgr:
        hint = mgr.get_hint(rfc_message_id)

    hint_account = json.dumps(hint["account"]) if hint else "null"
    hint_mailbox = json.dumps(hint["mailbox"]) if hint else "null"
    safe_mid = json.dumps(rfc_message_id)

    script = f"""
var r = MailCore.resolveByMessageId({safe_mid}, {hint_account}, {hint_mailbox});
if (r) {{
    JSON.stringify({{
        id: String(r.msg.id()),
        message_id: r.msg.messageId(),
        subject: r.msg.subject(),
        account_email: r.account,
        mailbox: r.mailbox
    }});
}} else {{
    JSON.stringify(null);
}}
"""
    timeout = 30 if hint else 15
    try:
        result = run_jxa_with_core(script, timeout=timeout)
    except (JXAError, TimeoutError):
        return None

    if not result:
        return None

    with SearchIndexManager() as mgr:
        mgr.upsert_hints([(rfc_message_id, int(result["id"]), result["account_email"], result["mailbox"])])

    return result


def upsert_listing_hints(results: list[dict]):
    """populate resolver hints from list command results."""
    hints = [
        (r["message_id"], int(r["id"]), r["account_email"], r["folder_name"])
        for r in results if r.get("message_id")
    ]
    if not hints:
        return
    with SearchIndexManager() as mgr:
        mgr.upsert_hints(hints)
