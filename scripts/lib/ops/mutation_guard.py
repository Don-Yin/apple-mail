"""Guardrails for live Mail.app write/delete operations."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from ..applescript import capture_frontmost_app, frontmost_app_fast, restore_frontmost_app
from ..diagnostics import changed_mail_crash_reports, mail_crash_report_snapshot
from .health import health_check


ALLOW_UI_MUTATION_ENV = "APPLE_MAIL_ALLOW_UI_MUTATION"
ALLOW_UI_MUTATION_COMMAND_ENV = "APPLE_MAIL_ALLOW_UI_MUTATION_COMMAND"


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_operations() -> set[str]:
    raw = os.environ.get(ALLOW_UI_MUTATION_COMMAND_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def live_mail_mutation_allowed(operation: str) -> bool:
    return _truthy_env(ALLOW_UI_MUTATION_ENV) and operation.lower() in _allowed_operations()


def live_mail_mutation_disabled(operation: str) -> dict:
    return {
        "success": False,
        "code": "MAIL_UI_MUTATION_DISABLED",
        "message": (
            f"{operation} is disabled by default because live Apple Mail write/delete scripting "
            "has crashed Mail during testing. Use hidden compose-draft for synced drafts, "
            "or compose-draft --backend artifact for a no-Mail draft file. "
            "Direct developer calls must set both "
            f"{ALLOW_UI_MUTATION_ENV}=1 and {ALLOW_UI_MUTATION_COMMAND_ENV}={operation!r}."
        ),
        "override_env": ALLOW_UI_MUTATION_ENV,
        "command_env": ALLOW_UI_MUTATION_COMMAND_ENV,
    }


def require_live_mail_mutation(operation: str) -> dict | None:
    if live_mail_mutation_allowed(operation):
        return None
    return live_mail_mutation_disabled(operation)


def _health_failed(health: dict) -> bool:
    return health.get("success") is False


def _focus_status(before: tuple[str, str] | None) -> dict:
    after = frontmost_app_fast()
    restored = False
    if before is None:
        restored = after is None
    elif after is not None:
        restored = bool(before[0] and before[0] == after[0]) or bool(before[1] and before[1] == after[1])
    return {
        "before": {"bundle_id": before[0], "name": before[1]} if before else None,
        "after": {"bundle_id": after[0], "name": after[1]} if after else None,
        "restored": restored,
    }


def run_guarded_local_mail_mutation(operation: str, action: Callable[[], dict]) -> dict:
    """Run a Mail.app mutation with repeatable safety checks around it.

    This does not authorize a mutation. Callers must still pass require_live_mail_mutation()
    first. The wrapper makes allowed local mutations auditable: pre/post Mail health,
    foreground-app restoration, and crash-report deltas are returned with every result.
    """
    started_at = time.time()
    frontmost_before = capture_frontmost_app()
    crash_before = mail_crash_report_snapshot()
    safety = {
        "operation": operation,
        "backend": "mailapp-local",
        "started_at": started_at,
        "pre_health": None,
        "post_health": None,
        "focus": None,
        "new_or_changed_crash_reports": [],
    }

    pre_health = health_check()
    safety["pre_health"] = pre_health
    if _health_failed(pre_health):
        return {
            "success": False,
            "code": "MAIL_HEALTH_PRECHECK_FAILED",
            "message": pre_health.get("message", "Mail.app health precheck failed"),
            "local_mail_safety": safety,
        }

    try:
        result = action()
    except Exception as exc:
        result = {
            "success": False,
            "code": "LOCAL_MAIL_MUTATION_EXCEPTION",
            "message": str(exc),
        }
    finally:
        restore_frontmost_app(frontmost_before)

    post_health = health_check()
    restore_frontmost_app(frontmost_before)
    safety["post_health"] = post_health
    safety["focus"] = _focus_status(frontmost_before)
    safety["new_or_changed_crash_reports"] = changed_mail_crash_reports(crash_before, started_at)
    safety["finished_at"] = time.time()
    safety["duration_ms"] = round((safety["finished_at"] - started_at) * 1000, 1)

    if not isinstance(result, dict):
        result = {"success": False, "code": "INVALID_MUTATION_RESULT", "message": "mutation returned non-dict result"}
    result = {**result, "local_mail_safety": safety}

    if safety["new_or_changed_crash_reports"]:
        return {
            **result,
            "success": False,
            "code": "MAIL_CRASH_DETECTED",
            "message": (
                result.get("message", "Mail.app mutation finished")
                + "; new or changed Mail crash report detected after mutation"
            ),
        }

    if _health_failed(post_health):
        return {
            **result,
            "success": False,
            "code": "MAIL_HEALTH_POSTCHECK_FAILED",
            "message": post_health.get("message", "Mail.app health postcheck failed"),
        }

    if safety["focus"]["before"] and not safety["focus"]["restored"]:
        return {
            **result,
            "success": False,
            "code": "MAIL_FOCUS_RESTORE_FAILED",
            "message": (
                result.get("message", "Mail.app mutation finished")
                + "; foreground app was not restored after mutation"
            ),
        }

    return result
