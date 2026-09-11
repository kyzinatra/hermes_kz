"""Small, stateless clients for official Kakao Korea APIs.

Secrets are read only from the process environment.  In the Hermes deployment
that environment is populated from ``/opt/data/.env``.  This module never reads,
writes, caches, or logs a location or credential.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


KAKAO_LOCAL_BASE = "https://dapi.kakao.com"
KAKAO_MOBILITY_DIRECTIONS = (
    "https://apis-navi.kakaomobility.com/v1/directions"
)
KAKAO_T_LAUNCH_URL = "https://service.kakaomobility.com/launch/kakaot/"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 4_000_000
MAX_REQUEST_ATTEMPTS = 3
INITIAL_RETRY_DELAY_SECONDS = 0.25
MAX_RETRY_DELAY_SECONDS = 5.0
_RETRYABLE_HTTP_STATUS_CODES = {408, 429}


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of forwarding Kakao credentials to a redirect."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirectHandler())


def _open_without_redirects(request: Request, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _retry_after_seconds(headers: Any) -> Optional[float]:
    """Return a bounded Retry-After delay without surfacing header contents."""
    try:
        raw_value = headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay):
        return None
    return min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS)


def _is_retryable_http_status(status_code: Any) -> bool:
    return (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and (
            status_code in _RETRYABLE_HTTP_STATUS_CODES
            or 500 <= status_code <= 599
        )
    )


PRIVACY_NOTICE = (
    "This plugin never writes or intentionally logs coordinates. Its registered "
    "Telegram pre-dispatch hook replaces static-pin coordinates with a RAM-only "
    "10-minute token before Hermes session dispatch; a successful route consumes "
    "that token. "
    "Telegram itself still receives the original pin."
)
PLACE_DATA_LIMITATION = (
    "Kakao Local Search does not provide opening hours, open-now state, prices, "
    "review counts, or inventory. Inspect the returned Kakao place page separately "
    "when the user needs current details. Exact origin-to-place distances are "
    "reduced to coarse bands and proximity rank for location privacy."
)
SHOPPING_LIMITATION = (
    "This is a zero-API browser handoff. Browser catalog results and prices do not "
    "verify physical-store shelf inventory."
)
KAKAO_T_LIMITATION = (
    "Kakao Mobility documents an official Kakao T app launcher, but no public URL "
    "that pre-fills a taxi destination."
)

CATEGORY_CODES = {
    "supermarket": "MT1",
    "convenience_store": "CS2",
    "parking": "PK6",
    "fuel_or_charging": "OL7",
    "subway_station": "SW8",
    "bank": "BK9",
    "culture": "CT1",
    "real_estate": "AG2",
    "public_institution": "PO3",
    "attraction": "AT4",
    "accommodation": "AD5",
    "restaurant": "FD6",
    "cafe": "CE7",
    "hospital": "HP8",
    "pharmacy": "PM9",
}


class KoreaApiError(Exception):
    """A user-safe typed failure; it never contains response bodies or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str,
        status_code: Optional[int] = None,
        provider_error_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.provider_error_code = provider_error_code

    def as_result(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status_code is not None:
            error["status_code"] = self.status_code
        if self.provider_error_code is not None:
            error["provider_error_code"] = self.provider_error_code
        return {
            "success": False,
            "meta": {
                "provider": self.provider,
                "provider_used": self.provider,
                "location_retention": "no_plugin_file_persistence",
            },
            "privacy_notice": PRIVACY_NOTICE,
            "error": error,
        }


def _success(
    provider: str,
    data: Any,
    *,
    limitations: Optional[Iterable[str]] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    result_meta: Dict[str, Any] = {
        "provider": provider,
        "provider_used": provider,
        "location_retention": "no_plugin_file_persistence",
    }
    if meta:
        result_meta.update(meta)
    result: Dict[str, Any] = {
        "success": True,
        "meta": result_meta,
        "privacy_notice": PRIVACY_NOTICE,
        "data": data,
    }
    if limitations:
        result["limitations"] = list(limitations)
    return result


def _text(value: Any, *, field: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must be a non-empty string.",
            provider="validation",
        )
    normalized = value.strip()
    if len(normalized) > maximum:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must not exceed {maximum} characters.",
            provider="validation",
        )
    return normalized


def _optional_text(value: Any, *, field: str, maximum: int = 100) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must be a string.",
            provider="validation",
        )
    normalized = value.strip()
    if len(normalized) > maximum:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must not exceed {maximum} characters.",
            provider="validation",
        )
    return normalized


