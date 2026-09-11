"""Ephemeral Telegram location handling for the Korea plugin.

An ingress guard replaces a static Telegram pin with an opaque capability token
before Hermes' busy-session queues or normal gateway dispatch can see it; the
gateway hook is a second cold-path guard. Coordinates live only in this process
and are removed after a successful route consumes the token or its short TTL expires.

There is deliberately no logging in this module. Hook failures are handled with
a coordinate-free rewrite because Hermes hooks otherwise fail open.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


LOCATION_TTL_SECONDS = 10 * 60
MAX_LOCATION_TOKENS = 128
_TOKEN_PATTERN = re.compile(r"^loc_[A-Za-z0-9_-]{24,128}$")
_LATITUDE_PATTERN = re.compile(
    r"(?im)^\s*latitude\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)
_LONGITUDE_PATTERN = re.compile(
    r"(?im)^\s*longitude\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)
_GOOGLE_MAP_COORDINATE_PATTERN = re.compile(
    r"(?i)google\.[^\s/]+/maps/[^\s]*?(?:query=|/)(-?(?:\d+(?:\.\d*)?|\.\d+))"
    r"\s*[,%2C]+\s*(-?(?:\d+(?:\.\d*)?|\.\d+))"
)
_INGRESS_SENTINEL = "korea_location_ingress_sanitized_v1"
_PATCH_MARKER = "__korea_location_ingress_patch_v1__"
_PATCH_STORE = "__korea_location_token_store_v1__"
_SAFE_LOCATION_PREFIXES = (
    "[Static Telegram location accepted as an ephemeral token.",
    "[Live Telegram location is not supported for privacy.",
    "[Edited Telegram location update ignored for privacy.",
    "[The Telegram location could not be processed without exposing coordinates.",
)


class LocationTokenError(Exception):
    """A coordinate-free failure suitable for conversion to a tool error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _LocationEntry:
    latitude: float
    longitude: float
    expires_at: float
    timer: Optional[threading.Timer] = None
    place_search_used: bool = False
    search_context_used: bool = False
    reservation_id: Optional[str] = None
    reservation_purpose: Optional[str] = None


@dataclass(frozen=True, repr=False)
class LocationReservation:
    """Opaque in-process lease; its repr never exposes token or coordinates."""

    token: str
    purpose: str
    reservation_id: str
    _entry: _LocationEntry

    def __repr__(self) -> str:
        return f"LocationReservation(purpose={self.purpose!r})"


def _coordinate(value: Any, *, latitude: bool) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid coordinate")
    result = float(value)
    bound = 90.0 if latitude else 180.0
    if not math.isfinite(result) or not -bound <= result <= bound:
        raise ValueError("invalid coordinate")
    return result


