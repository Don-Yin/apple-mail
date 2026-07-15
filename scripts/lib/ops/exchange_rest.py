"""Optional server-side Exchange draft adapter.

The public Apple Mail skill does not ship an Exchange implementation. Users opt in
with an executable configured through APPLE_MAIL_WEB_EXCHANGE_CLI and an explicit
account allowlist in APPLE_MAIL_EXCHANGE_REST_ACCOUNTS. Requests use protocol v1
JSON over stdin so subjects, bodies, recipients, and attachment paths do not appear
in process arguments.
"""

from __future__ import annotations

import json
import os
import subprocess
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path


WEB_EXCHANGE_CLI_ENV = "APPLE_MAIL_WEB_EXCHANGE_CLI"
EXCHANGE_REST_ACCOUNTS_ENV = "APPLE_MAIL_EXCHANGE_REST_ACCOUNTS"
ADAPTER_PROTOCOL_VERSION = 1


def exchange_rest_accounts() -> set[str]:
    raw = os.environ.get(EXCHANGE_REST_ACCOUNTS_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def exchange_adapter_path() -> Path | None:
    configured = os.environ.get(WEB_EXCHANGE_CLI_ENV, "").strip()
    return Path(configured).expanduser() if configured else None


def exchange_adapter_configured() -> bool:
    path = exchange_adapter_path()
    return bool(path and path.is_file() and os.access(path, os.X_OK))


def account_uses_exchange_rest(account_email: str | None) -> bool:
    account = (account_email or "").strip().lower()
    return bool(account and exchange_adapter_configured() and account in exchange_rest_accounts())


def exchange_adapter_metadata() -> dict:
    path = exchange_adapter_path()
    configured = exchange_adapter_configured()
    return {
        "protocol_version": ADAPTER_PROTOCOL_VERSION,
        "configured": configured,
        "cli_env": WEB_EXCHANGE_CLI_ENV,
        "accounts_env": EXCHANGE_REST_ACCOUNTS_ENV,
        "configured_accounts": sorted(exchange_rest_accounts()),
        "path_state": (
            "not-configured"
            if path is None
            else "ready"
            if configured
            else "missing-or-not-executable"
        ),
    }


def _configuration_error(account_email: str | None = None) -> dict | None:
    path = exchange_adapter_path()
    if path is None:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_NOT_CONFIGURED",
            "message": f"set {WEB_EXCHANGE_CLI_ENV} to an executable Exchange adapter",
            "backend": "exchange-rest",
        }
    if not path.exists():
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_NOT_FOUND",
            "message": "configured Exchange adapter path does not exist",
            "backend": "exchange-rest",
        }
    if not path.is_file() or not os.access(path, os.X_OK):
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_NOT_EXECUTABLE",
            "message": "configured Exchange adapter is not an executable file",
            "backend": "exchange-rest",
        }
    account = (account_email or "").strip().lower()
    if account and account not in exchange_rest_accounts():
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_ACCOUNT_NOT_CONFIGURED",
            "message": f"account is not listed in {EXCHANGE_REST_ACCOUNTS_ENV}",
            "backend": "exchange-rest",
            "account_email": account_email,
            "configured_accounts": sorted(exchange_rest_accounts()),
        }
    return None


def _adapter_error_summary(payload: dict) -> dict:
    error = payload.get("error") or {}
    candidate = error.get("code") or payload.get("code") or "EXCHANGE_ADAPTER_FAILED"
    code = (
        candidate
        if isinstance(candidate, str)
        and len(candidate) <= 64
        and candidate.replace("_", "").isalnum()
        else "EXCHANGE_ADAPTER_FAILED"
    )
    return {
        "code": code,
        "message": "Exchange adapter operation failed",
        "details": {},
    }


