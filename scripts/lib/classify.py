"""email type classification from subject and sender patterns."""

import re

_AUTO_REPLY_PATTERNS = re.compile(
    r"^(automatic reply|out of office|auto:|autoreply:|undeliverable|"
    r"delivery status notification|returned mail|mail delivery)",
    re.IGNORECASE,
)

_NOTIFICATION_PATTERNS = re.compile(
    r"^\[.*\]|^re: \[.*\]",
    re.IGNORECASE,
)


def classify_email(subject: str, sender: str = "") -> str:
    """classify email type from subject and sender patterns."""
    if _AUTO_REPLY_PATTERNS.search(subject):
        return "auto-reply"
    sender_lower = sender.lower()
    if "noreply@" in sender_lower or "no-reply@" in sender_lower:
        return "notification"
    if "notifications@github.com" in sender_lower:
        return "notification"
    if _NOTIFICATION_PATTERNS.match(subject):
        return "notification"
    return "human"
