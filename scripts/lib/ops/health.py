"""Health check for Apple Mail connectivity."""

from ..applescript import run_applescript


def health_check() -> dict:
    """Returns health check status; success=True if Mail.app is responding."""
    applescript = """
    tell application "Mail"
        return "OK"
    end tell
    """
    try:
        result = run_applescript(applescript, timeout=5)
        if "OK" in result.stdout:
            return {"message": "apple mail is responding correctly"}
        else:
            return {"success": False, "message": "apple mail not responding properly"}
    except (TimeoutError, RuntimeError) as e:
        return {"success": False, "message": f"apple mail not responding properly: {e}"}
