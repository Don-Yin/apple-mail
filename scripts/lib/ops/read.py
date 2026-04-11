"""Email retrieval using JXA direct ID lookup, with cache-on-read.

read_full_email splits into two phases so a slow msg.content() call
(large HTML, inline images, Exchange sync stall) never blocks the
metadata that is always fast:

  Phase 1 - metadata: subject, sender, dates, recipients, attachments (~0.5 s)
  Phase 2 - content:  JXA content() (10 s cap) -> search index -> disk .emlx
"""

from ..jxa import run_jxa_with_core, JXAError

_CONTENT_JXA_TIMEOUT = 10
_METADATA_JXA_TIMEOUT = 15


def read_full_email(identifier: str) -> dict:
    """Get full email content including all details and attachments."""
    try:
        eid_for_content = int(identifier.strip())
    except ValueError as e:
        return {"success": False, "message": f"invalid email id: {e}"}
    result = _fetch_metadata(eid_for_content)

    if result is None:
        return {"success": False, "message": f"email with id {identifier} not found"}

    eid = int(result["id"])
    content, source = _fetch_content_with_fallback(eid, result)
    result["content"] = content
    result["content_source"] = source

    if not content:
        result["content_note"] = (
            "Content could not be retrieved (possible causes: large HTML body, "
            "inline images, or Exchange sync delay). Try again later or view "
            "directly in Mail.app."
        )

    sender = result.get("sender", "")
    result["sender_name"] = sender.split("<")[0].strip() if "<" in sender else sender

    _cache_on_read(result)
    return result


def _build_metadata_jxa(find_expr: str) -> str:
    """Build JXA metadata extraction script given a find expression."""
    return f"""
var msg = {find_expr};
if (!msg) {{
    JSON.stringify({{found: false}});
}} else {{
    var toRecips = [];
    try {{
        var tos = msg.toRecipients();
        for (var i = 0; i < tos.length; i++) toRecips.push(tos[i].address());
    }} catch(e) {{}}

    var ccRecips = [];
    try {{
        var ccs = msg.ccRecipients();
        for (var i = 0; i < ccs.length; i++) ccRecips.push(ccs[i].address());
    }} catch(e) {{}}

    var bccRecips = [];
    try {{
        var bccs = msg.bccRecipients();
        for (var i = 0; i < bccs.length; i++) bccRecips.push(bccs[i].address());
    }} catch(e) {{}}

    var attachments = [];
    try {{
        var atts = msg.mailAttachments();
        for (var i = 0; i < atts.length; i++) {{
            var aName = "unknown", aSize = "0";
            try {{ aName = atts[i].name(); }} catch(e) {{}}
            try {{ aSize = String(atts[i].fileSize()); }} catch(e) {{}}
            attachments.push({{name: aName, size: aSize}});
        }}
    }} catch(e) {{}}

    var mboxObj = msg.mailbox();
    var accObj = mboxObj.account();
    var accEmail = "";
    try {{
        var addrs = accObj.emailAddresses();
        accEmail = addrs.length > 0 ? addrs[0] : accObj.name();
    }} catch(e) {{ accEmail = accObj.name(); }}

    JSON.stringify({{
        found: true,
        id: String(msg.id()),
        message_id: msg.messageId() || "",
        subject: msg.subject() || "",
        sender: msg.sender() || "",
        date_received: MailCore.formatDate(msg.dateReceived()),
        date_sent: MailCore.formatDate(msg.dateSent()),
        read_status: msg.readStatus(),
        flagged_status: msg.flaggedStatus(),
        account_email: accEmail,
        folder_name: mboxObj.name(),
        to_recipients: toRecips,
        cc_recipients: ccRecips,
        bcc_recipients: bccRecips,
        attachments: attachments
    }});
}}
"""


def _fetch_metadata(eid: int) -> dict | None:
    """Phase 1: fetch everything except content() by integer id."""
    script = f"var targetId = {eid};\n" + _build_metadata_jxa(
        "MailCore.findMessageAcrossAccounts(targetId)"
    )
    try:
        result = run_jxa_with_core(script, timeout=_METADATA_JXA_TIMEOUT)
    except (TimeoutError, JXAError):
        return None

    if not result or not result.get("found"):
        return None

    result.pop("found", None)
    return result


def _fetch_content_with_fallback(eid: int, metadata: dict) -> tuple[str, str]:
    """Phase 2: try JXA content(), then search index, then disk .emlx."""
    content = _try_jxa_content(eid)
    if content:
        return content, "jxa"

    content = _try_search_index(eid, metadata)
    if content:
        return content, "search_index"

    content = _try_disk_emlx(eid)
    if content:
        return content, "disk"

    return "", "unavailable"


def _try_jxa_content(eid: int) -> str:
    """Attempt to get content via JXA msg.content() with a short timeout."""
    script = f"""
var msg = MailCore.findMessageAcrossAccounts({eid});
if (msg) {{
    var content = "";
    try {{ content = msg.content() || ""; }} catch(e) {{}}
    JSON.stringify({{content: content}});
}} else {{
    JSON.stringify({{content: ""}});
}}"""
    try:
        result = run_jxa_with_core(script, timeout=_CONTENT_JXA_TIMEOUT)
        return (result or {}).get("content", "")
    except (TimeoutError, JXAError):
        return ""


def _try_search_index(eid: int, metadata: dict) -> str:
    """Look up content from the FTS5 search index (includes ID-shift healing)."""
    try:
        from ..search_index import SearchIndexManager

        with SearchIndexManager() as mgr:
            content_map = mgr.batch_content([eid], [metadata])
            return content_map.get(eid, "")
    except Exception:
        return ""


def _try_disk_emlx(eid: int) -> str:
    """Read content directly from the .emlx file on disk."""
    try:
        from ..search_index import SearchIndexManager

        with SearchIndexManager() as mgr:
            content_map = mgr.targeted_index({eid})
            return content_map.get(eid, "")
    except Exception:
        return ""


def _cache_on_read(result: dict):
    """Cache the fetched email content in the search index for future previews."""
    content = result.get("content", "")
    if not content:
        return

    try:
        from ..search_index import SearchIndexManager

        with SearchIndexManager() as mgr:
            message_id = int(result["id"])
            account = result.get("account_email", "")
            mailbox = result.get("folder_name", "")
            rfc_mid = result.get("message_id", "")
            mgr.cache_content(
                message_id=message_id,
                subject=result.get("subject", ""),
                sender=result.get("sender", ""),
                content=content,
                date_received=result.get("date_received", ""),
                account=account,
                mailbox=mailbox,
                rfc_message_id=rfc_mid or None,
            )
            if rfc_mid:
                mgr.upsert_hints([(rfc_mid, message_id, account, mailbox)])
    except Exception:
        pass