def _run_exchange_adapter(
    *,
    operation: str,
    account_email: str,
    payload: dict | None = None,
    auth_mode: str = "background",
    timeout: int = 90,
) -> dict:
    configuration_error = _configuration_error(account_email)
    if configuration_error:
        return configuration_error

    path = exchange_adapter_path()
    assert path is not None
    request = {
        "protocol_version": ADAPTER_PROTOCOL_VERSION,
        "operation": operation,
        "account": account_email,
        "auth_mode": auth_mode,
        "payload": payload or {},
    }
    try:
        proc = subprocess.run(
            [str(path)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_TIMEOUT",
            "message": f"Exchange adapter operation {operation!r} timed out after {timeout}s",
            "backend": "exchange-rest",
        }
    except OSError as exc:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_LAUNCH_FAILED",
            "message": f"could not launch Exchange adapter: {exc.strerror or exc.__class__.__name__}",
            "backend": "exchange-rest",
        }

    try:
        response = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_BAD_OUTPUT",
            "message": "Exchange adapter did not return one valid JSON response",
            "backend": "exchange-rest",
            "exit_code": proc.returncode,
        }
    if not isinstance(response, dict):
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_BAD_OUTPUT",
            "message": "Exchange adapter response must be a JSON object",
            "backend": "exchange-rest",
            "exit_code": proc.returncode,
        }
    if response.get("protocol_version") != ADAPTER_PROTOCOL_VERSION:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_PROTOCOL_MISMATCH",
            "message": f"Exchange adapter must implement protocol version {ADAPTER_PROTOCOL_VERSION}",
            "backend": "exchange-rest",
        }
    if proc.returncode != 0 and response.get("success") is not False:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_FAILED",
            "message": f"Exchange adapter exited with status {proc.returncode}",
            "backend": "exchange-rest",
        }
    if response.get("success"):
        data = response.get("data") or {}
        returned_account = (data.get("account_email") or "").strip().lower()
        if returned_account != account_email.strip().lower():
            return {
                "success": False,
                "code": "EXCHANGE_ADAPTER_ACCOUNT_MISMATCH",
                "message": "Exchange adapter response did not identify the requested authenticated account",
                "backend": "exchange-rest",
                "account_email": account_email,
            }
    return response


def _auth_required_result(
    account_email: str,
    reason: str,
    adapter_response: dict,
    *,
    message: str | None = None,
    focus_safe: bool = True,
) -> dict:
    return {
        "success": False,
        "code": "EXCHANGE_AUTH_REQUIRED",
        "message": message or "Exchange background auth is not ready; run exchange-auth-login explicitly",
        "backend": "exchange-rest",
        "account_email": account_email,
        "reason": reason,
        "focus_safe": focus_safe,
        "recommended_command": f"scripts/mail.sh exchange-auth-login --account {account_email}",
        "auth": {
            "reason": reason,
            "focus_safe": focus_safe,
            "interactive_login_required": True,
            "details": {},
        },
    }


def _map_auth_failure(
    account_email: str,
    response: dict,
    *,
    default_message: str,
    focus_safe: bool = True,
) -> dict | None:
    error = response.get("error") or {}
    code = error.get("code") or response.get("code")
    if code == "AUTH_REQUIRED":
        details = error.get("details") or {}
        candidate_reason = details.get("reason")
        allowed_reasons = {"login_required", "token_unavailable", "consent_required", "token_expired"}
        reason = candidate_reason if candidate_reason in allowed_reasons else "token_unavailable"
        return _auth_required_result(
            account_email,
            reason,
            response,
            message=default_message,
            focus_safe=focus_safe,
        )
    if code in {"HTTP_401", "UNAUTHORIZED"}:
        return _auth_required_result(
            account_email,
            "http_401",
            response,
            message="Exchange auth expired or was rejected; run exchange-auth-login explicitly",
            focus_safe=focus_safe,
        )
    return None


def _norm_addr_list(values: list[str] | None) -> list[str]:
    return sorted((value or "").strip().lower() for value in (values or []) if (value or "").strip())


