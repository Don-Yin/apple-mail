"""Non-UI draft artifact creation.

This backend intentionally does not talk to Mail.app. It writes an RFC 5322
message plus a small JSON manifest to disk so production draft generation is
not coupled to Apple Mail's live compose/delete scripting state.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from pathlib import Path


DEFAULT_ARTIFACT_DIR = Path("~/Documents/apple-mail-draft-artifacts").expanduser()


def _slug(text: str, fallback: str = "draft") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-._")
    return (slug[:80] or fallback).lower()


def _artifact_dir(output_dir: str | None = None) -> Path:
    raw = output_dir or os.environ.get("APPLE_MAIL_DRAFT_ARTIFACT_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_ARTIFACT_DIR


def _message_id_domain(account_email: str) -> str:
    _, _, domain = account_email.partition("@")
    return domain if domain else "local.invalid"


def _add_attachments(msg: EmailMessage, attachments: list[str]) -> list[dict]:
    rows = []
    for item in attachments:
        path = Path(item).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"attachment not found: {item}")
        if not path.is_file():
            raise ValueError(f"attachment is not a file: {item}")

        ctype, _ = mimetypes.guess_type(str(path))
        if ctype:
            maintype, subtype = ctype.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        data = path.read_bytes()
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        rows.append({
            "path": str(path.resolve()),
            "filename": path.name,
            "content_type": f"{maintype}/{subtype}",
            "bytes": len(data),
        })
    return rows


def create_draft_artifact(
    account_email: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    output_dir: str | None = None,
) -> dict:
    """Create an RFC 5322 draft artifact without opening or scripting Mail.app."""
    cc = cc or []
    bcc = bcc or []
    attachments = attachments or []
    if not to:
        return {"success": False, "message": "at least one recipient is required in the 'to' list"}

    msg = EmailMessage(policy=SMTP)
    message_id = make_msgid(domain=_message_id_domain(account_email))
    created_at = datetime.now(timezone.utc)
    msg["From"] = account_email
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(created_at)
    msg["Message-ID"] = message_id
    msg["X-Apple-Mail-Skill-Backend"] = "rfc822-artifact"
    msg.set_content(body)

    try:
        attachment_rows = _add_attachments(msg, attachments)
    except (FileNotFoundError, ValueError) as exc:
        return {"success": False, "message": str(exc)}

    out_dir = _artifact_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}-{_slug(subject)}"
    eml_path = out_dir / f"{stem}.eml"
    manifest_path = out_dir / f"{stem}.json"

    eml_path.write_bytes(msg.as_bytes(policy=SMTP))
    manifest = {
        "backend": "rfc822-artifact",
        "created_at": created_at.isoformat(),
        "message_id": message_id,
        "account_email": account_email,
        "subject": subject,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "attachments": attachment_rows,
        "eml_path": str(eml_path),
        "mail_app_written": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "success": True,
        "message": "draft artifact created; Mail.app was not opened or modified",
        "backend": "rfc822-artifact",
        "eml_path": str(eml_path),
        "manifest_path": str(manifest_path),
        "message_id": message_id,
        "mail_app_written": False,
    }
