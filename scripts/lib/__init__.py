from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS_DIR = SKILL_ROOT / "assets"
SCRIPTS_DIR = SKILL_ROOT / "scripts"


def strip_html(text: str) -> str:
    """extract visible text from html, stripping tags, urls, and tracking noise."""
    import re
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'https?://\S{80,}', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def relative_time(iso_str: str) -> str:
    """Convert ISO datetime string to human-readable relative time."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now() - dt
        seconds = delta.total_seconds()
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            mins = int(seconds / 60)
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        if seconds < 86400:
            hours = int(seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    except (ValueError, TypeError):
        return "unknown"
