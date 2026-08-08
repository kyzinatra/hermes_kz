"""Minimal, read-only Yandex Mail OAuth support.

The module deliberately uses only the Python standard library.  Its public
entry point for the mail plugin is :func:`get_access_token`, which returns a
``(username, access_token)`` tuple and refreshes an expiring token when
necessary.

No exception raised here includes credentials, authorization codes, tokens,
or response bodies from Yandex.
"""

from __future__ import annotations

import contextlib
import imaplib
import json
import math
import os
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


PathLike = Union[str, os.PathLike[str]]
OpenUrl = Callable[..., Any]

DEFAULT_CREDENTIALS_PATH = Path("/credentials/yandex-mail-oauth.json")
DEFAULT_TOKEN_PATH = Path("/opt/data/yandex-mail/oauth.json")

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
REDIRECT_URI = "https://oauth.yandex.ru/verification_code"
REQUIRED_SCOPE = "mail:imap_ro"
FORBIDDEN_SCOPES = frozenset({"mail:imap_full", "mail:smtp"})

IMAP_HOST = "imap.yandex.com"
IMAP_PORT = 993

_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_JSON_BYTES = 1024 * 1024
_DEFAULT_MIN_VALIDITY_SECONDS = 60.0

_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class YandexMailAuthError(RuntimeError):
    """A safe-to-display configuration, storage, or authorization error."""


class CredentialsError(YandexMailAuthError):
    """The OAuth client credentials file is missing or invalid."""


class TokenError(YandexMailAuthError):
    """The saved token is missing, invalid, expired, or unsafe."""