def _norm_body(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").strip()


def _norm_body_exact(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def _arial_html_body(body: str) -> str:
    rendered = escape(body.replace("\r\n", "\n")).replace("\n", "<br>")
    return (
        '<html><body><div style="font-family:Arial,sans-serif;font-size:11pt;white-space:pre-wrap;">'
        f"{rendered}</div></body></html>"
    )


def _style_declarations(value: str) -> list[tuple[str, str]]:
    declarations = []
    for declaration in value.split(";"):
        name, separator, property_value = declaration.partition(":")
        if separator:
            declarations.append((name.strip().lower(), property_value.strip()))
    return declarations


class _DraftHtmlValidator(HTMLParser):
    _allowed_attributes = {
        "html": set(),
        "head": set(),
        "meta": {"charset", "http-equiv", "content"},
        "body": set(),
        "div": {"style", "class", "dir"},
        "br": set(),
        "a": {"href", "title", "target", "rel", "data-outlook-id"},
    }
    _void_tags = {"meta", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.safe = True
        self.stack: list[str] = []
        self.counts = {"html": 0, "head": 0, "body": 0, "div": 0}
        self.body_seen = False
        self.root_div_attrs: dict[str, str | None] | None = None
        self.visible_parts: list[str] = []

    def _parent(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def _attributes_are_safe(self, name: str, attrs: list[tuple[str, str | None]]) -> bool:
        allowed = self._allowed_attributes.get(name)
        if allowed is None:
            return False
        normalized = {attr_name.lower(): (attr_value or "") for attr_name, attr_value in attrs}
        names = list(normalized)
        if (
            len(names) != len(attrs)
            or any(attr_name not in allowed for attr_name in names)
            or any(attr_name.startswith("on") for attr_name in names)
        ):
            return False
        if name == "meta":
            if set(names) == {"charset"}:
                return normalized["charset"].replace("-", "").lower() == "utf8"
            if set(names) == {"http-equiv", "content"}:
                return (
                    normalized["http-equiv"].lower() == "content-type"
                    and normalized["content"].replace(" ", "").lower() == "text/html;charset=utf-8"
                )
            return False
        if name == "div":
            if "class" in normalized and normalized["class"] != "PlainText":
                return False
            if "dir" in normalized and normalized["dir"].lower() not in {"ltr", "rtl"}:
                return False
        return True

    def _position_is_safe(self, name: str) -> bool:
        parent = self._parent()
        if name == "html":
            return parent is None and self.counts["html"] == 0
        if name == "head":
            return parent == "html" and self.counts["head"] == 0 and not self.body_seen
        if name == "meta":
            return parent == "head"
        if name == "body":
            return parent == "html" and self.counts["body"] == 0
        if name == "div":
            return parent == "body" and self.counts["div"] == 0
        if name == "a":
            return parent == "div"
        if name == "br":
            return parent in {"div", "a"}
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if not self._attributes_are_safe(name, attrs) or not self._position_is_safe(name):
            self.safe = False
            return
        if name in self.counts:
            self.counts[name] += 1
        if name == "body":
            self.body_seen = True
        if name == "div":
            self.root_div_attrs = {attr_name.lower(): attr_value for attr_name, attr_value in attrs}
        if name == "br" and "div" in self.stack:
            self.visible_parts.append("\n")
        if name not in self._void_tags:
            self.stack.append(name)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name not in self._void_tags:
            self.safe = False
            return
        self.handle_starttag(name, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in self._void_tags or not self.stack or self.stack[-1] != name:
            self.safe = False
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if "div" in self.stack:
            self.visible_parts.append(data)
        elif data.strip():
            self.safe = False

    def handle_entityref(self, name: str) -> None:
        if "div" in self.stack:
            self.visible_parts.append(unescape(f"&{name};"))
        else:
            self.safe = False

    def handle_charref(self, name: str) -> None:
        if "div" in self.stack:
            self.visible_parts.append(unescape(f"&#{name};"))
        else:
            self.safe = False

    def handle_comment(self, data: str) -> None:
        self.safe = False

    def handle_decl(self, decl: str) -> None:
        self.safe = False

    def handle_pi(self, data: str) -> None:
        self.safe = False


def _parse_draft_html(value: str | None) -> _DraftHtmlValidator | None:
    validator = _DraftHtmlValidator()
    try:
        validator.feed(value or "")
        validator.close()
    except Exception:
        return None
    valid = (
        validator.safe
        and not validator.stack
        and validator.counts["html"] == 1
        and validator.counts["head"] <= 1
        and validator.counts["body"] == 1
        and validator.counts["div"] == 1
        and validator.root_div_attrs is not None
    )
    return validator if valid else None


def _arial_wrapper_matches(parsed: _DraftHtmlValidator | None, requested_text: str) -> bool:
    if not parsed or parsed.root_div_attrs is None:
        return False
    style_value = parsed.root_div_attrs.get("style")
    if style_value is None:
        return False
    declarations = _style_declarations(style_value)
    names = [name for name, _ in declarations]
    properties = dict(declarations)
    allowed_root_properties = {"font-family", "font-size", "white-space", "color", "direction"}
    first_family = properties.get("font-family", "").split(",", 1)[0].strip(" \t'\"").lower()
    font_size = properties.get("font-size", "").replace(" ", "").lower()
    white_space = properties.get("white-space", "").replace(" ", "").lower()
    color = properties.get("color", "").replace(" ", "").lower()
    direction = properties.get("direction", "").replace(" ", "").lower()
    allowed_colors = {"", "black", "#000", "#000000", "rgb(0,0,0)"}
    return (
        len(names) == len(set(names))
        and set(names) <= allowed_root_properties
        and not any("!important" in property_value.lower() for _, property_value in declarations)
        and first_family == "arial"
        and font_size == "11pt"
        and white_space == "pre-wrap"
        and color in allowed_colors
        and direction in {"", "ltr", "rtl"}
        and _norm_body_exact("".join(parsed.visible_parts)) == _norm_body_exact(requested_text)
    )


def _verification_mismatches(
    *,
    draft: dict,
    account_email: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    requested_text: str | None = None,
    font: str = "provider-default",
) -> list[str]:
    mismatches = []
    if not draft.get("is_draft"):
        mismatches.append("is_draft")
    if (draft.get("account_email") or "").strip().lower() != account_email.strip().lower():
        mismatches.append("account_email")
    if draft.get("subject") != subject:
        mismatches.append("subject")
    content = draft.get("content") or ""
    parsed = _parse_draft_html(content) if requested_text is not None else None
    if requested_text is None:
        if _norm_body(content) != _norm_body(body):
            mismatches.append("body")
    elif not parsed or _norm_body_exact("".join(parsed.visible_parts)) != _norm_body_exact(requested_text):
        mismatches.append("body")
    if font == "arial" and not _arial_wrapper_matches(parsed, requested_text or ""):
        mismatches.append("font")
    if _norm_addr_list(draft.get("to_recipients")) != _norm_addr_list(to):
        mismatches.append("to_recipients")
    if _norm_addr_list(draft.get("cc_recipients")) != _norm_addr_list(cc):
        mismatches.append("cc_recipients")
    if _norm_addr_list(draft.get("bcc_recipients")) != _norm_addr_list(bcc):
        mismatches.append("bcc_recipients")
    if not draft.get("message_id"):
        mismatches.append("message_id")
    return mismatches


def compose_exchange_rest_draft(
    *,
    account_email: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    font: str = "provider-default",
    timeout: int = 90,
) -> dict:
    cc = cc or []
    bcc = bcc or []
    attachments = attachments or []
    if font not in {"arial", "provider-default"}:
        return {
            "success": False,
            "code": "UNSUPPORTED_DRAFT_FONT",
            "message": f"unsupported Exchange draft font: {font}",
            "backend": "exchange-rest",
        }
    configuration_error = _configuration_error(account_email)
    if configuration_error:
        return configuration_error

    rendered_body = _arial_html_body(body) if font == "arial" else body
    created = _run_exchange_adapter(
        operation="compose-draft",
        account_email=account_email,
        auth_mode="background",
        timeout=timeout,
        payload={
            "subject": subject,
            "body": rendered_body,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "attachments": attachments,
        },
    )
    if not created.get("success"):
        auth_failure = _map_auth_failure(
            account_email,
            created,
            default_message="Exchange background auth is not ready for compose-draft; run exchange-auth-login explicitly",
        )
        if auth_failure:
            return auth_failure
        error = _adapter_error_summary(created)
        return {"success": False, **error, "backend": "exchange-rest"}

    exchange_id = (created.get("data") or {}).get("id", "")
    if not exchange_id:
        return {
            "success": False,
            "code": "EXCHANGE_ADAPTER_MISSING_DRAFT_ID",
            "message": "Exchange adapter compose succeeded without returning a draft id",
            "backend": "exchange-rest",
        }

    readback = _run_exchange_adapter(
        operation="read-email",
        account_email=account_email,
        auth_mode="background",
        timeout=timeout,
        payload={"id": exchange_id},
    )
    if not readback.get("success"):
        auth_failure = _map_auth_failure(
            account_email,
            readback,
            default_message="Exchange background auth was lost during draft verification; run exchange-auth-login explicitly",
        )
        if auth_failure:
            return auth_failure
        error = _adapter_error_summary(readback)
        return {"success": False, **error, "backend": "exchange-rest", "exchange_id": exchange_id}

    draft = readback.get("data") or {}
    mismatches = _verification_mismatches(
        draft=draft,
        account_email=account_email,
        subject=subject,
        body=rendered_body,
        to=to,
        cc=cc,
        bcc=bcc,
        requested_text=body if font == "arial" else None,
        font=font,
    )
    if mismatches:
        return {
            "success": False,
            "code": "EXCHANGE_REST_VERIFY_FAILED",
            "message": "Exchange draft was created but readback did not match the request",
            "backend": "exchange-rest",
            "exchange_id": exchange_id,
            "verification": {"matched_server_draft": False, "mismatches": mismatches},
        }

    return {
        "success": True,
        "message": "Exchange draft created and verified server-side",
        "backend": "exchange-rest",
        "server_written": True,
        "mail_app_written": False,
        "verified": True,
        "exchange_id": exchange_id,
        "message_id": draft.get("message_id", ""),
        "account_email": account_email,
        "folder_name": "Drafts",
        "font": font,
        "draft": draft,
        "verification": {"matched_server_draft": True, "is_draft": True},
    }


def exchange_auth_status(*, account_email: str, timeout: int = 30) -> dict:
    configuration_error = _configuration_error(account_email)
    if configuration_error:
        return configuration_error
    status = _run_exchange_adapter(
        operation="status",
        account_email=account_email,
        auth_mode="background",
        timeout=timeout,
    )
    if not status.get("success"):
        auth_failure = _map_auth_failure(
            account_email,
            status,
            default_message="Exchange background auth status could not be determined",
        )
        if auth_failure:
            return auth_failure
        error = _adapter_error_summary(status)
        return {"success": False, **error, "backend": "exchange-rest"}
    data = status.get("data") or {}
    return {
        "success": True,
        "backend": "exchange-rest",
        "account_email": account_email,
        "ready": bool(data.get("ready")),
        "reason": data.get("reason"),
        "focus_safe": True,
        "interactive_login_required": bool(data.get("interactive_login_required")),
        "recommended_command": (
            f"scripts/mail.sh exchange-auth-login --account {account_email}"
            if not data.get("ready")
            else None
        ),
        "status": data,
    }


def exchange_auth_login(*, account_email: str, timeout: int = 90) -> dict:
    configuration_error = _configuration_error(account_email)
    if configuration_error:
        return configuration_error
    login = _run_exchange_adapter(
        operation="login",
        account_email=account_email,
        auth_mode="interactive",
        timeout=timeout,
    )
    if not login.get("success"):
        auth_failure = _map_auth_failure(
            account_email,
            login,
            default_message="Exchange interactive login did not complete",
            focus_safe=False,
        )
        if auth_failure:
            return auth_failure
        error = _adapter_error_summary(login)
        return {"success": False, **error, "backend": "exchange-rest"}
    data = login.get("data") or {}
    return {
        "success": True,
        "message": "Exchange interactive auth is ready",
        "backend": "exchange-rest",
        "account_email": account_email,
        "ready": bool(data.get("ready")),
        "reason": data.get("reason"),
        "focus_safe": False,
        "interactive_login_required": False,
        "status": data,
    }
