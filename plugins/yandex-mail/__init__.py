"""Hermes registration for narrow Yandex Mail tools."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .auth import get_access_token
from .mailbox import (
    ABSOLUTE_MAX_MESSAGE_BYTES,
    DEFAULT_BODY_CHAR_LIMIT,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_MAX_MESSAGE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_BODY_CHAR_LIMIT,
    SECURITY_NOTICE,
    YandexReadonlyMailbox,
)
from .schemas import (
    YANDEX_MAIL_LIST_INBOX,
    YANDEX_MAIL_MARK_READ,
    YANDEX_MAIL_READ_MESSAGE,
)


def _json_result(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _configuration_error() -> str:
    return _json_result(
        {
            "success": False,
            "security_notice": SECURITY_NOTICE,
            "error": {
                "code": "INVALID_CONFIGURATION",
                "message": "The Yandex Mail plugin configuration is invalid.",
            },
        }
    )


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its supported range")
    return value


def _build_mailbox() -> YandexReadonlyMailbox:
    timeout = _env_int(
        "YANDEX_MAIL_IMAP_TIMEOUT",
        int(DEFAULT_TIMEOUT_SECONDS),
        minimum=1,
        maximum=120,
    )
    body_char_limit = _env_int(
        "YANDEX_MAIL_BODY_CHAR_LIMIT",
        DEFAULT_BODY_CHAR_LIMIT,
        minimum=1,
        maximum=MAX_BODY_CHAR_LIMIT,
    )
    max_message_bytes = _env_int(
        "YANDEX_MAIL_MAX_MESSAGE_BYTES",
        DEFAULT_MAX_MESSAGE_BYTES,
        minimum=1,
        maximum=ABSOLUTE_MAX_MESSAGE_BYTES,
    )
    return YandexReadonlyMailbox(
        token_provider=get_access_token,
        # OAuth bearer tokens must only be sent to Yandex's official IMAP
        # endpoint.  Host and port are intentionally not configurable.
        host=DEFAULT_IMAP_HOST,
        port=DEFAULT_IMAP_PORT,
        timeout=timeout,
        body_char_limit=body_char_limit,
        max_message_bytes=max_message_bytes,
    )


def _list_inbox(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict):
        args = {}
    try:
        mailbox = _build_mailbox()
    except Exception:  # noqa: BLE001 - configuration values must not leak
        return _configuration_error()
    return _json_result(
        mailbox.list_inbox(
            limit=args.get("limit", 20),
            unread_only=args.get("unread_only", False),
            before_uid=args.get("before_uid"),
        )
    )


def _read_message(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict):
        args = {}
    try:
        mailbox = _build_mailbox()
    except Exception:  # noqa: BLE001 - configuration values must not leak
        return _configuration_error()
    return _json_result(
        mailbox.read_message(
            args.get("uid"),
            expected_uidvalidity=args.get("uidvalidity"),
            body_char_limit=args.get("body_char_limit"),
        )
    )


def _mark_read(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    if not isinstance(args, dict):
        args = {}
    try:
        mailbox = _build_mailbox()
    except Exception:  # noqa: BLE001 - configuration values must not leak
        return _configuration_error()
    return _json_result(
        mailbox.mark_read(
            args.get("uid"),
            expected_uidvalidity=args.get("uidvalidity"),
        )
    )


def register(ctx: Any) -> None:
    """Register the three narrow mailbox tools."""
    ctx.register_tool(
        name="yandex_mail_list_inbox",
        toolset="yandex_mail",
        schema=YANDEX_MAIL_LIST_INBOX,
        handler=_list_inbox,
        description="List newest messages in Yandex Mail INBOX (strictly read-only).",
    )
    ctx.register_tool(
        name="yandex_mail_read_message",
        toolset="yandex_mail",
        schema=YANDEX_MAIL_READ_MESSAGE,
        handler=_read_message,
        description="Read one Yandex Mail INBOX message by IMAP UID (strictly read-only).",
    )
    ctx.register_tool(
        name="yandex_mail_mark_read",
        toolset="yandex_mail",
        schema=YANDEX_MAIL_MARK_READ,
        handler=_mark_read,
        description="Mark one verified Yandex Mail INBOX message as read.",
    )