class OAuthRequestError(YandexMailAuthError):
    """Yandex rejected a request or returned an invalid response."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    username: str


def _as_path(value: PathLike) -> Path:
    return value if isinstance(value, Path) else Path(value)


def _read_json(path: Path, *, description: str, missing_ok: bool = False) -> Any:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_JSON_BYTES + 1)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise YandexMailAuthError(f"{description} file was not found: {path}") from None
    except OSError:
        raise YandexMailAuthError(f"Could not read the {description} file: {path}") from None

    if len(raw) > _MAX_JSON_BYTES:
        raise YandexMailAuthError(f"The {description} file is too large: {path}")

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise YandexMailAuthError(
            f"The {description} file is not valid UTF-8 JSON: {path}"
        ) from None


def load_credentials(
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
) -> Credentials:
    """Load and validate the non-token OAuth configuration."""

    path = _as_path(credentials_path)
    try:
        payload = _read_json(path, description="credentials")
    except YandexMailAuthError as exc:
        raise CredentialsError(str(exc)) from None

    if not isinstance(payload, dict):
        raise CredentialsError("The credentials file must contain a JSON object")

    values: Dict[str, str] = {}
    for field in ("client_id", "client_secret", "username"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CredentialsError(
                f"The credentials file is missing a non-empty '{field}' field"
            )
        values[field] = value.strip()

    return Credentials(**values)


def build_authorization_url(
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
) -> str:
    """Build the manual authorization-code URL with exactly one mail scope."""

    credentials = load_credentials(credentials_path)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": credentials.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": REQUIRED_SCOPE,
            "force_confirm": "yes",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def extract_authorization_code(code_or_callback_url: str) -> str:
    """Accept a bare authorization code or the complete manual callback URL."""

    if not isinstance(code_or_callback_url, str):
        raise OAuthRequestError("An authorization code is required")
    supplied = code_or_callback_url.strip()
    if not supplied:
        raise OAuthRequestError("An authorization code is required")

    parsed = urlsplit(supplied)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "oauth.yandex.ru":
            raise OAuthRequestError("The authorization callback URL is not from Yandex")
        if parsed.path.rstrip("/") != "/verification_code":
            raise OAuthRequestError("The authorization callback URL has an unexpected path")

        parameters = parse_qs(parsed.query, keep_blank_values=True)
        if not parameters and parsed.fragment:
            parameters = parse_qs(parsed.fragment, keep_blank_values=True)
        if "error" in parameters:
            raise OAuthRequestError("Yandex did not grant authorization")
        codes = parameters.get("code", [])
        if len(codes) != 1 or not codes[0].strip():
            raise OAuthRequestError("The authorization callback URL has no code")
        supplied = codes[0].strip()

    if len(supplied) > 4096 or any(character.isspace() for character in supplied):
        raise OAuthRequestError("The authorization code has an invalid format")
    return supplied


def _parse_and_validate_scope(value: Any) -> str:
    if not isinstance(value, str):
        raise TokenError("The OAuth token has no verifiable scope")
    scopes = frozenset(value.split())
    if scopes != {REQUIRED_SCOPE} or scopes.intersection(FORBIDDEN_SCOPES):
        raise TokenError("The OAuth token has unsafe or insufficient permissions")
    return " ".join(sorted(scopes))


def _post_form(
    fields: Mapping[str, str],
    *,
    opener: OpenUrl = urlopen,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    body = urlencode(fields).encode("ascii")
    request = Request(
        TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or status < 200 or status >= 300:
                raise OAuthRequestError("Yandex OAuth returned an unsuccessful status")
            raw = response.read(_MAX_JSON_BYTES + 1)
    except HTTPError as exc:
        status = exc.code if isinstance(exc.code, int) else "unknown"
        raise OAuthRequestError(
            f"Yandex OAuth rejected the request (HTTP {status})"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise OAuthRequestError("Could not contact Yandex OAuth") from None
    except OAuthRequestError:
        raise
    except Exception:
        # Custom transports and TLS implementations can raise many exception
        # types.  Never expose their messages: they may contain request data.
        raise OAuthRequestError("Could not complete the Yandex OAuth request") from None

    if len(raw) > _MAX_JSON_BYTES:
        raise OAuthRequestError("Yandex OAuth returned an oversized response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OAuthRequestError("Yandex OAuth returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise OAuthRequestError("Yandex OAuth returned an unexpected response")
    return payload


def _normalise_token_response(
    payload: Mapping[str, Any],
    *,
    username: str,
    previous: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthRequestError("Yandex OAuth did not return an access token")

    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OAuthRequestError("Yandex OAuth returned an unsupported token type")

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool):
        raise OAuthRequestError("Yandex OAuth returned an invalid token lifetime")
    try:
        lifetime = float(expires_in)
    except (TypeError, ValueError, OverflowError):
        raise OAuthRequestError("Yandex OAuth returned an invalid token lifetime") from None
    if not math.isfinite(lifetime) or lifetime <= 0:
        raise OAuthRequestError("Yandex OAuth returned an invalid token lifetime")

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = previous.get("refresh_token") if previous is not None else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthRequestError("Yandex OAuth did not return a refresh token")

    scope_value = payload.get("scope")
    if scope_value is None and previous is not None:
        scope_value = previous.get("scope")
    if scope_value is None:
        # Yandex documents `scope` as optional when the granted rights equal
        # the explicitly requested rights.  This request always asks for one
        # exact scope, so that is the effective scope in this case.
        scope_value = REQUIRED_SCOPE
    try:
        scope = _parse_and_validate_scope(scope_value)
    except TokenError as exc:
        raise OAuthRequestError(str(exc)) from None

    obtained_at = time.time()
    return {
        "version": 1,
        "username": username,
        "token_type": "bearer",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scope": scope,
        "obtained_at": obtained_at,
        "expires_at": obtained_at + lifetime,
    }


def _thread_lock_for(token_path: Path) -> threading.RLock:
    try:
        key = str(token_path.resolve(strict=False))
    except OSError:
        key = os.path.abspath(os.fspath(token_path))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _token_lock(token_path: Path) -> Iterator[None]:
    """Serialize token changes across threads and, on Unix, processes."""

    thread_lock = _thread_lock_for(token_path)
    with thread_lock:
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(token_path.parent, 0o700)
        except OSError:
            raise TokenError(
                f"Could not create or secure the token directory: {token_path.parent}"
            ) from None

        lock_path = token_path.with_name(token_path.name + ".lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(os.fspath(lock_path), flags, 0o600)
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
        except OSError:
            raise TokenError(f"Could not open the token lock: {lock_path}") from None

        flock_module = None
        try:
            try:
                import fcntl as flock_module  # type: ignore[import-not-found]
            except ImportError:
                flock_module = None
            if flock_module is not None:
                flock_module.flock(descriptor, flock_module.LOCK_EX)
            yield
        except OSError:
            raise TokenError("Could not lock the OAuth token store") from None
        finally:
            if flock_module is not None:
                try:
                    flock_module.flock(descriptor, flock_module.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _atomic_write_token(token_path: Path, token: Mapping[str, Any]) -> None:
    """Atomically replace a token JSON file with mode 0600."""

    temporary_path: Optional[str] = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{token_path.name}.",
            suffix=".tmp",
            dir=os.fspath(token_path.parent),
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(token, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        os.replace(temporary_path, token_path)
        temporary_path = None
        os.chmod(token_path, 0o600)

        if os.name == "posix":
            try:
                directory_descriptor = os.open(os.fspath(token_path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                # The file itself has already been flushed and atomically
                # replaced. Some filesystems do not permit directory fsync.
                pass
    except (OSError, TypeError, ValueError):
        raise TokenError(f"Could not save the OAuth token: {token_path}") from None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _load_saved_token(token_path: Path, *, username: str) -> Optional[Dict[str, Any]]:
    try:
        payload = _read_json(token_path, description="token", missing_ok=True)
    except YandexMailAuthError as exc:
        raise TokenError(str(exc)) from None
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise TokenError("The token file must contain a JSON object")

    stored_username = payload.get("username")
    if not isinstance(stored_username, str) or stored_username != username:
        raise TokenError("The saved token does not match the configured mailbox")

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise TokenError("The token file has no access token")

    token_type = payload.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise TokenError("The token file has an unsupported token type")

    refresh_token = payload.get("refresh_token")
    if refresh_token is not None and (
        not isinstance(refresh_token, str) or not refresh_token
    ):
        raise TokenError("The token file has an invalid refresh token")

    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool):
        raise TokenError("The token file has an invalid expiry time")
    try:
        parsed_expiry = float(expires_at)
    except (TypeError, ValueError, OverflowError):
        raise TokenError("The token file has an invalid expiry time") from None
    if not math.isfinite(parsed_expiry) or parsed_expiry <= 0:
        raise TokenError("The token file has an invalid expiry time")

    scope = _parse_and_validate_scope(payload.get("scope"))

    validated = dict(payload)
    validated["expires_at"] = parsed_expiry
    validated["scope"] = scope
    return validated


def exchange_authorization_code(
    code_or_callback_url: str,
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
    token_path: PathLike = DEFAULT_TOKEN_PATH,
    *,
    opener: OpenUrl = urlopen,
) -> None:
    """Exchange a manual authorization code and atomically store its token."""

    credentials = load_credentials(credentials_path)
    code = extract_authorization_code(code_or_callback_url)
    destination = _as_path(token_path)

    with _token_lock(destination):
        payload = _post_form(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            opener=opener,
        )
        token = _normalise_token_response(payload, username=credentials.username)
        _atomic_write_token(destination, token)


def get_access_token(
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
    token_path: PathLike = DEFAULT_TOKEN_PATH,
    *,
    opener: OpenUrl = urlopen,
    min_validity_seconds: float = _DEFAULT_MIN_VALIDITY_SECONDS,
) -> Tuple[str, str]:
    """Return ``(username, access_token)``, refreshing once when necessary.

    The lock is held while the token is re-read and refreshed.  A concurrent
    caller therefore observes the newly written token instead of using the
    same refresh token a second time.
    """

    credentials = load_credentials(credentials_path)
    destination = _as_path(token_path)
    try:
        requested_validity = float(min_validity_seconds)
    except (TypeError, ValueError, OverflowError):
        raise TokenError("The minimum token validity must be a number") from None
    if not math.isfinite(requested_validity):
        raise TokenError("The minimum token validity must be finite")
    validity_window = max(0.0, requested_validity)

    with _token_lock(destination):
        token = _load_saved_token(destination, username=credentials.username)
        if token is None:
            raise TokenError("Yandex Mail is not authorized; run the OAuth setup first")

        if token["expires_at"] > time.time() + validity_window:
            return credentials.username, token["access_token"]

        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise TokenError("The OAuth token expired and cannot be refreshed")

        payload = _post_form(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            opener=opener,
        )
        refreshed = _normalise_token_response(
            payload,
            username=credentials.username,
            previous=token,
        )
        _atomic_write_token(destination, refreshed)
        return credentials.username, refreshed["access_token"]


def get_status(
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
    token_path: PathLike = DEFAULT_TOKEN_PATH,
) -> Dict[str, Any]:
    """Return non-secret local authorization status without refreshing."""

    credentials = load_credentials(credentials_path)
    destination = _as_path(token_path)
    with _token_lock(destination):
        token = _load_saved_token(destination, username=credentials.username)

    if token is None:
        return {
            "authorized": False,
            "valid": False,
            "refresh_available": False,
            "scope": None,
            "expires_in_seconds": None,
        }

    remaining = token["expires_at"] - time.time()
    return {
        "authorized": True,
        "valid": remaining > 0,
        "refresh_available": bool(token.get("refresh_token")),
        "scope": token["scope"],
        "expires_in_seconds": max(0, int(remaining)),
    }


def check_live(
    credentials_path: PathLike = DEFAULT_CREDENTIALS_PATH,
    token_path: PathLike = DEFAULT_TOKEN_PATH,
    *,
    opener: OpenUrl = urlopen,
    timeout: float = 20.0,
) -> bool:
    """Authenticate to IMAP and open INBOX read-only without fetching mail."""

    username, access_token = get_access_token(
        credentials_path,
        token_path,
        opener=opener,
    )
    auth_string = (
        f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
    )
    client: Optional[imaplib.IMAP4_SSL] = None
    try:
        client = imaplib.IMAP4_SSL(
            IMAP_HOST,
            IMAP_PORT,
            ssl_context=ssl.create_default_context(),
            timeout=timeout,
        )
        client.authenticate("XOAUTH2", lambda _challenge: auth_string)
        response_type, _ = client.select("INBOX", readonly=True)
        if response_type != "OK":
            raise YandexMailAuthError("Yandex IMAP did not open INBOX read-only")
        return True
    except YandexMailAuthError:
        raise
    except Exception:
        # IMAP exceptions can include server text. Keep output stable and make
        # sure an echoed authentication payload can never escape.
        raise YandexMailAuthError("Yandex IMAP live check failed") from None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


__all__ = [
    "AUTHORIZE_URL",
    "DEFAULT_CREDENTIALS_PATH",
    "DEFAULT_TOKEN_PATH",
    "FORBIDDEN_SCOPES",
    "REDIRECT_URI",
    "REQUIRED_SCOPE",
    "CredentialsError",
    "OAuthRequestError",
    "TokenError",
    "YandexMailAuthError",
    "build_authorization_url",
    "check_live",
    "exchange_authorization_code",
    "extract_authorization_code",
    "get_access_token",
    "get_status",
    "load_credentials",
]