def _bounded_int(
    value: Any,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        value = None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must be an integer.",
            provider="validation",
        ) from exc
    if normalized < minimum or normalized > maximum:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must be between {minimum} and {maximum}.",
            provider="validation",
        )
    return normalized


def _coordinate(value: Any, *, field: str, latitude: bool) -> float:
    if isinstance(value, bool):
        value = None
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} must be a WGS84 coordinate.",
            provider="validation",
        ) from exc
    bound = 90.0 if latitude else 180.0
    if not math.isfinite(normalized) or not -bound <= normalized <= bound:
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{field} is outside the valid WGS84 range.",
            provider="validation",
        )
    return normalized


def _coordinate_pair(
    latitude: Any,
    longitude: Any,
    *,
    prefix: str = "",
) -> Tuple[float, float]:
    label = f"{prefix}_" if prefix else ""
    return (
        _coordinate(latitude, field=f"{label}latitude", latitude=True),
        _coordinate(longitude, field=f"{label}longitude", latitude=False),
    )


def _optional_coordinate_pair(
    latitude: Any,
    longitude: Any,
    *,
    prefix: str = "",
) -> Optional[Tuple[float, float]]:
    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        label = f"{prefix} " if prefix else ""
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"{label}latitude and longitude must be supplied together.",
            provider="validation",
        )
    return _coordinate_pair(latitude, longitude, prefix=prefix)


