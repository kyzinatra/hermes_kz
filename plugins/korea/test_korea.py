"""Mocked contract and privacy tests for the Korea Hermes plugin."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import threading
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


PLUGIN_PARENT = Path(__file__).resolve().parent.parent
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

import korea  # noqa: E402
from korea.client import (  # noqa: E402
    KAKAO_MOBILITY_DIRECTIONS,
    KAKAO_T_LAUNCH_URL,
    INITIAL_RETRY_DELAY_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    KoreaApiClient,
    KoreaApiError,
    _NO_REDIRECT_OPENER,
    _RejectRedirectHandler,
    make_kakao_links,
)
from korea.location_privacy import (  # noqa: E402
    LOCATION_TTL_SECONDS,
    LocationTokenError,
    LocationTokenStore,
    install_location_ingress_guard,
    is_location_ingress_guard_installed,
    make_pre_gateway_dispatch_hook,
)
from korea.schemas import (  # noqa: E402
    KOREA_PLACE_SEARCH,
    KOREA_REVERSE_GEOCODE,
    KOREA_ROUTE,
    LOCATION_SEARCH_CONTEXT,
)


KAKAO_KEY = "private-kakao-key"
ORIGIN_LATITUDE = 37.12345678
ORIGIN_LONGITUDE = 127.87654321
DESTINATION_LATITUDE = 37.555
DESTINATION_LONGITUDE = 126.999


class JsonResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "JsonResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def environment(name: str) -> str:
    return {
        "KAKAO_REST_API_KEY": KAKAO_KEY,
    }.get(name, "")


def headers_lower(request: Any) -> Dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


class OfficialApiContractTests(unittest.TestCase):
    def test_credentialed_transport_rejects_every_redirect(self) -> None:
        handler = _RejectRedirectHandler()
        request = Request(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers={"Authorization": f"KakaoAK {KAKAO_KEY}"},
        )
        destinations = {
            "same_origin": "https://dapi.kakao.com/redirected",
            "cross_origin": "https://attacker.invalid/collect",
        }

        self.assertTrue(
            any(
                isinstance(installed, _RejectRedirectHandler)
                for installed in _NO_REDIRECT_OPENER.handlers
            )
        )
        for origin_kind, destination in destinations.items():
            for status_code in (301, 302, 303, 307, 308):
                with self.subTest(origin_kind=origin_kind, status_code=status_code):
                    redirected = handler.redirect_request(
                        request,
                        fp=None,
                        code=status_code,
                        msg="redirect",
                        headers={"Location": destination},
                        newurl=destination,
                    )
                    self.assertIsNone(redirected)

    def test_place_search_uses_kakao_local_and_labels_provider(self) -> None:
        captured: Dict[str, Any] = {}

        def opener(request: Any, timeout: float) -> JsonResponse:
            captured.update(request=request, timeout=timeout)
            return JsonResponse(
                {
                    "meta": {"total_count": 3, "pageable_count": 3, "is_end": True},
                    "documents": [
                        {
                            "id": "1234",
                            "place_name": "라멘집",
                            "category_name": "음식점 > 일식",
                            "category_group_code": "FD6",
                            "phone": "02-123-4567",
                            "address_name": "서울 중구",
                            "road_address_name": "서울 중구 세종대로 1",
                            "x": "126.99",
                            "y": "37.55",
                            "distance": "347",
                            "place_url": "https://place.map.kakao.com/1234",
                        },
                        {
                            "id": "5678",
                            "place_name": "라멘집 둘",
                            "x": "126.98",
                            "y": "37.56",
                            "distance": "1289",
                        },
                        {
                            "id": "9012",
                            "place_name": "라멘집 셋",
                            "x": "127.01",
                            "y": "37.57",
                            "distance": "5412",
                        },
                    ],
                }
            )

        client = KoreaApiClient(opener=opener, env_lookup=environment)
        result = client.place_search(
            query="라멘",
            category="restaurant",
            latitude=ORIGIN_LATITUDE,
            longitude=ORIGIN_LONGITUDE,
            radius_m=1500,
        )

        parsed = urlsplit(captured["request"].full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "https://dapi.kakao.com/v2/local/search/keyword.json",
        )
        self.assertEqual(query["query"], ["라멘"])
        self.assertEqual(query["category_group_code"], ["FD6"])
        self.assertEqual(query["radius"], ["1500"])
        self.assertEqual(
            headers_lower(captured["request"])["authorization"],
            f"KakaoAK {KAKAO_KEY}",
        )
        self.assertEqual(result["meta"]["provider_used"], "kakao_local")
        place = result["data"]["places"][0]
        self.assertIsNone(place["live_details"]["hours"])
        self.assertIn("directions_from_current_location", place["links"])
        self.assertNotIn("distance_m", place)
        self.assertEqual(
            [item["distance_band"] for item in result["data"]["places"]],
            [
                "very_near_under_500m",
                "nearby_500m_to_1_5km",
                "far_3km_to_7km",
            ],
        )
        self.assertEqual(
            [item["proximity_rank"] for item in result["data"]["places"]],
            [1, 2, 3],
        )
        rendered = json.dumps(result, ensure_ascii=False)
        for exact_distance in ("347", "1289", "5412"):
            self.assertNotIn(exact_distance, rendered)

    def test_geocoding_uses_official_kakao_endpoints(self) -> None:
        calls: List[str] = []

        def opener(request: Any, timeout: float) -> JsonResponse:
            del timeout
            calls.append(request.full_url)
            if "coord2address" in request.full_url:
                return JsonResponse(
                    {
                        "documents": [
                            {
                                "road_address": {
                                    "address_name": "서울특별시 중구 세종대로 110",
                                    "building_name": "서울특별시청",
                                    "zone_no": "04524",
                                },
                                "address": {
                                    "address_name": "서울 중구 태평로1가 31",
                                    "region_1depth_name": "서울특별시",
                                    "region_2depth_name": "중구",
                                    "region_3depth_name": "태평로1가",
                                    "region_3depth_h_name": "소공동",
                                },
                            }
                        ]
                    }
                )
            return JsonResponse(
                {
                    "documents": [
                        {
                            "address_name": "서울 중구 세종대로 110",
                            "address_type": "ROAD_ADDR",
                            "x": "126.978",
                            "y": "37.566",
                            "road_address": {"address_name": "서울 중구 세종대로 110"},
                            "address": {"address_name": "서울 중구 태평로1가 31"},
                        }
                    ]
                }
            )

        client = KoreaApiClient(opener=opener, env_lookup=environment)
        geocoded = client.geocode("서울특별시 중구 세종대로 110")
        reversed_result = client.reverse_geocode(37.566, 126.978)
        search_context = client.location_search_context(
            37.566,
            126.978,
            query="late-night pharmacy",
        )

        self.assertIn("/v2/local/search/address.json", calls[0])
        self.assertIn("/v2/local/geo/coord2address.json", calls[1])
        self.assertIn("/v2/local/geo/coord2address.json", calls[2])
        self.assertEqual(geocoded["meta"]["provider_used"], "kakao_geocoding")
        self.assertEqual(
            reversed_result["data"]["results"][0]["building_name"],
            "서울특별시청",
        )
        self.assertEqual(
            search_context["data"]["localized_query"],
            "late-night pharmacy 서울특별시 중구 소공동",
        )
        rendered_context = json.dumps(search_context, ensure_ascii=False)
        for sensitive in (
            "37.566",
            "126.978",
            "세종대로",
            "서울특별시청",
            "04524",
            "태평로1가 31",
        ):
            self.assertNotIn(sensitive, rendered_context)

    def test_kakao_local_rejects_malformed_documents_contract(self) -> None:
        cases = [
            ("missing", {}),
            ("non_list", {"documents": {}}),
            ("non_mapping_item", {"documents": [None]}),
            ("empty_mapping_item", {"documents": [{}]}),
        ]
        operations = (
            ("place", lambda client: client.place_search(query="라멘")),
            ("geocode", lambda client: client.geocode("서울시청")),
            (
                "reverse",
                lambda client: client.reverse_geocode(37.566, 126.978),
            ),
            (
                "location_context",
                lambda client: client.location_search_context(
                    37.566,
                    126.978,
                    query="pharmacy",
                ),
            ),
        )
        for operation_name, operation in operations:
            for response_name, payload in cases:
                with self.subTest(
                    operation=operation_name,
                    response=response_name,
                ):
                    client = KoreaApiClient(
                        opener=lambda *_args, payload=payload, **_kwargs: JsonResponse(
                            payload
                        ),
                        env_lookup=environment,
                    )
                    with self.assertRaises(KoreaApiError) as raised:
                        operation(client)
                    self.assertEqual(raised.exception.code, "INVALID_RESPONSE")

    def test_all_route_modes_use_current_official_endpoints(self) -> None:
        calls: List[Any] = []
        responses = [
            {
                "status": "OK",
                "landingUrl": (
                    "https://unsafe.example/?origin="
                    f"{ORIGIN_LATITUDE},{ORIGIN_LONGITUDE}"
                ),
                "route": {
                    "properties": {"totalDistance": 800, "totalTime": 600},
                    "legs": [
                        {
                            "steps": [
                                {
                                    "properties": {
                                        "guidance": "직진",
                                        "distance": 100,
                                        "time": 80,
                                        "x": ORIGIN_LONGITUDE,
                                        "y": ORIGIN_LATITUDE,
                                    }
                                }
                            ]
                        }
                    ],
                },
            },
            {
                "status": "OK",
                "properties": {
                    "landingURL": (
                        "https://unsafe.example/?start="
                        f"{ORIGIN_LATITUDE},{ORIGIN_LONGITUDE}"
                    )
                },
                "routes": [
                    {
                        "properties": {
                            "type": "SUBWAY",
                            "totalDistance": 9000,
                            "totalTime": 1800,
                            "transfers": 1,
                            "fare": {"value": 1500},
                        },
                        "steps": [
                            {
                                "properties": {
                                    "guidance": "2호선",
                                    "type": "SUBWAY",
                                    "distance": 8000,
                                    "time": 1500,
                                    "stops": [{"name": "시청역"}],
                                    "vehicles": [{"type": "SUBWAY", "name": "2호선"}],
                                }
                            }
                        ],
                    }
                ],
            },
            {
                "routes": [
                    {
                        "result_code": 0,
                        "result_msg": "success",
                        "summary": {
                            "priority": "RECOMMEND",
                            "distance": 12000,
                            "duration": 1400,
                            "fare": {"taxi": 17000, "toll": 0},
                        },
                    }
                ]
            },
        ]

        def opener(request: Any, timeout: float) -> JsonResponse:
            del timeout
            calls.append(request)
            return JsonResponse(responses[len(calls) - 1])

        client = KoreaApiClient(opener=opener, env_lookup=environment)
        results = [
            client.route(
                mode=mode,
                origin_latitude=ORIGIN_LATITUDE,
                origin_longitude=ORIGIN_LONGITUDE,
                destination_latitude=DESTINATION_LATITUDE,
                destination_longitude=DESTINATION_LONGITUDE,
                destination_name="목적지",
            )
            for mode in ("walking", "transit", "car")
        ]

        self.assertIn("/v2/routing/walk", calls[0].full_url)
        self.assertIn("/v2/routing/publictraffic", calls[1].full_url)
        self.assertTrue(calls[2].full_url.startswith(KAKAO_MOBILITY_DIRECTIONS))
        self.assertEqual(
            parse_qs(urlsplit(calls[2].full_url).query)["alternatives"],
            ["true"],
        )
        self.assertEqual(
            [item["meta"]["provider_used"] for item in results],
            ["kakao_map_walking", "kakao_map_transit", "kakao_mobility_car"],
        )
        for result in results:
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(str(ORIGIN_LATITUDE), rendered)
            self.assertNotIn(str(ORIGIN_LONGITUDE), rendered)
            self.assertNotIn("unsafe.example", rendered)
            self.assertNotIn("provider_landing", rendered)
            self.assertNotIn("start_latitude", rendered)
            links = result["data"]["links"]["kakao_map"]
            self.assertEqual(
                set(links),
                {"map", "directions_from_current_location"},
            )

    def test_empty_and_malformed_routes_fail_with_typed_errors(self) -> None:
        cases = [
            ("walking", {}, "INVALID_RESPONSE"),
            ("walking", {"status": "OK", "route": []}, "INVALID_RESPONSE"),
            ("transit", {"status": "OK", "routes": []}, "NO_ROUTE"),
            ("transit", {"status": "OK", "routes": [{}]}, "INVALID_RESPONSE"),
            ("car", {"routes": []}, "NO_ROUTE"),
            ("car", {"routes": [{}]}, "INVALID_RESPONSE"),
        ]

        for mode, payload, expected_code in cases:
            with self.subTest(mode=mode, payload=payload):
                client = KoreaApiClient(
                    opener=lambda *_args, **_kwargs: JsonResponse(payload),
                    env_lookup=environment,
                )
                with self.assertRaises(KoreaApiError) as raised:
                    client.route(
                        mode=mode,
                        origin_latitude=ORIGIN_LATITUDE,
                        origin_longitude=ORIGIN_LONGITUDE,
                        destination_latitude=DESTINATION_LATITUDE,
                        destination_longitude=DESTINATION_LONGITUDE,
                    )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotEqual(raised.exception.provider, "validation")

    def test_car_validates_every_result_code_and_disables_single_alternative(self) -> None:
        captured: List[Any] = []
        payload = {
            "routes": [
                {
                    "result_code": 0,
                    "summary": {"distance": 1000, "duration": 300},
                },
                {
                    "result_code": 104,
                    "result_msg": "no route",
                    "summary": {"distance": 0, "duration": 0},
                },
            ]
        }

        def opener(request: Any, timeout: float) -> JsonResponse:
            del timeout
            captured.append(request)
            return JsonResponse(payload)

        client = KoreaApiClient(opener=opener, env_lookup=environment)
        with self.assertRaises(KoreaApiError) as raised:
            client.route(
                mode="car",
                origin_latitude=ORIGIN_LATITUDE,
                origin_longitude=ORIGIN_LONGITUDE,
                destination_latitude=DESTINATION_LATITUDE,
                destination_longitude=DESTINATION_LONGITUDE,
                max_routes=1,
            )

        self.assertEqual(raised.exception.code, "NO_ROUTE")
        self.assertEqual(raised.exception.provider_error_code, 104)
        self.assertEqual(
            parse_qs(urlsplit(captured[0].full_url).query)["alternatives"],
            ["false"],
        )

    def test_shopping_is_strict_browser_handoff_without_network_or_credentials(self) -> None:
        client = KoreaApiClient(
            opener=lambda *_args, **_kwargs: self.fail("network must not be called"),
            env_lookup=lambda _name: self.fail("credentials must not be read"),
        )

        result = client.shopping_search("무선 이어폰")

        self.assertTrue(result["success"])
        self.assertFalse(result["meta"]["external_api_used"])
        self.assertEqual(result["meta"]["provider_used"], "browser_handoff")
        self.assertFalse(result["data"]["api_call_made"])
        self.assertIn("search.shopping.naver.com", result["data"]["url"])

    def test_kakao_links_are_destination_only_and_taxi_prefill_is_not_claimed(self) -> None:
        result = make_kakao_links(
            destination_name="서울역",
            destination_latitude=37.5547,
            destination_longitude=126.9706,
            place_id="123",
        )

        links = result["data"]
        self.assertEqual(result["meta"]["provider_used"], "local_link_generator")
        self.assertEqual(links["kakao_t"]["app_launch"], KAKAO_T_LAUNCH_URL)
        self.assertFalse(links["kakao_t"]["destination_prefilled"])
        rendered = json.dumps(links)
        self.assertNotIn("t.kakao.com/launch", rendered)
        self.assertNotIn("dest_lat", rendered)
        self.assertEqual(
            set(links["kakao_map"]),
            {"map", "directions_from_current_location"},
        )

    def test_http_failure_does_not_echo_key_body_or_request_coordinates(self) -> None:
        failure = HTTPError(
            "https://dapi.kakao.com/private",
            403,
            f"echo {KAKAO_KEY} {ORIGIN_LATITUDE}",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps(
                    {
                        "code": -3,
                        "msg": f"server echoed {KAKAO_KEY} {ORIGIN_LONGITUDE}",
                    }
                ).encode("utf-8")
            ),
        )
        client = KoreaApiClient(
            opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
            env_lookup=environment,
        )

        with self.assertRaises(KoreaApiError) as raised:
            client.place_search(
                query="라멘",
                latitude=ORIGIN_LATITUDE,
                longitude=ORIGIN_LONGITUDE,
            )

        result = raised.exception.as_result()
        rendered = json.dumps(result)
        self.assertNotIn(KAKAO_KEY, rendered)
        self.assertNotIn(str(ORIGIN_LATITUDE), rendered)
        self.assertNotIn(str(ORIGIN_LONGITUDE), rendered)
        self.assertEqual(result["error"]["provider_error_code"], -3)
        self.assertIn("Usage settings", result["error"]["message"])

    def test_idempotent_get_retries_transient_failures_with_bounded_backoff(self) -> None:
        calls: List[Any] = []
        sleeps: List[float] = []

        def opener(request: Any, timeout: float) -> JsonResponse:
            del timeout
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    429,
                    f"do not expose {KAKAO_KEY}",
                    hdrs={"Retry-After": "9999"},
                    fp=io.BytesIO(b"{}"),
                )
            if len(calls) == 2:
                raise URLError(f"do not expose {ORIGIN_LATITUDE}")
            return JsonResponse(
                {"meta": {"is_end": True}, "documents": []}
            )

        client = KoreaApiClient(
            opener=opener,
            env_lookup=environment,
            sleeper=sleeps.append,
        )
        result = client.place_search(query="라멘")

        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            sleeps,
            [MAX_RETRY_DELAY_SECONDS, INITIAL_RETRY_DELAY_SECONDS * 2],
        )
        rendered = json.dumps(result)
        self.assertNotIn(KAKAO_KEY, rendered)
        self.assertNotIn(str(ORIGIN_LATITUDE), rendered)

    def test_retry_budget_is_bounded_and_final_http_failure_is_typed(self) -> None:
        calls: List[Any] = []
        sleeps: List[float] = []

        def opener(request: Any, timeout: float) -> JsonResponse:
            del timeout
            calls.append(request)
            raise HTTPError(
                request.full_url,
                503,
                f"do not expose {KAKAO_KEY}",
                hdrs={},
                fp=io.BytesIO(b'{"code":-1}'),
            )

        client = KoreaApiClient(
            opener=opener,
            env_lookup=environment,
            sleeper=sleeps.append,
        )
        with self.assertRaises(KoreaApiError) as raised:
            client.place_search(query="라멘")

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            sleeps,
            [INITIAL_RETRY_DELAY_SECONDS, INITIAL_RETRY_DELAY_SECONDS * 2],
        )
        self.assertEqual(raised.exception.code, "PROVIDER_HTTP_ERROR")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn(KAKAO_KEY, json.dumps(raised.exception.as_result()))


class LocationPrivacyTests(unittest.TestCase):
    def _store(self, **overrides: Any) -> LocationTokenStore:
        counter = iter(range(1000))
        options = {
            "ttl_seconds": LOCATION_TTL_SECONDS,
            "token_factory": lambda: f"{next(counter):024d}",
            "schedule_expiry": False,
        }
        options.update(overrides)
        return LocationTokenStore(**options)

    @staticmethod
    def _event(
        *,
        latitude: Any = ORIGIN_LATITUDE,
        longitude: Any = ORIGIN_LONGITUDE,
        live_period: Any = None,
        edit_date: Any = None,
        message_id: str = "message-1",
        text: str = "",
    ) -> Any:
        location = SimpleNamespace(
            latitude=latitude,
            longitude=longitude,
            live_period=live_period,
        )
        raw = SimpleNamespace(location=location, venue=None, edit_date=edit_date)
        return SimpleNamespace(
            text=text
            or (
                "[The user shared a location pin.]\n"
                f"latitude: {latitude}\nlongitude: {longitude}\n"
                "Map: https://www.google.com/maps/search/?api=1&"
                f"query={latitude},{longitude}"
            ),
            message_type="location",
            source=SimpleNamespace(
                platform="telegram",
                user_id="user-1",
                chat_id="chat-1",
                thread_id="",
            ),
            raw_message=raw,
            message_id=message_id,
            metadata={},
        )

    def test_static_pin_is_rewritten_to_opaque_ram_token(self) -> None:
        store = self._store()
        hook = make_pre_gateway_dispatch_hook(store)
        event = self._event()

        decision = hook(event)

        self.assertEqual(decision["action"], "rewrite")
        token_match = re.search(r"loc_[A-Za-z0-9_-]{24,128}", decision["text"])
        self.assertIsNotNone(token_match)
        token = token_match.group(0)
        self.assertEqual(store.peek(token), (ORIGIN_LATITUDE, ORIGIN_LONGITUDE))
        self.assertIsNone(event.raw_message)
        self.assertIn("location_search_context", decision["text"])
        for sensitive in (
            str(ORIGIN_LATITUDE),
            str(ORIGIN_LONGITUDE),
            "google.com/maps",
        ):
            self.assertNotIn(sensitive, decision["text"])
            self.assertNotIn(sensitive, event.text)

    def test_plain_enum_telegram_platform_is_intercepted(self) -> None:
        class Platform(Enum):
            TELEGRAM = "telegram"

        hook = make_pre_gateway_dispatch_hook(self._store())
        event = self._event(message_id="enum-platform")
        event.source.platform = Platform.TELEGRAM

        decision = hook(event)

        self.assertEqual(decision["action"], "rewrite")
        self.assertIn("location_token", decision["text"])
        self.assertIsNone(event.raw_message)

    def test_live_pin_initial_rewrites_and_edited_update_skips(self) -> None:
        hook = make_pre_gateway_dispatch_hook(self._store())
        initial = self._event(live_period=900, message_id="unique-live-1")
        edited = self._event(
            live_period=900,
            edit_date=object(),
            message_id="unique-live-1",
        )

        first = hook(initial)
        second = hook(edited)

        self.assertEqual(first["action"], "rewrite")
        self.assertIn("not supported", first["text"])
        self.assertEqual(second["action"], "skip")
        self.assertIsNone(initial.raw_message)
        self.assertIsNone(edited.raw_message)
        self.assertNotIn(str(ORIGIN_LATITUDE), first.get("text", ""))

    def test_edited_location_without_live_period_is_never_tokenized(self) -> None:
        store = self._store()
        hook = make_pre_gateway_dispatch_hook(store)
        edited_after_stop = self._event(
            live_period=None,
            edit_date=object(),
            message_id="stopped-live-edit",
        )

        decision = hook(edited_after_stop)

        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "edited_location_update")
        self.assertEqual(len(store), 0)
        self.assertIsNone(edited_after_stop.raw_message)
        self.assertNotIn(str(ORIGIN_LATITUDE), edited_after_stop.text)

    def test_location_parse_failure_is_coordinate_closed(self) -> None:
        hook = make_pre_gateway_dispatch_hook(self._store())
        event = self._event(
            latitude="not-a-number",
            longitude="also-bad",
            text=(
                "bad https://www.google.com/maps/search/?api=1&"
                "query=999999999,888888888"
            ),
        )

        decision = hook(event)

        self.assertEqual(decision["action"], "rewrite")
        self.assertNotIn("google.com", decision["text"])
        self.assertNotIn("999999999", decision["text"])
        self.assertIsNone(event.raw_message)

    def test_immutable_location_event_is_dropped_before_dispatch(self) -> None:
        class ImmutableEvent:
            def __init__(self) -> None:
                object.__setattr__(self, "_locked", False)
                self.text = (
                    f"Latitude: {ORIGIN_LATITUDE}\n"
                    f"Longitude: {ORIGIN_LONGITUDE}"
                )
                self.raw_message = SimpleNamespace(
                    location=SimpleNamespace(
                        latitude=ORIGIN_LATITUDE,
                        longitude=ORIGIN_LONGITUDE,
                        live_period=None,
                    ),
                    venue=None,
                    edit_date=None,
                )
                self.message_type = "location"
                self.source = SimpleNamespace(platform="telegram")
                self.message_id = "immutable-location"
                self.metadata = {}
                object.__setattr__(self, "_locked", True)

            def __setattr__(self, name: str, value: Any) -> None:
                if getattr(self, "_locked", False) and name in {
                    "text",
                    "raw_message",
                }:
                    raise AttributeError("immutable")
                object.__setattr__(self, name, value)

        called: List[bool] = []

        class BasePlatformAdapter:
            async def handle_message(self, event: Any) -> None:
                called.append(True)

        store = self._store()
        self.assertTrue(
            install_location_ingress_guard(
                store,
                base_class=BasePlatformAdapter,
            )
        )
        event = ImmutableEvent()

        result = asyncio.run(BasePlatformAdapter().handle_message(event))

        self.assertIsNone(result)
        self.assertEqual(called, [])
        self.assertIsNotNone(event.raw_message)
        self.assertEqual(len(store), 0)

    def test_busy_session_ingress_is_sanitized_before_original_handler(self) -> None:
        store = self._store()

        class BasePlatformAdapter:
            def __init__(self) -> None:
                self.received: List[Any] = []

            async def handle_message(self, event: Any) -> str:
                self.received.append(
                    (event.text, event.raw_message, dict(event.metadata))
                )
                return "queued_or_interrupted"

        original = BasePlatformAdapter.handle_message
        self.assertTrue(
            install_location_ingress_guard(store, base_class=BasePlatformAdapter)
        )
        protected = BasePlatformAdapter.handle_message
        self.assertIsNot(protected, original)
        self.assertTrue(
            install_location_ingress_guard(store, base_class=BasePlatformAdapter)
        )
        self.assertIs(BasePlatformAdapter.handle_message, protected)
        self.assertTrue(
            is_location_ingress_guard_installed(base_class=BasePlatformAdapter)
        )

        adapter = BasePlatformAdapter()
        event = self._event(message_id="busy-static")
        result = asyncio.run(adapter.handle_message(event))

        self.assertEqual(result, "queued_or_interrupted")
        self.assertEqual(len(adapter.received), 1)
        persisted_text, persisted_raw, persisted_metadata = adapter.received[0]
        self.assertIsNone(persisted_raw)
        self.assertTrue(persisted_metadata["korea_location_ingress_sanitized_v1"])
        self.assertRegex(persisted_text, r"location_token: loc_[A-Za-z0-9_-]+")
        self.assertNotIn(str(ORIGIN_LATITUDE), persisted_text)
        self.assertNotIn("google.com/maps", persisted_text)

        # The normal cold-session hook sees the sentinel and must not issue a
        # second token or overwrite the safe token text.
        token_count = len(store)
        self.assertIsNone(make_pre_gateway_dispatch_hook(store)(event))
        self.assertEqual(len(store), token_count)
        self.assertEqual(event.text, persisted_text)

        edited_live = self._event(
            live_period=600,
            edit_date=object(),
            message_id="busy-live-edit",
        )
        self.assertIsNone(asyncio.run(adapter.handle_message(edited_live)))
        self.assertEqual(len(adapter.received), 1)
        self.assertIsNone(edited_live.raw_message)

    def test_plugin_registration_rebinds_early_launcher_guard_store(self) -> None:
        launcher_store = self._store()
        plugin_store = self._store()

        class BasePlatformAdapter:
            async def handle_message(self, event: Any) -> str:
                return event.text

        self.assertTrue(
            install_location_ingress_guard(
                launcher_store,
                base_class=BasePlatformAdapter,
            )
        )
        protected = BasePlatformAdapter.handle_message
        self.assertTrue(
            install_location_ingress_guard(
                plugin_store,
                base_class=BasePlatformAdapter,
            )
        )
        self.assertIs(BasePlatformAdapter.handle_message, protected)

        event = self._event(message_id="post-plugin-registration")
        text = asyncio.run(BasePlatformAdapter().handle_message(event))
        token = re.search(r"loc_[A-Za-z0-9_-]{24,128}", text).group(0)

        self.assertEqual(len(launcher_store), 0)
        self.assertEqual(
            plugin_store.peek(token),
            (ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
        )

    def test_token_store_expires_consumes_and_evicts(self) -> None:
        now = [10.0]
        counter = iter(range(1000))
        store = LocationTokenStore(
            ttl_seconds=10,
            max_entries=2,
            clock=lambda: now[0],
            token_factory=lambda: f"{next(counter):024d}",
            schedule_expiry=False,
        )
        first = store.issue(1, 2)
        second = store.issue(3, 4)
        third = store.issue(5, 6)
        with self.assertRaises(LocationTokenError):
            store.peek(first)
        self.assertEqual(store.consume(second), (3.0, 4.0))
        with self.assertRaises(LocationTokenError):
            store.peek(second)
        now[0] = 21.0
        with self.assertRaises(LocationTokenError):
            store.peek(third)
        self.assertEqual(len(store), 0)

    def test_route_reservation_is_exclusive_and_commits_or_releases(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)

        first, latitude, longitude = store.reserve(token, purpose="route")
        self.assertEqual((latitude, longitude), (ORIGIN_LATITUDE, ORIGIN_LONGITUDE))
        with self.assertRaises(LocationTokenError) as in_use:
            store.reserve(token, purpose="route")
        self.assertEqual(in_use.exception.code, "LOCATION_TOKEN_IN_USE")

        self.assertTrue(store.release(first))
        self.assertEqual(store.peek(token), (ORIGIN_LATITUDE, ORIGIN_LONGITUDE))
        second, _latitude, _longitude = store.reserve(token, purpose="route")
        self.assertTrue(store.commit(second))
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_place_search_can_commit_once_then_route_can_consume(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)

        place, _latitude, _longitude = store.reserve(token, purpose="place_search")
        self.assertTrue(store.commit(place))
        with self.assertRaises(LocationTokenError) as reused:
            store.reserve(token, purpose="place_search")
        self.assertEqual(
            reused.exception.code,
            "LOCATION_TOKEN_PLACE_SEARCH_USED",
        )

        route, _latitude, _longitude = store.reserve(token, purpose="route")
        self.assertTrue(store.commit(route))
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_search_context_can_commit_once_without_blocking_place_or_route(
        self,
    ) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)

        context, _latitude, _longitude = store.reserve(
            token,
            purpose="search_context",
        )
        self.assertTrue(store.commit(context))
        with self.assertRaises(LocationTokenError) as reused:
            store.reserve(token, purpose="search_context")
        self.assertEqual(
            reused.exception.code,
            "LOCATION_TOKEN_SEARCH_CONTEXT_USED",
        )

        place, _latitude, _longitude = store.reserve(token, purpose="place_search")
        self.assertTrue(store.commit(place))
        route, _latitude, _longitude = store.reserve(token, purpose="route")
        self.assertTrue(store.commit(route))
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_stale_reservation_cannot_affect_reissued_identical_token(self) -> None:
        store = LocationTokenStore(
            ttl_seconds=LOCATION_TTL_SECONDS,
            token_factory=lambda: "x" * 24,
            schedule_expiry=False,
        )
        token = store.issue(1, 2)
        stale, _latitude, _longitude = store.reserve(token, purpose="route")
        self.assertTrue(store.revoke(token))
        self.assertEqual(store.issue(3, 4), token)

        self.assertFalse(store.release(stale))
        self.assertFalse(store.commit(stale))
        self.assertEqual(store.peek(token), (3.0, 4.0))

    def test_search_keeps_token_reverse_is_blocked_and_route_consumes_it(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)
        seen: List[Any] = []

        class FakeClient:
            def location_search_context(self, **kwargs: Any) -> Dict[str, Any]:
                seen.append(("context", kwargs))
                return {
                    "success": True,
                    "meta": {
                        "provider": "kakao_geocoding",
                        "provider_used": "kakao_geocoding",
                    },
                    "data": {
                        "locality": "서울특별시 중구 소공동",
                        "localized_query": "라멘 서울특별시 중구 소공동",
                    },
                }

            def place_search(self, **kwargs: Any) -> Dict[str, Any]:
                seen.append(("place", kwargs))
                return {
                    "success": True,
                    "meta": {"provider": "kakao_local", "provider_used": "kakao_local"},
                    "data": {"places": []},
                }

            def route(self, **kwargs: Any) -> Dict[str, Any]:
                seen.append(("route", kwargs))
                return {
                    "success": True,
                    "meta": {
                        "provider": "kakao_map_walking",
                        "provider_used": "kakao_map_walking",
                    },
                    "data": {"mode": "walking"},
                }

        with patch.object(korea, "LOCATION_TOKENS", store), patch.object(
            korea,
            "KoreaApiClient",
            return_value=FakeClient(),
        ):
            context_output = korea._location_search_context(
                {"query": "라멘", "location_token": token}
            )
            second_context_output = korea._location_search_context(
                {"query": "라멘", "location_token": token}
            )
            place_output = korea._place_search(
                {"query": "라멘", "location_token": token}
            )
            second_place_output = korea._place_search(
                {"query": "라멘", "location_token": token}
            )
            reverse_output = korea._reverse_geocode({"location_token": token})
            self.assertEqual(
                store.peek(token),
                (ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
            )
            route_output = korea._route(
                {
                    "mode": "walking",
                    "origin_location_token": token,
                    "destination_latitude": DESTINATION_LATITUDE,
                    "destination_longitude": DESTINATION_LONGITUDE,
                }
            )

        self.assertEqual([item[0] for item in seen], ["context", "place", "route"])
        self.assertEqual(seen[1][1]["radius_m"], 3000)
        self.assertEqual(seen[1][1]["sort"], "distance")
        for _name, kwargs in seen:
            actual_latitude = kwargs.get("latitude", kwargs.get("origin_latitude"))
            self.assertEqual(actual_latitude, ORIGIN_LATITUDE)
            self.assertEqual(
                kwargs.get("longitude", kwargs.get("origin_longitude")),
                ORIGIN_LONGITUDE,
            )
            self.assertNotIn("location_token", kwargs)
            self.assertNotIn("origin_location_token", kwargs)
        for output in (
            context_output,
            second_context_output,
            place_output,
            second_place_output,
            reverse_output,
            route_output,
        ):
            self.assertNotIn(str(ORIGIN_LATITUDE), output)
            self.assertNotIn(str(ORIGIN_LONGITUDE), output)
        self.assertIn("search_context_use_consumed_other_uses_retained", context_output)
        second_context_result = json.loads(second_context_output)
        self.assertFalse(second_context_result["success"])
        self.assertEqual(
            second_context_result["error"]["code"],
            "LOCATION_TOKEN_SEARCH_CONTEXT_USED",
        )
        self.assertIn("place_search_use_consumed_route_use_retained", place_output)
        second_place_result = json.loads(second_place_output)
        self.assertFalse(second_place_result["success"])
        self.assertEqual(
            second_place_result["error"]["code"],
            "LOCATION_TOKEN_PLACE_SEARCH_USED",
        )
        reverse_result = json.loads(reverse_output)
        self.assertFalse(reverse_result["success"])
        self.assertEqual(
            reverse_result["error"]["code"],
            "LOCATION_TOKEN_REVERSE_GEOCODE_BLOCKED",
        )
        self.assertEqual(reverse_result["meta"]["provider_used"], "privacy_policy")
        self.assertIn("consumed_after_successful_route", route_output)
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_search_context_requires_token_and_releases_it_after_failure(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)
        calls: List[Dict[str, Any]] = []

        class FakeClient:
            def location_search_context(self, **kwargs: Any) -> Dict[str, Any]:
                calls.append(kwargs)
                if len(calls) == 1:
                    raise KoreaApiError(
                        "PROVIDER_UNAVAILABLE",
                        "Kakao could not be reached.",
                        provider="kakao_geocoding",
                    )
                return {
                    "success": True,
                    "meta": {
                        "provider": "kakao_geocoding",
                        "provider_used": "kakao_geocoding",
                    },
                    "data": {
                        "locality": "서울특별시 중구 소공동",
                        "localized_query": "pharmacy 서울특별시 중구 소공동",
                    },
                }

        with patch.object(korea, "LOCATION_TOKENS", store), patch.object(
            korea,
            "KoreaApiClient",
            return_value=FakeClient(),
        ):
            raw_coordinates = json.loads(
                korea._location_search_context(
                    {
                        "query": "pharmacy",
                        "latitude": ORIGIN_LATITUDE,
                        "longitude": ORIGIN_LONGITUDE,
                    }
                )
            )
            failed = json.loads(
                korea._location_search_context(
                    {"query": "pharmacy", "location_token": token}
                )
            )
            self.assertEqual(
                store.peek(token),
                (ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
            )
            succeeded = json.loads(
                korea._location_search_context(
                    {"query": "pharmacy", "location_token": token}
                )
            )

        self.assertEqual(raw_coordinates["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(failed["error"]["code"], "PROVIDER_UNAVAILABLE")
        self.assertTrue(succeeded["success"])
        self.assertEqual(len(calls), 2)
        for output in (raw_coordinates, failed, succeeded):
            rendered = json.dumps(output, ensure_ascii=False)
            self.assertNotIn(str(ORIGIN_LATITUDE), rendered)
            self.assertNotIn(str(ORIGIN_LONGITUDE), rendered)

    def test_malformed_place_response_releases_token_for_retry(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)
        responses = iter(
            [
                {"documents": [None]},
                {"meta": {"is_end": True}, "documents": []},
            ]
        )
        client = KoreaApiClient(
            opener=lambda *_args, **_kwargs: JsonResponse(next(responses)),
            env_lookup=environment,
        )
        request = {"query": "라멘", "location_token": token}

        with patch.object(korea, "LOCATION_TOKENS", store), patch.object(
            korea,
            "KoreaApiClient",
            return_value=client,
        ):
            failed = json.loads(korea._place_search(request))
            self.assertEqual(
                store.peek(token),
                (ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
            )
            succeeded = json.loads(korea._place_search(request))
            reused = json.loads(korea._place_search(request))

        self.assertFalse(failed["success"])
        self.assertEqual(failed["error"]["code"], "INVALID_RESPONSE")
        self.assertTrue(succeeded["success"])
        self.assertFalse(reused["success"])
        self.assertEqual(
            reused["error"]["code"],
            "LOCATION_TOKEN_PLACE_SEARCH_USED",
        )

    def test_transient_route_failure_releases_token_for_one_retry(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)
        calls: List[Dict[str, Any]] = []

        class FakeClient:
            def route(self, **kwargs: Any) -> Dict[str, Any]:
                calls.append(kwargs)
                if len(calls) == 1:
                    raise KoreaApiError(
                        "PROVIDER_UNAVAILABLE",
                        "Kakao could not be reached.",
                        provider="kakao_map_walking",
                    )
                return {
                    "success": True,
                    "meta": {
                        "provider": "kakao_map_walking",
                        "provider_used": "kakao_map_walking",
                    },
                    "data": {"mode": "walking"},
                }

        request = {
            "mode": "walking",
            "origin_location_token": token,
            "destination_latitude": DESTINATION_LATITUDE,
            "destination_longitude": DESTINATION_LONGITUDE,
        }
        with patch.object(korea, "LOCATION_TOKENS", store), patch.object(
            korea,
            "KoreaApiClient",
            return_value=FakeClient(),
        ):
            failed = json.loads(korea._route(request))
            self.assertEqual(
                store.peek(token),
                (ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
            )
            succeeded_output = korea._route(request)

        self.assertFalse(failed["success"])
        self.assertEqual(failed["error"]["code"], "PROVIDER_UNAVAILABLE")
        self.assertIn("consumed_after_successful_route", succeeded_output)
        self.assertEqual(len(calls), 2)
        for output in (json.dumps(failed), succeeded_output):
            self.assertNotIn(str(ORIGIN_LATITUDE), output)
            self.assertNotIn(str(ORIGIN_LONGITUDE), output)
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_concurrent_second_route_cannot_obtain_reserved_coordinates(self) -> None:
        store = self._store()
        token = store.issue(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)
        entered_provider = threading.Event()
        finish_provider = threading.Event()
        calls: List[Dict[str, Any]] = []
        first_outputs: List[str] = []

        class BlockingClient:
            def route(self, **kwargs: Any) -> Dict[str, Any]:
                calls.append(kwargs)
                entered_provider.set()
                if not finish_provider.wait(2):
                    raise AssertionError("test did not release the provider")
                return {
                    "success": True,
                    "meta": {
                        "provider": "kakao_map_walking",
                        "provider_used": "kakao_map_walking",
                    },
                    "data": {"mode": "walking"},
                }

        request = {
            "mode": "walking",
            "origin_location_token": token,
            "destination_latitude": DESTINATION_LATITUDE,
            "destination_longitude": DESTINATION_LONGITUDE,
        }
        with patch.object(korea, "LOCATION_TOKENS", store), patch.object(
            korea,
            "KoreaApiClient",
            return_value=BlockingClient(),
        ):
            worker = threading.Thread(
                target=lambda: first_outputs.append(korea._route(request)),
                daemon=True,
            )
            worker.start()
            self.assertTrue(entered_provider.wait(1))
            second = json.loads(korea._route(request))
            finish_provider.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertFalse(second["success"])
        self.assertEqual(second["error"]["code"], "LOCATION_TOKEN_IN_USE")
        self.assertNotIn(str(ORIGIN_LATITUDE), json.dumps(second))
        self.assertEqual(len(first_outputs), 1)
        self.assertIn("consumed_after_successful_route", first_outputs[0])
        with self.assertRaises(LocationTokenError):
            store.peek(token)

    def test_location_schemas_enforce_privacy_specific_coordinate_contracts(self) -> None:
        context_parameters = LOCATION_SEARCH_CONTEXT["parameters"]
        place_parameters = KOREA_PLACE_SEARCH["parameters"]
        reverse_parameters = KOREA_REVERSE_GEOCODE["parameters"]
        route_parameters = KOREA_ROUTE["parameters"]

        self.assertEqual(context_parameters["required"], ["location_token"])
        self.assertNotIn("latitude", context_parameters["properties"])
        self.assertNotIn("longitude", context_parameters["properties"])
        self.assertIn("allOf", place_parameters)
        self.assertIn("oneOf", route_parameters)
        self.assertEqual(reverse_parameters["required"], ["latitude", "longitude"])
        self.assertNotIn("location_token", reverse_parameters["properties"])
        self.assertIn("origin_location_token", route_parameters["properties"])

        invalid_route = json.loads(
            korea._route(
                {
                    "mode": "walking",
                    "destination_latitude": DESTINATION_LATITUDE,
                    "destination_longitude": DESTINATION_LONGITUDE,
                }
            )
        )
        self.assertEqual(invalid_route["error"]["code"], "INVALID_ARGUMENT")

    def test_register_exposes_hook_and_all_tools(self) -> None:
        class Context:
            def __init__(self) -> None:
                self.hooks: List[Any] = []
                self.tools: List[Any] = []

            def register_hook(self, name: str, callback: Any) -> None:
                self.hooks.append((name, callback))

            def register_tool(self, **kwargs: Any) -> None:
                self.tools.append(kwargs)

        context = Context()
        with patch.object(
            korea,
            "install_location_ingress_guard",
            return_value=True,
        ):
            korea.register(context)

        self.assertEqual(context.hooks[0][0], "pre_gateway_dispatch")
        self.assertEqual(len(context.tools), 7)
        self.assertIn(
            "location_search_context",
            {item["name"] for item in context.tools},
        )
        self.assertIn("korea_route", {item["name"] for item in context.tools})

        with patch.object(
            korea,
            "install_location_ingress_guard",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "ingress privacy guard"):
                korea.register(Context())


if __name__ == "__main__":
    unittest.main()
