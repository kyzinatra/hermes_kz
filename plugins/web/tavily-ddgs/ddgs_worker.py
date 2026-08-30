#!/usr/bin/env python3
"""One-shot DDGS worker with a small JSON stdin/stdout contract."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict


_MAX_INPUT_BYTES = 64 * 1024
_MAX_QUERY_CHARS = 4_000  # Keep aligned with provider._MAX_SEARCH_QUERY_CHARS.
_MAX_FIELD_CHARS = 16_000
_MAX_RESULTS = 20  # Keep aligned with provider._MAX_SEARCH_RESULTS.


def _text(value: Any) -> str:
    return str(value or "")[:_MAX_FIELD_CHARS]


def _failure(message: str) -> Dict[str, Any]:
    return {"success": False, "error": message}


def run(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return _failure("DDGS worker received invalid input")
    query = str(payload.get("query") or "").strip()
    limit = payload.get("limit")
    if not query or len(query) > _MAX_QUERY_CHARS:
        return _failure("DDGS worker received an invalid query")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULTS:
        return _failure("DDGS worker received an invalid result limit")

    try:
        from ddgs import DDGS

        results = []
        with DDGS(timeout=10) as client:
            for index, item in enumerate(client.text(query, max_results=limit)):
                if index >= limit:
                    break
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "title": _text(item.get("title")),
                        "url": _text(item.get("href") or item.get("url")),
                        "description": _text(item.get("body")),
                        "position": index + 1,
                    }
                )
    except Exception as exc:  # noqa: BLE001 - isolate provider failures
        return _failure(f"DDGS search failed: {type(exc).__name__}")

    return {
        "success": True,
        "data": {"web": results},
        "provider": "ddgs",
        "actual_provider": "ddgs",
        "provider_used": "ddgs",
        "meta": {
            "provider": "ddgs",
            "actual_provider": "ddgs",
            "provider_used": "ddgs",
        },
    }


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        result = _failure("DDGS worker input was too large")
    else:
        try:
            result = run(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = _failure("DDGS worker received invalid JSON")
    sys.stdout.buffer.write(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