def _format_coordinate(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _integer_or_none(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coarse_distance_band(distance_m: Optional[int]) -> Optional[str]:
    if distance_m is None or distance_m < 0:
        return None
    if distance_m < 500:
        return "very_near_under_500m"
    if distance_m < 1500:
        return "nearby_500m_to_1_5km"
    if distance_m < 3000:
        return "moderate_1_5km_to_3km"
    if distance_m < 7000:
        return "far_3km_to_7km"
    return "very_far_over_7km"


def _float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _require_mapping(value: Any, *, provider: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise KoreaApiError(
            "INVALID_RESPONSE",
            "The provider returned an unexpected JSON structure.",
            provider=provider,
        )
    return value


def _required_document_list(
    value: Any,
    *,
    provider: str,
) -> List[Dict[str, Any]]:
    """Require Kakao Local's top-level ``documents`` response contract.

    A valid empty result is ``[]``. Missing/non-list values or malformed
    elements are provider errors, not successful zero-result searches; silently
    normalizing them would also consume a one-shot Telegram location search.
    """
    if not isinstance(value, list) or any(
        not isinstance(item, dict) or not item for item in value
    ):
        raise KoreaApiError(
            "INVALID_RESPONSE",
            f"{provider} returned an invalid documents field.",
            provider=provider,
        )
    return value


def _list_of_mappings(value: Any) -> List[Dict[str, Any]]:
    """Best-effort normalization for optional nested provider fields."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _invalid_route_response(provider: str, field: str) -> None:
    raise KoreaApiError(
        "INVALID_RESPONSE",
        f"{provider} returned an invalid {field} route field.",
        provider=provider,
    )


def _required_route_mapping(
    value: Any,
    *,
    provider: str,
    field: str,
) -> Dict[str, Any]:
    if not isinstance(value, dict) or not value:
        _invalid_route_response(provider, field)
    return value


def _required_route_list(
    value: Any,
    *,
    provider: str,
    field: str,
    allow_empty: bool = False,
) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _invalid_route_response(provider, field)
    if not value and not allow_empty:
        raise KoreaApiError(
            "NO_ROUTE",
            f"{provider} returned no routes.",
            provider=provider,
        )
    return value


def _required_nonnegative_int(
    value: Any,
    *,
    provider: str,
    field: str,
) -> int:
    parsed = _integer_or_none(value)
    if parsed is None or parsed < 0:
        _invalid_route_response(provider, field)
    return parsed


def _route_status_failure(payload: Dict[str, Any], *, provider: str) -> None:
    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        _invalid_route_response(provider, "status")
    status = raw_status.strip()
    if status != "OK":
        raise KoreaApiError(
            "NO_ROUTE",
            f"Kakao route search returned status {status}.",
            provider=provider,
        )


def _link_segment(name: str, latitude: float, longitude: float) -> str:
    safe_name = quote(name or "위치", safe="")
    return (
        f"{safe_name},{_format_coordinate(latitude)},"
        f"{_format_coordinate(longitude)}"
    )


def build_kakao_links(
    destination_name: str,
    destination_latitude: float,
    destination_longitude: float,
    *,
    place_id: str = "",
) -> Dict[str, Any]:
    """Build destination-only KakaoMap URLs and the official Kakao T launcher."""
    destination = _link_segment(
        destination_name,
        destination_latitude,
        destination_longitude,
    )
    map_target = quote(place_id, safe="") if place_id else destination
    links: Dict[str, Any] = {
        "kakao_map": {
            "map": f"https://map.kakao.com/link/map/{map_target}",
            "directions_from_current_location": (
                f"https://map.kakao.com/link/to/{destination}"
            ),
        },
        "kakao_t": {
            "app_launch": KAKAO_T_LAUNCH_URL,
            "destination_prefilled": False,
            "limitation": KAKAO_T_LIMITATION,
        },
    }
    return links


class KoreaApiClient:
    """Dependency-injectable, stateless API facade."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = _open_without_redirects,
        env_lookup: Callable[[str], Optional[str]] = os.getenv,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not callable(opener) or not callable(env_lookup) or not callable(sleeper):
            raise ValueError("opener, env_lookup, and sleeper must be callable")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self._opener = opener
        self._env_lookup = env_lookup
        self._timeout = float(timeout)
        self._sleeper = sleeper

    def _secret(self, name: str, *, provider: str) -> str:
        value = self._env_lookup(name)
        normalized = value.strip() if isinstance(value, str) else ""
        if not normalized:
            raise KoreaApiError(
                "MISSING_CREDENTIAL",
                (
                    f"{name} is not configured in the process environment "
                    "sourced from /opt/data/.env."
                ),
                provider=provider,
            )
        return normalized

    def _request_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        provider: str,
    ) -> Dict[str, Any]:
        query = urlencode(
            [(key, value) for key, value in params.items() if value is not None],
            doseq=True,
        )
        target = f"{url}?{query}" if query else url
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "hermes-korea-plugin/1.0",
            **headers,
        }
        raw = b""
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            request = Request(target, headers=request_headers, method="GET")
            try:
                response = self._opener(request, timeout=self._timeout)
                with response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                break
            except HTTPError as exc:
                if (
                    attempt + 1 < MAX_REQUEST_ATTEMPTS
                    and _is_retryable_http_status(exc.code)
                ):
                    retry_after = _retry_after_seconds(exc.headers)
                    delay = min(
                        INITIAL_RETRY_DELAY_SECONDS * (2**attempt),
                        MAX_RETRY_DELAY_SECONDS,
                    )
                    if retry_after is not None:
                        delay = max(delay, retry_after)
                    try:
                        exc.close()
                    except (AttributeError, OSError):
                        pass
                    self._sleeper(delay)
                    continue

                provider_error_code: Optional[int] = None
                try:
                    error_raw = exc.read(16_385)
                    if len(error_raw) <= 16_384:
                        error_payload = json.loads(error_raw.decode("utf-8"))
                        if isinstance(error_payload, dict):
                            raw_code = error_payload.get("code")
                            if isinstance(raw_code, int) and not isinstance(
                                raw_code,
                                bool,
                            ):
                                provider_error_code = raw_code
                except (
                    AttributeError,
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ):
                    pass
                finally:
                    try:
                        exc.close()
                    except (AttributeError, OSError):
                        pass
                message = f"{provider} returned HTTP {exc.code}."
                if exc.code == 403 and provider in {
                    "kakao_local",
                    "kakao_geocoding",
                    "kakao_map_walking",
                    "kakao_map_transit",
                }:
                    if provider_error_code == -5:
                        message = (
                            "Kakao Map returned HTTP 403 because this app lacks the "
                            "required API permission."
                        )
                    else:
                        message = (
                            "Kakao Map returned HTTP 403. Enable Kakao Map in the app's "
                            "Usage settings and configure the REST API key for Kakao Map."
                        )
                raise KoreaApiError(
                    "PROVIDER_HTTP_ERROR",
                    message,
                    provider=provider,
                    status_code=exc.code,
                    provider_error_code=provider_error_code,
                ) from exc
            except (URLError, socket.timeout, TimeoutError, OSError) as exc:
                if attempt + 1 < MAX_REQUEST_ATTEMPTS:
                    delay = min(
                        INITIAL_RETRY_DELAY_SECONDS * (2**attempt),
                        MAX_RETRY_DELAY_SECONDS,
                    )
                    self._sleeper(delay)
                    continue
                raise KoreaApiError(
                    "PROVIDER_UNAVAILABLE",
                    f"{provider} could not be reached.",
                    provider=provider,
                ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise KoreaApiError(
                "RESPONSE_TOO_LARGE",
                f"{provider} returned a response larger than the safety limit.",
                provider=provider,
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KoreaApiError(
                "INVALID_RESPONSE",
                f"{provider} returned invalid JSON.",
                provider=provider,
            ) from exc
        return _require_mapping(decoded, provider=provider)

    def _kakao_get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any],
        provider: str,
    ) -> Dict[str, Any]:
        key = self._secret("KAKAO_REST_API_KEY", provider=provider)
        url = (
            path_or_url
            if path_or_url.startswith("https://")
            else f"{KAKAO_LOCAL_BASE}{path_or_url}"
        )
        return self._request_json(
            url,
            params=params,
            headers={"Authorization": f"KakaoAK {key}"},
            provider=provider,
        )

    def place_search(
        self,
        *,
        query: Any = None,
        category: Any = None,
        latitude: Any = None,
        longitude: Any = None,
        radius_m: Any = 2000,
        sort: Any = "distance",
        limit: Any = 10,
        page: Any = 1,
    ) -> Dict[str, Any]:
        query_text = ""
        if query is not None:
            query_text = _text(query, field="query", maximum=200)
        category_text = ""
        if category is not None:
            category_text = _text(category, field="category", maximum=50).lower()
            if category_text not in CATEGORY_CODES:
                raise KoreaApiError(
                    "INVALID_ARGUMENT",
                    "category is not a supported Kakao category alias.",
                    provider="validation",
                )
        if not query_text and not category_text:
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                "Provide query, category, or both.",
                provider="validation",
            )
        center = _optional_coordinate_pair(latitude, longitude)
        if not query_text and center is None:
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                "A category-only search requires latitude and longitude.",
                provider="validation",
            )
        safe_sort = str(sort or "accuracy").lower()
        if safe_sort not in {"accuracy", "distance"}:
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                "sort must be accuracy or distance.",
                provider="validation",
            )
        if safe_sort == "distance" and center is None:
            safe_sort = "accuracy"
        safe_limit = _bounded_int(
            limit, field="limit", default=10, minimum=1, maximum=15
        )
        safe_page = _bounded_int(page, field="page", default=1, minimum=1, maximum=45)
        params: Dict[str, Any] = {
            "size": safe_limit,
            "page": safe_page,
            "sort": safe_sort,
        }
        if center is not None:
            center_lat, center_lon = center
            params.update(
                {
                    "x": _format_coordinate(center_lon),
                    "y": _format_coordinate(center_lat),
                    "radius": _bounded_int(
                        radius_m,
                        field="radius_m",
                        default=2000,
                        minimum=1,
                        maximum=20000,
                    ),
                }
            )
        if query_text:
            path = "/v2/local/search/keyword.json"
            params["query"] = query_text
            if category_text:
                params["category_group_code"] = CATEGORY_CODES[category_text]
            search_type = "keyword"
        else:
            path = "/v2/local/search/category.json"
            params["category_group_code"] = CATEGORY_CODES[category_text]
            search_type = "category"

        payload = self._kakao_get(params=params, path_or_url=path, provider="kakao_local")
        documents = _required_document_list(
            payload.get("documents"),
            provider="kakao_local",
        )
        raw_distances = [
            _integer_or_none(document.get("distance")) for document in documents
        ]
        ranked = sorted(
            (
                (distance, index)
                for index, distance in enumerate(raw_distances)
                if distance is not None and distance >= 0
            ),
            key=lambda item: (item[0], item[1]),
        )
        proximity_ranks = {
            index: rank for rank, (_distance, index) in enumerate(ranked, start=1)
        }
        places: List[Dict[str, Any]] = []
        for index, document in enumerate(documents):
            place_lat = _float_or_none(document.get("y"))
            place_lon = _float_or_none(document.get("x"))
            name = str(document.get("place_name", ""))
            place_id = str(document.get("id", ""))
            item: Dict[str, Any] = {
                "id": place_id,
                "name_ko": name,
                "category": str(document.get("category_name", "")),
                "category_group_code": str(document.get("category_group_code", "")),
                "phone": str(document.get("phone", "")),
                "address": str(document.get("address_name", "")),
                "road_address": str(document.get("road_address_name", "")),
                "latitude": place_lat,
                "longitude": place_lon,
                "distance_band": _coarse_distance_band(raw_distances[index]),
                "proximity_rank": proximity_ranks.get(index),
                "place_url": str(document.get("place_url", "")),
                "live_details": {
                    "hours": None,
                    "open_now": None,
                    "price_range": None,
                    "review_count": None,
                    "inventory": None,
                },
            }
            if place_lat is not None and place_lon is not None:
                item["links"] = build_kakao_links(
                    name or "위치",
                    place_lat,
                    place_lon,
                    place_id=place_id if place_id.isdigit() else "",
                )["kakao_map"]
            places.append(item)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        return _success(
            "kakao_local",
            {
                "search_type": search_type,
                "places": places,
                "distance_privacy": (
                    "Exact origin-to-place distances are withheld. Bands are coarse; "
                    "proximity_rank is relative only within these returned results."
                    if center is not None
                    else "No search center was supplied."
                ),
                "pagination": {
                    "total_count": _integer_or_none(meta.get("total_count")),
                    "pageable_count": _integer_or_none(meta.get("pageable_count")),
                    "is_end": bool(meta.get("is_end", True)),
                    "page": safe_page,
                },
            },
            limitations=[PLACE_DATA_LIMITATION],
            meta={"endpoint": path},
        )

    def geocode(
        self,
        address: Any,
        *,
        exact: Any = False,
        limit: Any = 5,
    ) -> Dict[str, Any]:
        address_text = _text(address, field="address", maximum=300)
        safe_limit = _bounded_int(limit, field="limit", default=5, minimum=1, maximum=30)
        payload = self._kakao_get(
            "/v2/local/search/address.json",
            params={
                "query": address_text,
                "analyze_type": "exact" if exact is True else "similar",
                "size": safe_limit,
            },
            provider="kakao_geocoding",
        )
        results: List[Dict[str, Any]] = []
        for document in _required_document_list(
            payload.get("documents"),
            provider="kakao_geocoding",
        ):
            road = document.get("road_address")
            parcel = document.get("address")
            results.append(
                {
                    "matched_address": str(document.get("address_name", "")),
                    "match_type": str(document.get("address_type", "")),
                    "latitude": _float_or_none(document.get("y")),
                    "longitude": _float_or_none(document.get("x")),
                    "road_address": (
                        str(road.get("address_name", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                    "parcel_address": (
                        str(parcel.get("address_name", ""))
                        if isinstance(parcel, dict)
                        else None
                    ),
                    "building_name": (
                        str(road.get("building_name", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                    "postal_code": (
                        str(road.get("zone_no", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                }
            )
        return _success(
            "kakao_geocoding",
            {"results": results, "count": len(results)},
            meta={"endpoint": "/v2/local/search/address.json"},
        )

    def reverse_geocode(self, latitude: Any, longitude: Any) -> Dict[str, Any]:
        lat, lon = _coordinate_pair(latitude, longitude)
        payload = self._kakao_get(
            "/v2/local/geo/coord2address.json",
            params={
                "x": _format_coordinate(lon),
                "y": _format_coordinate(lat),
                "input_coord": "WGS84",
            },
            provider="kakao_geocoding",
        )
        results: List[Dict[str, Any]] = []
        for document in _required_document_list(
            payload.get("documents"),
            provider="kakao_geocoding",
        ):
            road = document.get("road_address")
            parcel = document.get("address")
            results.append(
                {
                    "road_address": (
                        str(road.get("address_name", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                    "parcel_address": (
                        str(parcel.get("address_name", ""))
                        if isinstance(parcel, dict)
                        else None
                    ),
                    "building_name": (
                        str(road.get("building_name", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                    "postal_code": (
                        str(road.get("zone_no", ""))
                        if isinstance(road, dict)
                        else None
                    ),
                }
            )
        return _success(
            "kakao_geocoding",
            {"results": results, "count": len(results)},
            meta={"endpoint": "/v2/local/geo/coord2address.json"},
        )

    def location_search_context(
        self,
        latitude: Any,
        longitude: Any,
        query: Any = "",
    ) -> Dict[str, Any]:
        """Return a coarse locality suitable for provider-neutral web search.

        Unlike ``reverse_geocode``, this deliberately discards the road, parcel,
        building, postal code, and coordinate fields returned by Kakao.  The
        caller can therefore localize a Google/DDGS/Tavily query without putting
        the user's exact Telegram pin into model-visible history.
        """
        lat, lon = _coordinate_pair(latitude, longitude)
        query_text = ""
        if query not in (None, ""):
            query_text = _text(query, field="query", maximum=300)
        payload = self._kakao_get(
            "/v2/local/geo/coord2address.json",
            params={
                "x": _format_coordinate(lon),
                "y": _format_coordinate(lat),
                "input_coord": "WGS84",
            },
            provider="kakao_geocoding",
        )
        documents = _required_document_list(
            payload.get("documents"),
            provider="kakao_geocoding",
        )
        if not documents:
            raise KoreaApiError(
                "LOCATION_CONTEXT_NOT_FOUND",
                "No administrative locality was found for this location.",
                provider="kakao_geocoding",
            )

        document = documents[0]
        parcel = document.get("address")
        road = document.get("road_address")
        source = parcel if isinstance(parcel, dict) and parcel else road
        if not isinstance(source, dict):
            raise KoreaApiError(
                "INVALID_RESPONSE",
                "Kakao geocoding returned no usable administrative locality.",
                provider="kakao_geocoding",
            )

        region_1 = str(source.get("region_1depth_name", "")).strip()
        region_2 = str(source.get("region_2depth_name", "")).strip()
        region_3 = str(
            source.get("region_3depth_h_name")
            or source.get("region_3depth_name")
            or ""
        ).strip()
        locality_parts = list(
            dict.fromkeys(
                part for part in (region_1, region_2, region_3) if part
            )
        )
        if not locality_parts:
            raise KoreaApiError(
                "INVALID_RESPONSE",
                "Kakao geocoding returned an empty administrative locality.",
                provider="kakao_geocoding",
            )
        locality = " ".join(locality_parts)
        localized_query = " ".join(part for part in (query_text, locality) if part)
        return _success(
            "kakao_geocoding",
            {
                "locality": locality,
                "region_1depth_name": region_1 or None,
                "region_2depth_name": region_2 or None,
                "region_3depth_name": region_3 or None,
                "precision": "administrative_neighborhood",
                "localized_query": localized_query,
                "instruction": (
                    "Use localized_query with web_search or a Google/browser "
                    "search. Treat it as an area hint, not an exact-distance or "
                    "nearest-place result."
                ),
            },
            limitations=[
                "The context intentionally omits coordinates, street, building, "
                "postal code, and exact address.",
                "Administrative-locality bias is approximate; use Korea place and "
                "route tools when exact proximity matters.",
            ],
            meta={
                "endpoint": "/v2/local/geo/coord2address.json",
                "location_precision": "administrative_neighborhood",
            },
        )

    def route(
        self,
        *,
        mode: Any,
        origin_latitude: Any,
        origin_longitude: Any,
        destination_latitude: Any,
        destination_longitude: Any,
        origin_name: Any = "출발",
        destination_name: Any = "도착",
        walking_preference: Any = "broad_first",
        car_priority: Any = "recommend",
        max_routes: Any = 3,
    ) -> Dict[str, Any]:
        safe_mode = str(mode or "").lower()
        if safe_mode not in {"walking", "transit", "car"}:
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                "mode must be walking, transit, or car.",
                provider="validation",
            )
        origin_lat, origin_lon = _coordinate_pair(
            origin_latitude,
            origin_longitude,
            prefix="origin",
        )
        destination_lat, destination_lon = _coordinate_pair(
            destination_latitude,
            destination_longitude,
            prefix="destination",
        )
        start_name = _optional_text(origin_name, field="origin_name") or "출발"
        end_name = _optional_text(destination_name, field="destination_name") or "도착"
        route_limit = _bounded_int(
            max_routes, field="max_routes", default=3, minimum=1, maximum=5
        )
        links = build_kakao_links(
            end_name,
            destination_lat,
            destination_lon,
        )

        if safe_mode == "walking":
            preference = str(walking_preference or "broad_first").lower()
            walking_modes = {
                "broad_first": "BROAD_FIRST",
                "shortest": "SHORTEST",
                "accessible": "ACCESSIBLE",
            }
            if preference not in walking_modes:
                raise KoreaApiError(
                    "INVALID_ARGUMENT",
                    "walking_preference is unsupported.",
                    provider="validation",
                )
            endpoint = "/v2/routing/walk"
            provider = "kakao_map_walking"
            payload = self._kakao_get(
                endpoint,
                params={
                    "start_x": _format_coordinate(origin_lon),
                    "start_y": _format_coordinate(origin_lat),
                    "end_x": _format_coordinate(destination_lon),
                    "end_y": _format_coordinate(destination_lat),
                    "s_name": start_name,
                    "e_name": end_name,
                    "input_coord": "WGS84",
                    "output_coord": "WGS84",
                    "route_mode": walking_modes[preference],
                },
                provider=provider,
            )
            _route_status_failure(payload, provider=provider)
            route = _required_route_mapping(
                payload.get("route"), provider=provider, field="route"
            )
            properties = _required_route_mapping(
                route.get("properties"), provider=provider, field="route.properties"
            )
            legs = _required_route_list(
                route.get("legs"), provider=provider, field="route.legs"
            )
            total_distance = _required_nonnegative_int(
                properties.get("totalDistance"),
                provider=provider,
                field="route.properties.totalDistance",
            )
            total_time = _required_nonnegative_int(
                properties.get("totalTime"),
                provider=provider,
                field="route.properties.totalTime",
            )
            steps: List[Dict[str, Any]] = []
            for leg in legs:
                leg_steps = _required_route_list(
                    leg.get("steps"),
                    provider=provider,
                    field="route.legs.steps",
                    allow_empty=True,
                )
                for step in leg_steps:
                    step_properties = _required_route_mapping(
                        step.get("properties"),
                        provider=provider,
                        field="route.legs.steps.properties",
                    )
                    steps.append(
                        {
                            "guidance": str(step_properties.get("guidance", "")),
                            "distance_m": _integer_or_none(step_properties.get("distance")),
                            "duration_s": _integer_or_none(step_properties.get("time")),
                        }
                    )
                    if len(steps) >= 80:
                        break
                if len(steps) >= 80:
                    break
            data = {
                "mode": safe_mode,
                "status": "OK",
                "distance_m": total_distance,
                "duration_s": total_time,
                "steps": steps,
                "links": links,
            }
        elif safe_mode == "transit":
            endpoint = "/v2/routing/publictraffic"
            provider = "kakao_map_transit"
            payload = self._kakao_get(
                endpoint,
                params={
                    "start_x": _format_coordinate(origin_lon),
                    "start_y": _format_coordinate(origin_lat),
                    "end_x": _format_coordinate(destination_lon),
                    "end_y": _format_coordinate(destination_lat),
                    "s_name": start_name,
                    "e_name": end_name,
                    "input_coord": "WGS84",
                    "output_coord": "WGS84",
                },
                provider=provider,
            )
            _route_status_failure(payload, provider=provider)
            raw_routes = _required_route_list(
                payload.get("routes"), provider=provider, field="routes"
            )
            routes: List[Dict[str, Any]] = []
            for raw_route in raw_routes[:route_limit]:
                properties = _required_route_mapping(
                    raw_route.get("properties"),
                    provider=provider,
                    field="routes.properties",
                )
                total_distance = _required_nonnegative_int(
                    properties.get("totalDistance"),
                    provider=provider,
                    field="routes.properties.totalDistance",
                )
                total_time = _required_nonnegative_int(
                    properties.get("totalTime"),
                    provider=provider,
                    field="routes.properties.totalTime",
                )
                route_steps: List[Dict[str, Any]] = []
                raw_steps = _required_route_list(
                    raw_route.get("steps"),
                    provider=provider,
                    field="routes.steps",
                    allow_empty=True,
                )
                for raw_step in raw_steps:
                    step_properties = _required_route_mapping(
                        raw_step.get("properties"),
                        provider=provider,
                        field="routes.steps.properties",
                    )
                    stops = [
                        str(stop.get("name", ""))
                        for stop in _list_of_mappings(step_properties.get("stops"))[:25]
                    ]
                    vehicles = [
                        {
                            "type": str(vehicle.get("type", "")),
                            "name": str(vehicle.get("name", "")),
                        }
                        for vehicle in _list_of_mappings(
                            step_properties.get("vehicles")
                        )[:10]
                    ]
                    route_steps.append(
                        {
                            "guidance": str(step_properties.get("guidance", "")),
                            "type": str(step_properties.get("type", "")),
                            "distance_m": _integer_or_none(step_properties.get("distance")),
                            "duration_s": _integer_or_none(step_properties.get("time")),
                            "stops": stops,
                            "vehicles": vehicles,
                        }
                    )
                fare = properties.get("fare") if isinstance(properties.get("fare"), dict) else {}
                routes.append(
                    {
                        "type": str(properties.get("type", "")),
                        "distance_m": total_distance,
                        "duration_s": total_time,
                        "transfers": _integer_or_none(properties.get("transfers")),
                        "fare_won": _integer_or_none(fare.get("value")),
                        "steps": route_steps,
                    }
                )
            data = {
                "mode": safe_mode,
                "status": "OK",
                "routes": routes,
                "links": links,
            }
        else:
            priority = str(car_priority or "recommend").lower()
            priorities = {
                "recommend": "RECOMMEND",
                "time": "TIME",
                "distance": "DISTANCE",
            }
            if priority not in priorities:
                raise KoreaApiError(
                    "INVALID_ARGUMENT",
                    "car_priority is unsupported.",
                    provider="validation",
                )
            endpoint = KAKAO_MOBILITY_DIRECTIONS
            provider = "kakao_mobility_car"
            payload = self._kakao_get(
                endpoint,
                params={
                    "origin": (
                        f"{_format_coordinate(origin_lon)},"
                        f"{_format_coordinate(origin_lat)}"
                    ),
                    "destination": (
                        f"{_format_coordinate(destination_lon)},"
                        f"{_format_coordinate(destination_lat)}"
                    ),
                    "priority": priorities[priority],
                    "summary": "true",
                    "alternatives": "true" if route_limit > 1 else "false",
                    "road_details": "false",
                },
                provider=provider,
            )
            raw_routes = _required_route_list(
                payload.get("routes"), provider=provider, field="routes"
            )
            validated_routes: List[Tuple[Dict[str, Any], int]] = []
            for raw_route in raw_routes:
                result_code = raw_route.get("result_code")
                if not isinstance(result_code, int) or isinstance(
                    result_code,
                    bool,
                ):
                    _invalid_route_response(provider, "routes.result_code")
                if result_code != 0:
                    raise KoreaApiError(
                        "NO_ROUTE",
                        "Kakao Mobility could not build every requested car route.",
                        provider=provider,
                        provider_error_code=result_code,
                    )
                validated_routes.append((raw_route, result_code))
            routes = []
            for raw_route, result_code in validated_routes[:route_limit]:
                summary = _required_route_mapping(
                    raw_route.get("summary"),
                    provider=provider,
                    field="routes.summary",
                )
                distance = _required_nonnegative_int(
                    summary.get("distance"),
                    provider=provider,
                    field="routes.summary.distance",
                )
                duration = _required_nonnegative_int(
                    summary.get("duration"),
                    provider=provider,
                    field="routes.summary.duration",
                )
                fare = summary.get("fare") if isinstance(summary.get("fare"), dict) else {}
                routes.append(
                    {
                        "result_code": result_code,
                        "result_message": str(raw_route.get("result_msg", "")),
                        "priority": str(summary.get("priority", "")),
                        "distance_m": distance,
                        "duration_s": duration,
                        "estimated_taxi_fare_won": _integer_or_none(fare.get("taxi")),
                        "estimated_toll_won": _integer_or_none(fare.get("toll")),
                    }
                )
            data = {
                "mode": safe_mode,
                "routes": routes,
                "links": links,
            }
        return _success(
            provider,
            data,
            limitations=[
                "Durations, fares, and service availability are estimates and may change."
            ],
            meta={"endpoint": endpoint},
        )

    def shopping_search(
        self,
        query: Any,
    ) -> Dict[str, Any]:
        query_text = _text(query, field="query", maximum=200)
        browser_url = (
            "https://search.shopping.naver.com/search/all?query="
            + quote(query_text, safe="")
        )
        return _success(
            "browser_handoff",
            {
                "url": browser_url,
                "target": "naver_shopping_web",
                "api_call_made": False,
                "instruction": (
                    "Open this URL with the local browser. For nearby stock, inspect "
                    "the official sites of chains returned by korea_place_search."
                ),
            },
            limitations=[SHOPPING_LIMITATION],
            meta={"external_api_used": False},
        )


def make_kakao_links(
    *,
    destination_name: Any,
    destination_latitude: Any,
    destination_longitude: Any,
    place_id: Any = "",
) -> Dict[str, Any]:
    destination = _text(destination_name, field="destination_name", maximum=100)
    destination_lat, destination_lon = _coordinate_pair(
        destination_latitude,
        destination_longitude,
        prefix="destination",
    )
    safe_place_id = ""
    if place_id not in (None, ""):
        safe_place_id = str(place_id).strip()
        if not safe_place_id.isdigit():
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                "place_id must contain only digits.",
                provider="validation",
            )
    links = build_kakao_links(
        destination,
        destination_lat,
        destination_lon,
        place_id=safe_place_id,
    )
    return _success(
        "local_link_generator",
        links,
        limitations=[KAKAO_T_LIMITATION],
    )
