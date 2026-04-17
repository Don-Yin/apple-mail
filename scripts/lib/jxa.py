"""JXA script execution and content enrichment for Apple Mail."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import ASSETS_DIR, SCRIPTS_DIR, strip_html
from .search_index.schema import PROGRESS_PATH

MAIL_CORE_JS = (Path(__file__).parent / "mail_core.js").read_text()

_PREVIEW_LEN = 16000
_JXA_BUDGET_SECONDS = 60
_MAIL_PY = SCRIPTS_DIR / "mail.py"
_MAIL_SH = SCRIPTS_DIR / "mail.sh"

_STALE_TIMEOUT = 60
_LOCK_TIMEOUT = 5
_MAX_QUEUE_SIZE = 1000


class JXAError(Exception):
    """Raised when a JXA script fails to execute."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def run_jxa(script: str, timeout: int = 120) -> str:
    """Execute a raw JXA script and return stdout."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"jxa script timed out after {timeout}s")

    if result.returncode != 0:
        raise JXAError(f"jxa error: {result.stderr.strip()}", result.stderr)

    return result.stdout.strip()


def run_jxa_with_core(script_body: str, timeout: int = 120) -> any:
    """Execute a JXA script with mail_core.js injected, returns parsed JSON."""
    full_script = f"{MAIL_CORE_JS}\n\n{script_body}"
    output = run_jxa(full_script, timeout)

    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        preview = output[:500] + "..." if len(output) > 500 else output
        raise JXAError(
            f"failed to parse jxa output as json: {e}\noutput: {preview}", output
        ) from e


def enrich_with_content(messages: list[dict]) -> dict:
    """Enrich message dicts with content previews from the search index.

    Pipeline:
    1. mgr.maybe_prune()           -- auto-prune if DB > 256 MB
    2. mgr.batch_content()         -- index lookup with ID-shift self-healing
    3. mgr.targeted_index()        -- find .emlx files on disk for misses
    4. _jxa_fetch_with_budget()    -- JXA content() fallback within 60 s budget
    5. _spawn_background_indexer() -- detached worker for remaining IDs

    Returns a wrapper dict with enriched emails, coverage stats, index age,
    and optional background_indexing status.
    """
    from .search_index import SearchIndexManager

    mgr = SearchIndexManager()

    try:
        if not messages:
            return _build_wrapper([], mgr)

        mgr.maybe_prune()

        msg_ids = [int(m["id"]) for m in messages]
        content_map = mgr.batch_content(msg_ids, messages)

        missing_ids = set(msg_ids) - set(content_map.keys())
        if missing_ids:
            disk_content = mgr.targeted_index(missing_ids)
            content_map.update(disk_content)

        still_missing = set(msg_ids) - set(content_map.keys())
        bg_spawned = False

        if still_missing:
            still_missing_msgs = [m for m in messages if int(m["id"]) in still_missing]
            jxa_content = _jxa_fetch_with_budget(still_missing_msgs, mgr)
            content_map.update(jxa_content)

            final_missing = set(msg_ids) - set(content_map.keys())
            if final_missing:
                final_missing_ids = [mid for mid in final_missing]
                bg_spawned = _spawn_background_indexer(final_missing_ids)

        enriched = []
        for msg in messages:
            mid = int(msg["id"])
            content = content_map.get(mid, "")
            preview = strip_html(content)[:_PREVIEW_LEN] if content else ""

            entry = {**msg}
            entry["preview"] = preview
            if content:
                entry["preview_source"] = "indexed"
                entry["preview_truncated"] = len(content) > _PREVIEW_LEN
                entry["preview_available"] = True
            elif bg_spawned and mid in (set(msg_ids) - set(content_map.keys())):
                entry["preview_source"] = "background_indexing"
                entry["preview_truncated"] = False
                entry["preview_available"] = False
            else:
                entry["preview_source"] = "not_indexed"
                entry["preview_truncated"] = False
                entry["preview_available"] = False
            enriched.append(entry)

        return _build_wrapper(enriched, mgr, bg_spawned)
    finally:
        mgr.close()


def _build_wrapper(
    enriched: list[dict],
    mgr,
    bg_spawned: bool = False,
) -> dict:
    """Build the standard enrichment wrapper dict."""
    total = len(enriched)
    covered = sum(1 for e in enriched if e.get("preview_available"))

    wrapper = {
        "emails": enriched,
        "preview_coverage": {
            "covered": covered,
            "total": total,
            "percentage": round(covered / total * 100, 1) if total else 100.0,
        },
        "index_age": mgr.get_index_age(),
        "note": (
            "Previews are first ~5000 chars only (not full content). "
            "Use read-email for complete content."
        ),
    }

    if bg_spawned:
        bg_status = _get_background_status()
        if bg_status:
            wrapper["background_indexing"] = bg_status

    return wrapper


def _jxa_fetch_with_budget(
    messages: list[dict], mgr, budget: float = _JXA_BUDGET_SECONDS
) -> dict[int, str]:
    """Fetch content via JXA for messages not in index, within time budget."""
    fetched: dict[int, str] = {}
    start = time.monotonic()

    for msg in messages:
        if time.monotonic() - start > budget:
            break

        msg_id = int(msg["id"])
        folder = msg.get("folder_name", "")
        account = msg.get("account_email", "")
        safe_folder = json.dumps(folder)
        safe_account = json.dumps(account)
        try:
            script = f"""