class LocationTokenStore:
    """Thread-safe, process-RAM-only store with active timer expiration."""

    def __init__(
        self,
        *,
        ttl_seconds: int = LOCATION_TTL_SECONDS,
        max_entries: int = MAX_LOCATION_TOKENS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        schedule_expiry: bool = True,
    ) -> None:
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._token_factory = token_factory
        self._schedule_expiry = schedule_expiry
        self._entries: Dict[str, _LocationEntry] = {}
        self._lock = threading.RLock()

    def issue(self, latitude: Any, longitude: Any) -> str:
        lat = _coordinate(latitude, latitude=True)
        lon = _coordinate(longitude, latitude=False)
        with self._lock:
            self._purge_expired_locked()
            while len(self._entries) >= self.max_entries:
                self._evict_earliest_locked()
            token = ""
            for _attempt in range(8):
                token = "loc_" + self._token_factory()
                if _TOKEN_PATTERN.fullmatch(token) and token not in self._entries:
                    break
            else:  # pragma: no cover - cryptographic collisions are implausible
                raise RuntimeError("could not allocate a location token")
            entry = _LocationEntry(
                latitude=lat,
                longitude=lon,
                expires_at=self._clock() + self.ttl_seconds,
            )
            self._entries[token] = entry
            if self._schedule_expiry:
                timer = threading.Timer(self.ttl_seconds, self._expire, args=(token,))
                timer.daemon = True
                entry.timer = timer
                timer.start()
            return token

    def peek(self, token: Any) -> Tuple[float, float]:
        return self._resolve(token, consume=False)

    def consume(self, token: Any) -> Tuple[float, float]:
        return self._resolve(token, consume=True)

    def reserve(
        self,
        token: Any,
        *,
        purpose: str,
    ) -> Tuple[LocationReservation, float, float]:
        """Exclusively lease coordinates for one approved location operation."""
        if purpose not in {"place_search", "search_context", "route"}:
            raise ValueError("unsupported location reservation purpose")
        safe_token = token.strip() if isinstance(token, str) else ""
        if not _TOKEN_PATTERN.fullmatch(safe_token):
            raise LocationTokenError(
                "INVALID_LOCATION_TOKEN",
                "The location token is malformed. Send a new static Telegram pin.",
            )
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(safe_token)
            if entry is None:
                raise LocationTokenError(
                    "LOCATION_TOKEN_EXPIRED",
                    "The location token is expired, already consumed, or unknown. "
                    "Send a new static Telegram pin.",
                )
            if entry.reservation_id is not None:
                raise LocationTokenError(
                    "LOCATION_TOKEN_IN_USE",
                    "The location token is already in use by another request.",
                )
            if purpose == "place_search" and entry.place_search_used:
                raise LocationTokenError(
                    "LOCATION_TOKEN_PLACE_SEARCH_USED",
                    "This location token has already been used for a place search. "
                    "It remains available for any unused coarse search context "
                    "and for one route.",
                )
            if purpose == "search_context" and entry.search_context_used:
                raise LocationTokenError(
                    "LOCATION_TOKEN_SEARCH_CONTEXT_USED",
                    "This location token has already produced a general web-search "
                    "context. Reuse that coarse locality or send a new static "
                    "Telegram pin.",
                )
            reservation_id = secrets.token_urlsafe(24)
            entry.reservation_id = reservation_id
            entry.reservation_purpose = purpose
            reservation = LocationReservation(
                token=safe_token,
                purpose=purpose,
                reservation_id=reservation_id,
                _entry=entry,
            )
            return reservation, entry.latitude, entry.longitude

    def commit(self, reservation: LocationReservation) -> bool:
        """Commit a matching lease: mark search used or consume a route token."""
        if not isinstance(reservation, LocationReservation):
            return False
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(reservation.token)
            if (
                entry is None
                or entry is not reservation._entry
                or entry.reservation_id != reservation.reservation_id
                or entry.reservation_purpose != reservation.purpose
            ):
                return False
            if reservation.purpose == "route":
                self._entries.pop(reservation.token, None)
                self._zero_entry_locked(entry)
                return True
            if reservation.purpose == "place_search":
                entry.place_search_used = True
            elif reservation.purpose == "search_context":
                entry.search_context_used = True
            entry.reservation_id = None
            entry.reservation_purpose = None
            return True

    def release(self, reservation: LocationReservation) -> bool:
        """Release only the exact active lease, leaving coordinates in RAM."""
        if not isinstance(reservation, LocationReservation):
            return False
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(reservation.token)
            if (
                entry is None
                or entry is not reservation._entry
                or entry.reservation_id != reservation.reservation_id
                or entry.reservation_purpose != reservation.purpose
            ):
                return False
            entry.reservation_id = None
            entry.reservation_purpose = None
            return True

    def revoke(self, token: Any) -> bool:
        """Remove and zero a token without exposing its coordinates."""
        safe_token = token.strip() if isinstance(token, str) else ""
        if not _TOKEN_PATTERN.fullmatch(safe_token):
            return False
        with self._lock:
            entry = self._entries.pop(safe_token, None)
            if entry is None:
                return False
            self._zero_entry_locked(entry)
            return True

    def _resolve(self, token: Any, *, consume: bool) -> Tuple[float, float]:
        safe_token = token.strip() if isinstance(token, str) else ""
        if not _TOKEN_PATTERN.fullmatch(safe_token):
            raise LocationTokenError(
                "INVALID_LOCATION_TOKEN",
                "The location token is malformed. Send a new static Telegram pin.",
            )
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(safe_token)
            if entry is None:
                raise LocationTokenError(
                    "LOCATION_TOKEN_EXPIRED",
                    "The location token is expired, already consumed, or unknown. "
                    "Send a new static Telegram pin.",
                )
            if entry.reservation_id is not None:
                raise LocationTokenError(
                    "LOCATION_TOKEN_IN_USE",
                    "The location token is already in use by another request.",
                )
            coordinates = (entry.latitude, entry.longitude)
            if consume:
                self._entries.pop(safe_token, None)
                self._zero_entry_locked(entry)
            return coordinates

    def _expire(self, token: str) -> None:
        with self._lock:
            entry = self._entries.pop(token, None)
            if entry is not None:
                self._zero_entry_locked(entry, cancel_timer=False)

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            token for token, entry in self._entries.items() if entry.expires_at <= now
        ]
        for token in expired:
            entry = self._entries.pop(token)
            self._zero_entry_locked(entry)

    def _evict_earliest_locked(self) -> None:
        candidates = [
            token
            for token, entry in self._entries.items()
            if entry.reservation_id is None
        ]
        if not candidates:
            raise RuntimeError("all location token slots are in use")
        token = min(candidates, key=lambda item: self._entries[item].expires_at)
        entry = self._entries.pop(token)
        self._zero_entry_locked(entry)

    @staticmethod
    def _zero_entry_locked(
        entry: _LocationEntry,
        *,
        cancel_timer: bool = True,
    ) -> None:
        entry.latitude = 0.0
        entry.longitude = 0.0
        entry.reservation_id = None
        entry.reservation_purpose = None
        if cancel_timer and entry.timer is not None:
            entry.timer.cancel()

    def clear(self) -> None:
        """Best-effort zeroing helper used by tests and process shutdown code."""
        with self._lock:
            for entry in self._entries.values():
                self._zero_entry_locked(entry)
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._entries)


