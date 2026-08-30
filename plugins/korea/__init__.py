"""Hermes registration for official Kakao Korea tools and browser handoffs."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Tuple

from .client import KoreaApiClient, KoreaApiError, make_kakao_links
from .location_privacy import (
    LOCATION_TOKENS,
    PRE_GATEWAY_DISPATCH,
    LocationReservation,
    LocationTokenError,
    install_location_ingress_guard,
    is_location_ingress_guard_installed,
)
from .schemas import (
    KOREA_GEOCODE,
    KOREA_KAKAO_LINKS,
    KOREA_PLACE_SEARCH,
    KOREA_REVERSE_GEOCODE,
    KOREA_ROUTE,
    KOREA_SHOPPING_SEARCH,
)


TOKEN_PLACE_SEARCH_RADIUS_M = 3000


def _json_result(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _unexpected_failure() -> Dict[str, Any]:
    return {
        "success": False,
        "meta": {
            "provider": "korea_plugin",
            "provider_used": "korea_plugin",
            "location_retention": "no_plugin_file_persistence",
        },
        "error": {
            "code": "UNEXPECTED_ERROR",
            "message": "The Korea plugin encountered an unexpected internal error.",
        },
    }


def _handle(
    args: Any,
    operation: Callable[[KoreaApiClient, Dict[str, Any]], Dict[str, Any]],
) -> str:
    safe_args = args if isinstance(args, dict) else {}
    try:
        # A fresh stateless client per call makes the no-location-retention boundary
        # explicit. Only its injected HTTP/env callables remain after the invocation.
        return _json_result(operation(KoreaApiClient(), safe_args))
    except KoreaApiError as exc:
        return _json_result(exc.as_result())
    except Exception:  # noqa: BLE001 - never leak secrets or raw provider bodies
        return _json_result(_unexpected_failure())


def _reserve_token_coordinates(
    values: Dict[str, Any],
    *,
    token_field: str,
    latitude_field: str,
    longitude_field: str,
    purpose: str,
    required: bool,
) -> Tuple[Dict[str, Any], Optional[LocationReservation]]:
    """Exclusively resolve a token without adding coordinates to tool output."""
    resolved = dict(values)
    token = resolved.pop(token_field, None)
    has_latitude = latitude_field in resolved
    has_longitude = longitude_field in resolved
    if token not in (None, "") and (has_latitude or has_longitude):
        raise KoreaApiError(
            "INVALID_ARGUMENT",
            f"Use either {token_field} or the coordinate pair, not both.",
            provider="validation",
        )
    if token in (None, ""):
        if has_latitude != has_longitude or (required and not has_latitude):
            raise KoreaApiError(
                "INVALID_ARGUMENT",
                (
                    f"Provide {token_field} or both {latitude_field} and "
                    f"{longitude_field}."
                ),
                provider="validation",
            )
        return resolved, None
    try:
        reservation, latitude, longitude = LOCATION_TOKENS.reserve(
            token,
            purpose=purpose,
        )
    except LocationTokenError as exc:
        raise KoreaApiError(
            exc.code,
            exc.message,
            provider="location_token_store",
        ) from exc
    resolved[latitude_field] = latitude
    resolved[longitude_field] = longitude
    return resolved, reservation


def _place_search(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs

    def operation(client: KoreaApiClient, values: Dict[str, Any]) -> Dict[str, Any]:
        used_token = values.get("location_token") not in (None, "")
        resolved, reservation = _reserve_token_coordinates(
            values,
            token_field="location_token",
            latitude_field="latitude",
            longitude_field="longitude",
            purpose="place_search",
            required=False,
        )
        committed = False
        try:
            if used_token:
                # A caller-controlled radius would be a distance-membership oracle:
                # repeated binary-search requests could reconstruct exact POI distances.
                resolved["radius_m"] = TOKEN_PLACE_SEARCH_RADIUS_M
                resolved["sort"] = "distance"
            result = client.place_search(**resolved)
            if used_token and result.get("success") is True:
                committed = LOCATION_TOKENS.commit(reservation)
                result.setdefault("meta", {})["location_token_state"] = (
                    "place_search_use_consumed_route_use_retained_until_ttl"
                    if committed
                    else "expired_during_successful_place_search"
                )
                result["meta"]["token_search_radius"] = (
                    "fixed_3km_privacy_boundary"
                )
            return result
        finally:
            if reservation is not None and not committed:
                LOCATION_TOKENS.release(reservation)

    return _handle(args, operation)


def _geocode(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _handle(args, lambda client, values: client.geocode(**values))


def _reverse_geocode(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    safe_args = args if isinstance(args, dict) else {}
    if "location_token" in safe_args:
        return _json_result(
            KoreaApiError(
                "LOCATION_TOKEN_REVERSE_GEOCODE_BLOCKED",
                (
                    "Reverse geocoding an ephemeral current-location token is "
                    "blocked because an exact address could enter persistent history."
                ),
                provider="privacy_policy",
            ).as_result()
        )
    return _handle(
        safe_args,
        lambda client, values: client.reverse_geocode(**values),
    )


def _route(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs

    def operation(client: KoreaApiClient, values: Dict[str, Any]) -> Dict[str, Any]:
        used_token = values.get("origin_location_token") not in (None, "")
        resolved, reservation = _reserve_token_coordinates(
            values,
            token_field="origin_location_token",
            latitude_field="origin_latitude",
            longitude_field="origin_longitude",
            purpose="route",
            required=True,
        )
        committed = False
        try:
            result = client.route(**resolved)
            if used_token and result.get("success") is True:
                committed = LOCATION_TOKENS.commit(reservation)
                result.setdefault("meta", {})["location_token_state"] = (
                    "consumed_after_successful_route"
                    if committed
                    else "expired_during_successful_route"
                )
            return result
        finally:
            if reservation is not None and not committed:
                LOCATION_TOKENS.release(reservation)

    return _handle(args, operation)


def _shopping_search(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    return _handle(args, lambda client, values: client.shopping_search(**values))


def _kakao_links(args: Dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    safe_args = args if isinstance(args, dict) else {}
    try:
        return _json_result(make_kakao_links(**safe_args))
    except KoreaApiError as exc:
        return _json_result(exc.as_result())
    except Exception:  # noqa: BLE001 - typed, secret-free tool failure
        return _json_result(_unexpected_failure())


def register(ctx: Any) -> None:
    """Register six narrow Korea tools and the Telegram privacy hook."""
    if not install_location_ingress_guard(LOCATION_TOKENS):
        raise RuntimeError(
            "Korea plugin could not install its Telegram ingress privacy guard."
        )
    ctx.register_hook("pre_gateway_dispatch", PRE_GATEWAY_DISPATCH)
    registrations = [
        (
            "korea_place_search",
            KOREA_PLACE_SEARCH,
            _place_search,
            "Search Korean places with official Kakao Local APIs.",
        ),
        (
            "korea_geocode",
            KOREA_GEOCODE,
            _geocode,
            "Convert a Korean address to coordinates with Kakao.",
        ),
        (
            "korea_reverse_geocode",
            KOREA_REVERSE_GEOCODE,
            _reverse_geocode,
            "Convert coordinates to a Korean address with Kakao.",
        ),
        (
            "korea_route",
            KOREA_ROUTE,
            _route,
            "Build walking, transit, or car routes with official Kakao APIs.",
        ),
        (
            "korea_shopping_search",
            KOREA_SHOPPING_SEARCH,
            _shopping_search,
            "Generate a zero-API Naver Shopping URL for the local browser.",
        ),
        (
            "korea_kakao_links",
            KOREA_KAKAO_LINKS,
            _kakao_links,
            "Generate documented KakaoMap and official Kakao T launch links.",
        ),
    ]
    for name, schema, handler, description in registrations:
        ctx.register_tool(
            name=name,
            toolset="korea",
            schema=schema,
            handler=handler,
            description=description,
        )
