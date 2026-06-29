"""Local diagnostics used to harden Mail.app mutation paths."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path


MAIL_DIAGNOSTIC_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"


def _parse_report_header(path: Path) -> dict:
    try:
        first_line = path.read_text(errors="replace").splitlines()[0]
        header = json.loads(first_line)
    except Exception:
        return {}

    timestamp_epoch = None
    raw_timestamp = header.get("timestamp") or header.get("captureTime")
    if raw_timestamp:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
            try:
                timestamp_epoch = datetime.strptime(raw_timestamp, fmt).timestamp()
                break
            except ValueError:
                pass

    return {
        "app_name": header.get("app_name") or header.get("procName"),
        "timestamp": raw_timestamp,
        "timestamp_epoch": timestamp_epoch,
        "incident_id": header.get("incident_id") or header.get("incident"),
        "bug_type": header.get("bug_type"),
    }


def mail_crash_report_snapshot() -> dict[str, dict]:
    """Return a compact snapshot of current Mail crash reports."""
    reports: dict[str, dict] = {}
    if not MAIL_DIAGNOSTIC_DIR.exists():
        return reports

    for path in MAIL_DIAGNOSTIC_DIR.glob("Mail-*.ips"):
        try:
            stat = path.stat()
        except OSError:
            continue
        header = _parse_report_header(path)
        reports[str(path)] = {
            "path": str(path),
            "mtime": stat.st_mtime,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            **header,
        }
    return reports


def changed_mail_crash_reports(before: dict[str, dict], started_at: float | None = None) -> list[dict]:
    """Detect Mail crash reports created or modified after a mutation attempt."""
    started_at = started_at or time.time()
    after = mail_crash_report_snapshot()
    changed = []

    for path, report in after.items():
        prior = before.get(path)
        is_new = prior is None
        is_modified = (
            prior is not None
            and (
                prior.get("mtime_ns") != report.get("mtime_ns")
                or prior.get("size") != report.get("size")
                or prior.get("incident_id") != report.get("incident_id")
            )
        )
        if not is_new and not is_modified:
            continue

        event_epoch = report.get("timestamp_epoch")
        if event_epoch is not None and event_epoch < started_at - 5:
            continue
        report = {**report, "changed_after_start": True, "event_before_start": False}
        changed.append(report)

    changed.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    return changed


def newest_mail_crash_report() -> dict | None:
    reports = list(mail_crash_report_snapshot().values())
    if not reports:
        return None
    reports.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    return reports[0]
