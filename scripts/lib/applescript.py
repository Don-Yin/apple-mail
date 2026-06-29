"""AppleScript execution, validation, and escaping utilities for Apple Mail."""

import re
import subprocess
import threading
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


def _capture_frontmost_app() -> tuple[str, str] | None:
    script = """
        tell application "System Events"
            set frontProc to first application process whose frontmost is true
            set procName to name of frontProc
            set procBundle to ""
            try
                set procBundle to bundle identifier of frontProc
            end try
            return procBundle & "\t" & procName
        end tell
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    bundle_id, _, name = result.stdout.strip().partition("\t")
    if not bundle_id and not name:
        return None
    return bundle_id, name


def _restore_frontmost_app(frontmost: tuple[str, str] | None):
    if not frontmost:
        return

    bundle_id, name = frontmost
    bundle_escaped = escape_applescript(bundle_id)
    name_escaped = escape_applescript(name)
    script = f"""
        set restoredFocus to false
        if "{bundle_escaped}" is not "" then
            try
                tell application id "{bundle_escaped}" to activate
                set restoredFocus to true
            end try
        end if
        if restoredFocus is false and "{name_escaped}" is not "" then
            try
                tell application "{name_escaped}" to activate
            end try
        end if
    """
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _frontmost_app_fast() -> tuple[str, str] | None:
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"],
            capture_output=True,
            text=True,
            timeout=1,
        ).stdout.strip()
        info = subprocess.run(
            ["lsappinfo", "info", "-only", "name,bundleID", asn],
            capture_output=True,
            text=True,
            timeout=1,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None

    name_match = re.search(r'"LSDisplayName"="([^"]*)"', info)
    bundle_match = re.search(r'"CFBundleIdentifier"="([^"]*)"', info)
    bundle_id = bundle_match.group(1) if bundle_match else ""
    name = name_match.group(1) if name_match else ""
    if not bundle_id and not name:
        return None
    if bundle_id == "com.apple.loginwindow" or name == "loginwindow":
        fallback = _capture_frontmost_app()
        if fallback and fallback[0] != "com.apple.loginwindow" and fallback[1] != "loginwindow":
            return fallback
    return bundle_id, name


def _activate_frontmost_app(frontmost: tuple[str, str] | None):
    if not frontmost:
        return
    bundle_id, name = frontmost
    if bundle_id:
        try:
            subprocess.run(
                ["open", "-b", bundle_id],
                capture_output=True,
                text=True,
                timeout=1,
            )
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
    _restore_frontmost_app((bundle_id, name))


def capture_frontmost_app() -> tuple[str, str] | None:
    """Capture the current foreground app as (bundle_id, display_name)."""
    return _capture_frontmost_app()


def restore_frontmost_app(frontmost: tuple[str, str] | None):
    """Best-effort restore of a foreground app captured by capture_frontmost_app()."""
    _activate_frontmost_app(frontmost)


def frontmost_app_fast() -> tuple[str, str] | None:
    """Fast foreground-app probe used for post-operation focus verification."""
    return _frontmost_app_fast()


def _start_focus_guard(frontmost: tuple[str, str] | None) -> tuple[threading.Event | None, threading.Thread | None]:
    if not frontmost or frontmost[0] == "com.apple.mail":
        return None, None

    stop = threading.Event()

    def guard():
        while not stop.is_set():
            current = _frontmost_app_fast()
            if current and (current[0] == "com.apple.mail" or current[1] == "Mail"):
                _activate_frontmost_app(frontmost)
            time.sleep(0.01)

    thread = threading.Thread(target=guard, daemon=True)
    thread.start()
    return stop, thread


def _start_mail_keyboard_shield() -> tuple[threading.Event | None, list[threading.Thread], bool]:
    """Drop key events during a protected compose operation.

    The reliable no-residue Mail draft path briefly uses a visible compose
    backend. During that short window, typing must not land in Mail or any app.
    """
    try:
        import Quartz  # type: ignore
        from CoreFoundation import (  # type: ignore
            CFRunLoopAddSource,
            CFRunLoopGetCurrent,
            CFRunLoopRunInMode,
            kCFRunLoopDefaultMode,
        )
    except Exception:
        return None, [], False

    stop = threading.Event()
    taps = []

    def callback(proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout:
            for active_tap in taps:
                Quartz.CGEventTapEnable(active_tap, True)
            return event
        return None

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
    locations = [
        Quartz.kCGHIDEventTap,
        Quartz.kCGSessionEventTap,
        Quartz.kCGAnnotatedSessionEventTap,
    ]
    for location in locations:
        tap = Quartz.CGEventTapCreate(
            location,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            callback,
            None,
        )
        if tap is not None:
            taps.append(tap)
    if not taps:
        return None, [], False

    def event_loop():
        for tap in taps:
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode)
            Quartz.CGEventTapEnable(tap, True)
        while not stop.is_set():
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)
        for tap in taps:
            Quartz.CGEventTapEnable(tap, False)

    tap_thread = threading.Thread(target=event_loop, daemon=True)
    tap_thread.start()
    return stop, [tap_thread], True


def run_applescript(
    script: str,
    timeout: int = 120,
    preserve_focus: bool = False,
    guard_focus: bool = True,
    shield_mail_keyboard: bool = False,
    require_keyboard_shield: bool = False,
) -> subprocess.CompletedProcess:
    """Execute an AppleScript via osascript with timeout and error checking."""
    frontmost = _capture_frontmost_app() if preserve_focus else None
    focus_stop, focus_thread = _start_focus_guard(frontmost) if preserve_focus and guard_focus else (None, None)
    shield_stop, shield_threads, shielded = (
        _start_mail_keyboard_shield() if shield_mail_keyboard else (None, [], False)
    )
    if require_keyboard_shield and not shielded:
        if focus_stop is not None:
            focus_stop.set()
        if focus_thread is not None:
            focus_thread.join(timeout=1)
        raise RuntimeError("Mail keyboard shield unavailable; refusing to open a typeable compose window")
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"applescript timed out after {timeout}s")
    finally:
        if shield_stop is not None:
            shield_stop.set()
        for shield_thread in shield_threads:
            shield_thread.join(timeout=1)
        if focus_stop is not None:
            focus_stop.set()
        if focus_thread is not None:
            focus_thread.join(timeout=1)
        if preserve_focus:
            _activate_frontmost_app(frontmost)

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
    """Build AppleScript attachment section.

    Mail.app is picky about attachment insertion. Passing a bare string as the
    `file name` can appear to succeed while leaving no visible attachment in the
    saved draft. Use a POSIX file object and attach it at the end of the message
    body, which matches the reliable Apple Mail scripting pattern.
    """
    if not attachment_paths:
        return ""

    lines = []
    for idx, path in enumerate(attachment_paths, start=1):
        var_name = f"attachmentFile{idx}"
        lines.extend([
            f'set {var_name} to POSIX file "{escape_applescript(path)}"',
            f'tell content of {target}',
            f'    make new attachment with properties {{file name:{var_name}}} at after the last paragraph',
            f'end tell',
        ])
    return "\n            ".join(lines) + "\n            "


def sync_mail_state(delay_seconds: float = 0.3, preserve_focus: bool = False):
    """Synchronize with Mail.app to ensure operations complete and IDs are stable."""
    script = """
        tell application "Mail"
            repeat with acc in accounts
                try
                    synchronize with acc
                end try
            end repeat
        end tell
    """
    try:
        run_applescript(script, timeout=10, preserve_focus=preserve_focus)
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
