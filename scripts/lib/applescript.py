"""AppleScript execution, validation, and escaping utilities for Apple Mail."""

import re
import subprocess
import time
from pathlib import Path


def validate_id(value: str, label: str = "id") -> str:
    """Validate that an id is numeric to prevent AppleScript injection."""
    if not re.match(r"^\d+$", value.strip()):
        raise ValueError(f"invalid {label}: {value!r} — must be numeric")
    return value.strip()


def escape_applescript(text: str) -> str:
    """Escape a string for safe interpolation into AppleScript."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return text


def run_applescript(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Execute an AppleScript via osascript with timeout and error checking."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"applescript timed out after {timeout}s")

    if result.returncode != 0:
        raise RuntimeError(f"applescript error: {result.stderr.strip()}")

    return result


def validate_attachments(attachments: list[str]) -> tuple[list[str], str]:
    """Validate attachment file paths, returns (valid_paths, error_msg)."""
    if not attachments:
        return [], ""

    valid_paths = []
    for path in attachments:
        p = Path(path).expanduser()
        if not p.exists():
            return [], f"attachment not found: {path}"
        if not p.is_file():
            return [], f"attachment is not a file: {path}"
        valid_paths.append(str(p.absolute()))

    return valid_paths, ""


def build_recipients(recipients: list[str], recipient_type: str, target: str = "newMessage") -> str:
    """Build AppleScript recipient section."""
    if not recipients:
        return ""

    lines = [
        f'tell {target} to make new {recipient_type} recipient with properties {{address:"{escape_applescript(addr)}"}}'
        for addr in recipients
    ]
    return "\n            ".join(lines) + "\n            "


def build_attachments(attachment_paths: list[str], target: str = "newMessage") -> str:
    """Build AppleScript attachment section."""
    if not attachment_paths:
        return ""

    lines = [
        f'tell {target} to make new attachment with properties {{file name:"{escape_applescript(path)}"}}'
        for path in attachment_paths
    ]
    return "\n            ".join(lines) + "\n            "


def sync_mail_state(delay_seconds: float = 0.3):
    """Synchronize with Mail.app to ensure operations complete and IDs are stable."""
    script = """
        tell application "Mail"
            synchronize
        end tell
    """
    try:
        run_applescript(script, timeout=10)
    except (TimeoutError, RuntimeError):
        pass
    time.sleep(delay_seconds)


def build_find_by_int_block(email_id: str) -> str:
    """applescript block to find email by integer id across all accounts."""
    return f"""
            set targetId to {email_id} as integer
            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    try
                        set msgList to (messages of mbox whose id is targetId)
                        if (count of msgList) > 0 then
                            set foundMessage to item 1 of msgList
                            set sourceAccount to acc
                            exit repeat
                        end if
                    end try
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat
"""


def build_find_in_drafts_block(draft_id: str) -> str:
    """applescript block to find a draft by integer id in draft folders."""
    return f"""
            set targetId to {draft_id} as integer
            repeat with acc in accounts
                repeat with mbox in mailboxes of acc
                    if name of mbox contains "raft" then
                        try
                            set msgList to (messages of mbox whose id is targetId)
                            if (count of msgList) > 0 then
                                set foundMessage to item 1 of msgList
                                set sourceAccount to acc
                                exit repeat
                            end if
                        end try
                    end if
                end repeat
                if foundMessage is not missing value then exit repeat
            end repeat
"""


def build_account_find_block(email: str, var_name: str = "targetAccount") -> str:
    """applescript block to find account by email address."""
    escaped = escape_applescript(email)
    return f"""
            set {var_name} to missing value
            set targetEmail to "{escaped}"
            repeat with acc in accounts
                set accEmails to email addresses of acc
                repeat with addr in accEmails
                    if (contents of addr) is targetEmail then
                        set {var_name} to acc
                        exit repeat
                    end if
                end repeat
                if {var_name} is not missing value then exit repeat
            end repeat
"""
