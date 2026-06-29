"""JXA script execution and content enrichment for Apple Mail."""

import json
import os
import selectors
import subprocess
import time
from pathlib import Path

from . import strip_html

MAIL_CORE_JS = (Path(__file__).parent / "mail_core.js").read_text()

_PREVIEW_LEN = 16000
_MAX_JXA_OUTPUT_BYTES = 12 * 1024 * 1024
_MAX_JXA_STDERR_BYTES = 1 * 1024 * 1024


class JXAError(Exception):
    """Raised when a JXA script fails to execute."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def run_jxa(script: str, timeout: int = 120) -> str:
    """execute a raw jxa script and return bounded stdout."""
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_size = 0
    stderr_size = 0
    deadline = time.monotonic() + timeout

    process = subprocess.Popen(
        ["osascript", "-l", "JavaScript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise TimeoutError(f"jxa script timed out after {timeout}s")

            for key, _ in selector.select(timeout=min(0.2, remaining)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue

                if key.data == "stdout":
                    stdout_size += len(chunk)
                    if stdout_size > _MAX_JXA_OUTPUT_BYTES:
                        process.kill()
                        process.wait()
                        raise JXAError(
                            f"jxa output exceeded {_MAX_JXA_OUTPUT_BYTES // 1024 // 1024} MB"
                        )
                    stdout_chunks.append(chunk)
                else:
                    stderr_size += len(chunk)
                    if stderr_size > _MAX_JXA_STDERR_BYTES:
                        process.kill()
                        process.wait()
                        raise JXAError(
                            f"jxa stderr exceeded {_MAX_JXA_STDERR_BYTES // 1024 // 1024} MB"
                        )
                    stderr_chunks.append(chunk)
    finally:
        selector.close()

    returncode = process.wait()
    output = b"".join(stdout_chunks).decode("utf-8", "replace").strip()
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()

    if returncode != 0:
        raise JXAError(f"jxa error: {stderr}", stderr)

    return output


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
    """enrich message dicts with safe content previews from index or disk.

    Pipeline:
    1. mgr.maybe_prune()           -- auto-prune if DB > 256 MB
    2. mgr.batch_content()         -- index lookup with ID-shift self-healing
    3. mgr.targeted_index()        -- find .emlx files on disk for misses

    Missing content stays unavailable; the skill does not pull full bodies
    through Mail.app as a fallback.
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
            else:
                entry["preview_source"] = "not_indexed"
                entry["preview_truncated"] = False
                entry["preview_available"] = False
            enriched.append(entry)

        return _build_wrapper(enriched, mgr)
    finally:
        mgr.close()


def _build_wrapper(
    enriched: list[dict],
    mgr,
) -> dict:
    """build the standard enrichment wrapper dict."""
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
            "Use read-email for cached/disk content, or build-index for deeper triage."
        ),
    }

    return wrapper