var msg = null;
if ({safe_account} && {safe_folder}) {{
    try {{
        var acc = MailCore.getAccountByEmail({safe_account});
        var mbox = MailCore.getMailbox(acc, {safe_folder});
        var msgs = mbox.messages.whose({{id: {msg_id}}})();
        if (msgs.length > 0) msg = msgs[0];
    }} catch(e) {{}}
}}
if (!msg) msg = MailCore.findMessageAcrossAccounts({msg_id});
if (msg) {{
    var content = "";
    try {{ content = msg.content() || ""; }} catch(e) {{}}
    JSON.stringify({{id: {msg_id}, content: content}});
}} else {{
    JSON.stringify({{id: {msg_id}, content: ""}});
}}"""
            result = run_jxa_with_core(script, timeout=15)
            if result and result.get("content"):
                content = result["content"][:1_000_000]
                mgr.cache_content(
                    message_id=msg_id,
                    subject=msg.get("subject", ""),
                    sender=msg.get("sender", ""),
                    content=content,
                    date_received=msg.get("date_received", ""),
                    account=msg.get("account_email", ""),
                    mailbox=msg.get("folder_name", ""),
                    rfc_message_id=msg.get("message_id") or None,
                )
                fetched[msg_id] = content
        except Exception:
            continue

    return fetched


def _spawn_background_indexer(msg_ids: list[int]) -> bool:
    """Spawn a detached background indexer for remaining IDs.

    Returns True if a worker was spawned or is already running.
    Merges new IDs into existing queue (lock-protected, bounded) if a worker is active.
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    if PROGRESS_PATH.exists():
        try:
            merged = _merge_ids_into_queue(msg_ids)
            if merged:
                return True
        except Exception:
            pass

    if len(msg_ids) > _MAX_QUEUE_SIZE:
        msg_ids = msg_ids[:_MAX_QUEUE_SIZE]

    id_args = [str(i) for i in msg_ids]
    log_dir = ASSETS_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logs = sorted(log_dir.glob("background-index-*.log"), key=lambda p: p.stat().st_mtime)
    for stale in logs[:-10]:
        stale.unlink(missing_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"background-index-{timestamp}.log"

    with open(log_file, "w") as log:
        subprocess.Popen(
            [sys.executable, str(_MAIL_PY), "_background-index", "--ids"] + id_args,
            start_new_session=True,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )

    return True


def _merge_ids_into_queue(new_ids: list[int]) -> bool:
    """Merge new IDs into an active worker's queue via atomic file update.

    The background worker holds an exclusive flock for its entire lifetime,
    so we do NOT attempt to acquire that lock here. Instead we rely on:
    - atomic read-modify-write of the progress JSON
    - the worker re-reading remaining_ids from the file before each iteration

    Returns True if merge succeeded (worker is alive and queue updated).
    Returns False if no active worker found.
    """
    try:
        if not PROGRESS_PATH.exists():
            return False

        progress = json.loads(PROGRESS_PATH.read_text())
        if progress.get("status") != "running":
            return False

        pid = progress.get("pid")
        if not pid or not _is_pid_alive(pid):
            return False

        existing = set(progress.get("remaining_ids", []))
        existing.update(new_ids)

        if len(existing) > _MAX_QUEUE_SIZE:
            existing = set(sorted(existing)[:_MAX_QUEUE_SIZE])

        progress["remaining_ids"] = sorted(existing)
        progress["total"] = len(progress.get("attempted_ids", [])) + len(existing)
        _atomic_write_json(PROGRESS_PATH, progress)
        return True
    except Exception:
        return False


def _get_background_status() -> dict | None:
    """Check if background indexer is running, return status dict or None."""
    if not PROGRESS_PATH.exists():
        return None

    try:
        progress = json.loads(PROGRESS_PATH.read_text())
        status = progress.get("status", "")
        if status == "running":
            pid = progress.get("pid")
            if pid and _is_pid_alive(pid):
                mail_sh = str(_MAIL_SH)
                return {
                    "status": "running",
                    "remaining": len(progress.get("remaining_ids", [])),
                    "check_command": f"{mail_sh} index-status",
                }
        return None
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _atomic_write_json(path: Path, data: dict):
    """Write JSON to file atomically (write temp -> fsync -> rename)."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
