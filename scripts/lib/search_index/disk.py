"""Direct disk reading of Apple Mail .emlx files for FTS5 indexing.

Requires Full Disk Access for Terminal.

Mail.app storage structure:
    ~/Library/Mail/V10/
    +-- [Account-UUID]/
    |   +-- [Mailbox].mbox/
    |       +-- .../.../Messages/
    |           +-- 12345.emlx
    |           +-- 12346.emlx
    +-- MailData/

.emlx format:
    53192                 <- byte count of MIME content
    From: sender@...      <- RFC 5322 headers + body
    Subject: Hello
    ...
    <?xml version="1.0"?> <- plist metadata footer
"""

import email
import email.message
import re
import warnings
from email.header import decode_header, make_header
from pathlib import Path
from typing import Iterator

MAIL_VERSION = "V10"
MAX_EMLX_SIZE = 25 * 1024 * 1024


def find_mail_directory() -> Path:
    """Locate ~/Library/Mail/V10/ and verify access."""
    mail_dir = Path.home() / "Library" / "Mail" / MAIL_VERSION
    if not mail_dir.exists():
        raise FileNotFoundError(f"mail directory not found: {mail_dir}")
    try:
        next(mail_dir.iterdir(), None)
    except PermissionError as e:
        raise PermissionError(
            f"cannot access {mail_dir} — grant Full Disk Access to Terminal in "
            "System Settings > Privacy & Security > Full Disk Access"
        ) from e
    return mail_dir


def scan_emlx_files(mail_dir: Path) -> Iterator[Path]:
    """Yield all non-partial .emlx file paths."""
    for p in mail_dir.rglob("*.emlx"):
        if ".partial.emlx" not in p.name:
            yield p


def parse_emlx(path: Path) -> dict | None:
    """Parse a single .emlx file. Returns dict with id/subject/sender/content/date_received
    or None on failure."""
    try:
        if path.stat().st_size > MAX_EMLX_SIZE:
            return None

        raw = path.read_bytes()
        nl = raw.find(b"\n")
        if nl == -1:
            return None
        try:
            byte_count = int(raw[:nl].strip())
        except ValueError:
            return None

        mime_bytes = raw[nl + 1 : nl + 1 + byte_count]
        msg = email.message_from_bytes(mime_bytes)

        subject = _decode_header(msg["Subject"])
        sender = _decode_header(msg["From"])

        date_received = ""
        if msg["Date"]:
            try:
                from email.utils import parsedate_to_datetime

                date_received = parsedate_to_datetime(msg["Date"]).isoformat()
            except (ValueError, TypeError):
                date_received = msg["Date"] or ""

        body = _extract_body(msg)
        msg_id = int(path.stem)

        return {
            "id": msg_id,
            "subject": subject,
            "sender": sender,
            "content": body,
            "date_received": date_received,
            "emlx_path": str(path),
        }
    except Exception:
        return None


def scan_all_emails(mail_dir: Path) -> Iterator[dict]:
    """Scan all .emlx files, yielding parsed email dicts with account/mailbox inferred from path."""
    for emlx_path in scan_emlx_files(mail_dir):
        parsed = parse_emlx(emlx_path)
        if not parsed:
            continue
        account, mailbox = infer_account_mailbox(emlx_path, mail_dir)
        parsed["account"] = account
        parsed["mailbox"] = mailbox
        yield parsed


def get_disk_inventory(mail_dir: Path) -> set[tuple[str, str, int]]:
    """Fast filesystem scan returning set of (account, mailbox, msg_id) without parsing content."""
    inventory = set()
    for p in scan_emlx_files(mail_dir):
        try:
            msg_id = int(p.stem)
            account, mailbox = infer_account_mailbox(p, mail_dir)
            inventory.add((account, mailbox, msg_id))
        except (ValueError, AttributeError):
            continue
    return inventory


def infer_account_mailbox(emlx_path: Path, mail_dir: Path) -> tuple[str, str]:
    """Extract account UUID and mailbox name from file path.

    This is the canonical implementation -- no duplication elsewhere.
    """
    try:
        parts = emlx_path.relative_to(mail_dir).parts
        account = parts[0] if parts else "Unknown"
        mailbox = "Unknown"
        if len(parts) > 1:
            mbox_part = parts[1]
            mailbox = mbox_part[:-5] if mbox_part.endswith(".mbox") else mbox_part
        return (account, mailbox)
    except ValueError:
        return ("Unknown", "Unknown")


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def _extract_body(msg: email.message.Message) -> str:
    """Extract plain text from email, falling back to stripped HTML."""
    if msg.is_multipart():
        text_parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_parts.append(payload.decode(charset, errors="replace"))
        if text_parts:
            return "\n".join(text_parts)

        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return _strip_html(payload.decode(charset, errors="replace"))
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if not payload:
            return ""
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        return _strip_html(text) if msg.get_content_type() == "text/html" else text


def _strip_html(html: str) -> str:
    """Convert HTML to plain text via BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return re.sub(r" +", " ", text).strip()
    except Exception:
        return ""
