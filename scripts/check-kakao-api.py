#!/usr/bin/env python3
"""Run sanitized live checks against every Kakao endpoint used by the plugin.

The REST key is read once from stdin, kept in process memory, and never printed
or written to disk. Output contains only check names, provider labels, counts,
and typed errors from the plugin.
"""

from __future__ import annotations

import json
import getpass
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from korea.client import KoreaApiClient, KoreaApiError  # noqa: E402


ORIGIN_LATITUDE = 37.394776627382875
ORIGIN_LONGITUDE = 127.11119669891646
DESTINATION_LATITUDE = 37.4199323570413
DESTINATION_LONGITUDE = 127.12629039752096


def _summary(name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    summary: Dict[str, Any] = {
        "name": name,
        "success": result.get("success") is True,
        "provider_used": (result.get("meta") or {}).get("provider_used"),
    }
    if isinstance(data.get("places"), list):
        summary["result_count"] = len(data["places"])
    elif isinstance(data.get("results"), list):
        summary["result_count"] = len(data["results"])
    elif isinstance(data.get("routes"), list):
        summary["result_count"] = len(data["routes"])
    elif data.get("distance_m") is not None:
        summary["route_summary_present"] = True
    return summary


def main() -> int:
    api_key = (
        getpass.getpass("Kakao REST API key (not stored): ").strip()
        if sys.stdin.isatty()
        else sys.stdin.readline().strip()
    )
    if not api_key:
        print(json.dumps({"success": False, "error": "missing_key_on_stdin"}))
        return 2

    client = KoreaApiClient(
        env_lookup=lambda name: api_key if name == "KAKAO_REST_API_KEY" else None,
        timeout=20,
    )
    checks: List[tuple[str, Callable[[], Dict[str, Any]]]] = [
        (
            "local_search",
            lambda: client.place_search(
                query="판교역",
                sort="accuracy",
                limit=1,
            ),
        ),
        (
            "geocode",
            lambda: client.geocode("경기도 성남시 분당구 판교역로 166", limit=1),
        ),
        (
            "reverse_geocode",
            lambda: client.reverse_geocode(ORIGIN_LATITUDE, ORIGIN_LONGITUDE),
        ),
        (
            "walking_route",
            lambda: client.route(
                mode="walking",
                origin_latitude=ORIGIN_LATITUDE,
                origin_longitude=ORIGIN_LONGITUDE,
                destination_latitude=DESTINATION_LATITUDE,
                destination_longitude=DESTINATION_LONGITUDE,
            ),
        ),
        (
            "transit_route",
            lambda: client.route(
                mode="transit",
                origin_latitude=ORIGIN_LATITUDE,
                origin_longitude=ORIGIN_LONGITUDE,
                destination_latitude=DESTINATION_LATITUDE,
                destination_longitude=DESTINATION_LONGITUDE,
                max_routes=1,
            ),
        ),
        (
            "car_route",
            lambda: client.route(
                mode="car",
                origin_latitude=ORIGIN_LATITUDE,
                origin_longitude=ORIGIN_LONGITUDE,
                destination_latitude=DESTINATION_LATITUDE,
                destination_longitude=DESTINATION_LONGITUDE,
                max_routes=1,
            ),
        ),
    ]

    reports: List[Dict[str, Any]] = []
    for name, check in checks:
        try:
            reports.append(_summary(name, check()))
        except KoreaApiError as exc:
            reports.append(
                {
                    "name": name,
                    "success": False,
                    "provider_used": exc.provider,
                    "error_code": exc.code,
                    "status_code": exc.status_code,
                    "message": exc.message,
                }
            )
        except Exception as exc:  # noqa: BLE001 - never serialize exception text
            reports.append(
                {
                    "name": name,
                    "success": False,
                    "error_code": type(exc).__name__,
                }
            )

    print(
        json.dumps(
            {
                "success": all(report["success"] for report in reports),
                "checks": reports,
            },
            ensure_ascii=False,
        )
    )
    return 0 if all(report["success"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