LOCATION_TOKENS = LocationTokenStore()
_LIVE_EVENT_EXPIRY: Dict[str, float] = {}
_LIVE_EVENT_LOCK = threading.Lock()


def _read(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _is_telegram_location(event: Any) -> bool:
    source = _read(event, "source")
    raw_platform = _read(source, "platform")
    platform = str(getattr(raw_platform, "value", raw_platform) or "").lower()
    if platform != "telegram" and not platform.endswith(".telegram"):
        return False
    message_type = _read(event, "message_type")
    value = str(getattr(message_type, "value", message_type) or "").lower()
    return value == "location" or value.endswith(".location")


def _raw_location(event: Any) -> Any:
    raw_message = _read(event, "raw_message")
    venue = _read(raw_message, "venue")
    return _read(venue, "location") or _read(raw_message, "location")


def _extract_coordinates(event: Any) -> Tuple[float, float]:
    location = _raw_location(event)
    if location is not None:
        latitude = _read(location, "latitude")
        longitude = _read(location, "longitude")
        if latitude is not None and longitude is not None:
            return (
                _coordinate(latitude, latitude=True),
                _coordinate(longitude, latitude=False),
            )

    text = str(_read(event, "text") or "")
    latitude_match = _LATITUDE_PATTERN.search(text)
    longitude_match = _LONGITUDE_PATTERN.search(text)
    if latitude_match and longitude_match:
        return (
            _coordinate(latitude_match.group(1), latitude=True),
            _coordinate(longitude_match.group(1), latitude=False),
        )
    url_match = _GOOGLE_MAP_COORDINATE_PATTERN.search(text)
    if url_match:
        return (
            _coordinate(url_match.group(1), latitude=True),
            _coordinate(url_match.group(2), latitude=False),
        )
    raise ValueError("location coordinates are unavailable")


def _is_live_location(event: Any) -> bool:
    live_period = _read(_raw_location(event), "live_period")
    try:
        return live_period is not None and int(live_period) > 0
    except (TypeError, ValueError):
        return live_period is not None


def _repeated_live_event(event: Any) -> bool:
    message_id = _read(event, "message_id")
    if message_id in (None, ""):
        return False
    source = _read(event, "source")
    identity = "|".join(
        str(_read(source, field) or "")
        for field in ("platform", "user_id", "chat_id", "thread_id")
    )
    key = f"{identity}|{message_id}"
    now = time.monotonic()
    with _LIVE_EVENT_LOCK:
        expired = [item for item, expiry in _LIVE_EVENT_EXPIRY.items() if expiry <= now]
        for item in expired:
            _LIVE_EVENT_EXPIRY.pop(item, None)
        if key in _LIVE_EVENT_EXPIRY:
            return True
        if len(_LIVE_EVENT_EXPIRY) >= MAX_LOCATION_TOKENS:
            oldest = min(_LIVE_EVENT_EXPIRY, key=_LIVE_EVENT_EXPIRY.get)
            _LIVE_EVENT_EXPIRY.pop(oldest, None)
        _LIVE_EVENT_EXPIRY[key] = now + LOCATION_TTL_SECONDS
        return False


def _scrub_event(event: Any, replacement: str) -> bool:
    """Remove coordinate-bearing objects and verify the mutable boundary."""
    if isinstance(event, dict):
        try:
            event["text"] = replacement
            event["raw_message"] = None
        except Exception:  # noqa: BLE001 - caller will drop the event
            return False
        return event.get("text") == replacement and event.get("raw_message") is None
    try:
        event.text = replacement
    except Exception:  # noqa: BLE001 - the returned rewrite remains authoritative
        pass
    try:
        event.raw_message = None
    except Exception:  # noqa: BLE001 - some event implementations may be immutable
        pass
    return (
        str(_read(event, "text") or "") == replacement
        and _read(event, "raw_message") is None
    )


def _metadata(event: Any) -> Any:
    return _read(event, "metadata")


def _has_ingress_sentinel(event: Any) -> bool:
    metadata = _metadata(event)
    return isinstance(metadata, Mapping) and metadata.get(_INGRESS_SENTINEL) is True


def _mark_ingress_sentinel(event: Any) -> bool:
    metadata = _metadata(event)
    if isinstance(metadata, dict):
        metadata[_INGRESS_SENTINEL] = True
        return True
    replacement = dict(metadata) if isinstance(metadata, Mapping) else {}
    replacement[_INGRESS_SENTINEL] = True
    try:
        if isinstance(event, dict):
            event["metadata"] = replacement
        else:
            event.metadata = replacement
        return True
    except Exception:  # noqa: BLE001 - safe-text detection is the fallback
        return False


def _already_sanitized(event: Any) -> bool:
    if _has_ingress_sentinel(event):
        return True
    if _read(event, "raw_message") is not None:
        return False
    text = str(_read(event, "text") or "")
    return text.startswith(_SAFE_LOCATION_PREFIXES)


def sanitize_telegram_location(
    event: Any,
    store: LocationTokenStore = LOCATION_TOKENS,
) -> Optional[Dict[str, str]]:
    """Sanitize one Telegram LOCATION event without logging or returning coords."""
    try:
        if not _is_telegram_location(event):
            return None
        if _already_sanitized(event):
            return None
        raw_message = _read(event, "raw_message")
        if _read(raw_message, "edit_date") is not None:
            replacement = (
                "[Edited Telegram location update ignored for privacy. Ask the "
                "user to send one new static location pin.]"
            )
            _scrub_event(event, replacement)
            return {
                "action": "skip",
                "reason": "edited_location_update",
            }
        if _is_live_location(event):
            repeated_edit = _repeated_live_event(event)
            replacement = (
                "[Live Telegram location is not supported for privacy. Ask the "
                "user to stop live sharing and send one static location pin.]"
            )
            scrubbed = _scrub_event(event, replacement)
            if not scrubbed:
                return {
                    "action": "skip",
                    "reason": "location_event_could_not_be_scrubbed",
                }
            if repeated_edit:
                return {
                    "action": "skip",
                    "reason": "repeated_live_location_update",
                }
            return {"action": "rewrite", "text": replacement}
        latitude, longitude = _extract_coordinates(event)
        token = store.issue(latitude, longitude)
        replacement = (
            "[Static Telegram location accepted as an ephemeral token. "
            f"location_token: {token}. Use it with location_search_context for "
            "Google or general web search, with korea_place_search for nearby "
            "places, or as origin_location_token with korea_route. The route "
            "consumes it; "
            f"otherwise it expires in {store.ttl_seconds} seconds. Never copy "
            "the original coordinates into memory or the response.]"
        )
        if not _scrub_event(event, replacement):
            store.revoke(token)
            return {
                "action": "skip",
                "reason": "location_event_could_not_be_scrubbed",
            }
        return {"action": "rewrite", "text": replacement}
    except Exception:  # noqa: BLE001 - location handling must fail coordinate-closed
        replacement = (
            "[The Telegram location could not be processed without exposing "
            "coordinates. Ask the user to send a new static location pin.]"
        )
        if not _scrub_event(event, replacement):
            return {
                "action": "skip",
                "reason": "location_event_could_not_be_scrubbed",
            }
        return {"action": "rewrite", "text": replacement}


def make_pre_gateway_dispatch_hook(
    store: LocationTokenStore = LOCATION_TOKENS,
) -> Callable[..., Optional[Dict[str, str]]]:
    """Create the Hermes ``pre_gateway_dispatch`` privacy callback."""

    def pre_gateway_dispatch(event: Any, **_kwargs: Any) -> Optional[Dict[str, str]]:
        return sanitize_telegram_location(event, store)

    return pre_gateway_dispatch


def install_location_ingress_guard(
    store: LocationTokenStore = LOCATION_TOKENS,
    *,
    base_class: Any = None,
) -> bool:
    """Patch the busy-session ingress before any pending-message persistence."""
    if base_class is None:
        try:
            module = importlib.import_module("gateway.platforms.base")
            base_class = getattr(module, "BasePlatformAdapter")
        except (ImportError, AttributeError):
            return False
    current = getattr(base_class, "handle_message", None)
    if not inspect.iscoroutinefunction(current):
        return False
    setattr(base_class, _PATCH_STORE, store)
    if getattr(current, _PATCH_MARKER, False):
        return True

    @functools.wraps(current)
    async def protected_handle_message(
        self: Any,
        event: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        active_store = getattr(base_class, _PATCH_STORE, store)
        decision = sanitize_telegram_location(event, active_store)
        if decision is not None:
            if decision.get("action") == "skip":
                return None
            _mark_ingress_sentinel(event)
        return await current(self, event, *args, **kwargs)

    setattr(protected_handle_message, _PATCH_MARKER, True)
    setattr(base_class, "handle_message", protected_handle_message)
    return True


def is_location_ingress_guard_installed(*, base_class: Any = None) -> bool:
    """Return whether the process-lifetime ingress guard is active."""
    if base_class is None:
        try:
            module = importlib.import_module("gateway.platforms.base")
            base_class = getattr(module, "BasePlatformAdapter")
        except (ImportError, AttributeError):
            return False
    current = getattr(base_class, "handle_message", None)
    return bool(
        inspect.iscoroutinefunction(current)
        and getattr(current, _PATCH_MARKER, False)
        and getattr(base_class, _PATCH_STORE, None) is not None
    )


PRE_GATEWAY_DISPATCH = make_pre_gateway_dispatch_hook()
