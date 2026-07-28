"""Regression tests for the Tavily → DDGS composite provider."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from provider import (
    RetryableTavilyError,
    TavilyDdgsWebSearchProvider,
    TavilyRequestError,
    _extract_tavily,
    _search_tavily,
)


def _success(provider: str = "test"):
    return {
        "success": True,
        "data": {
            "web": [
                {
                    "title": f"{provider} result",
                    "url": "https://example.com",
                    "description": "ok",
                    "position": 1,
                }
            ]
        },
    }


class TavilyDdgsProviderTests(unittest.TestCase):
    def test_tavily_uses_bearer_header_and_not_json_key(self):
        response = Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"results":[]}',
        )
        response.json.return_value = {"results": []}

        with patch("httpx.post", return_value=response) as post:
            result = _search_tavily("tvly-test-token", "query", 5)

        self.assertTrue(result["success"])
        request = post.call_args
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer tvly-test-token",
        )
        self.assertNotIn("api_key", request.kwargs["json"])

    def test_tavily_extract_uses_bearer_header_and_normalizes_content(self):
        response = Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"results":[]}',
        )
        response.json.return_value = {
            "results": [
                {
                    "url": "https://example.com",
                    "raw_content": "# Example",
                }
            ]
        }

        with patch("httpx.post", return_value=response) as post:
            documents = _extract_tavily(
                "tvly-test-token",
                ["https://example.com"],
            )

        request = post.call_args
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer tvly-test-token",
        )
        self.assertNotIn("api_key", request.kwargs["json"])
        self.assertEqual(documents[0]["content"], "# Example")

    def test_html_403_is_retryable_but_json_403_is_not(self):
        html_response = Mock(
            status_code=403,
            headers={"content-type": "text/html"},
            text="<html>forbidden</html>",
        )
        with patch("httpx.post", return_value=html_response):
            with self.assertRaises(RetryableTavilyError):
                _search_tavily("tvly-test-token", "query", 5)

        json_response = Mock(
            status_code=403,
            headers={"content-type": "application/json"},
            text='{"detail":"forbidden"}',
        )
        with patch("httpx.post", return_value=json_response):
            with self.assertRaises(TavilyRequestError):
                _search_tavily("tvly-test-token", "query", 5)

    def test_timeout_service_and_transport_errors_are_retryable(self):
        for status in (408, 500, 503):
            with self.subTest(status=status):
                response = Mock(
                    status_code=status,
                    headers={"content-type": "application/json"},
                    text="{}",
                )
                with patch("httpx.post", return_value=response):
                    with self.assertRaises(RetryableTavilyError):
                        _search_tavily("tvly-test-token", "query", 5)

        import httpx

        request = httpx.Request("POST", "https://api.tavily.com/search")
        failure = httpx.ConnectError("offline", request=request)
        with patch("httpx.post", side_effect=failure):
            with self.assertRaises(RetryableTavilyError):
                _search_tavily("tvly-test-token", "query", 5)

    def test_malformed_success_schema_is_retryable(self):
        for malformed in ([], {"results": {}}, {"results": ["bad-item"]}):
            with self.subTest(malformed=malformed):
                response = Mock(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    text="{}",
                )
                response.json.return_value = malformed
                with patch("httpx.post", return_value=response):
                    with self.assertRaises(RetryableTavilyError):
                        _search_tavily("tvly-test-token", "query", 5)

    def test_primary_success_does_not_call_ddgs(self):
        calls = []
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=lambda *_: {
                **_success("tavily"),
                "meta": {"provider": "tavily", "fallback_used": False},
            },
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("query")

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["provider"], "tavily")
        self.assertEqual(calls, [])

    def test_quota_and_rate_statuses_fall_back(self):
        for status in (429, 432, 433):
            with self.subTest(status=status):
                calls = []

                def fail_tavily(*_):
                    raise RetryableTavilyError("quota", status)

                provider = TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "dummy",
                    tavily_search=fail_tavily,
                    ddgs_search=lambda *_: calls.append("ddgs") or _success("ddgs"),
                )

                result = provider.search("query")

                self.assertTrue(result["success"])
                self.assertEqual(result["meta"]["provider"], "ddgs")
                self.assertTrue(result["meta"]["fallback_used"])
                self.assertEqual(result["meta"]["tavily_status_code"], status)
                self.assertEqual(calls, ["ddgs"])

    def test_bad_key_is_visible_and_does_not_fall_back(self):
        calls = []

        def fail_tavily(*_):
            raise TavilyRequestError("bad key", 401)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=fail_tavily,
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("query")

        self.assertFalse(result["success"])
        self.assertIn("HTTP 401", result["error"])
        self.assertEqual(calls, [])

    def test_missing_key_uses_ddgs(self):
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "",
            tavily_search=lambda *_: self.fail("Tavily should not be called"),
            ddgs_search=lambda *_: _success("ddgs"),
        )

        result = provider.search("query")

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["provider"], "ddgs")

    def test_extract_without_key_returns_typed_error(self):
        provider = TavilyDdgsWebSearchProvider(key_lookup=lambda: "")

        result = provider.extract(["https://example.com"])

        self.assertEqual(len(result), 1)
        self.assertIn("TAVILY_API_KEY", result[0]["error"])

    def test_double_failure_is_reported(self):
        def fail_tavily(*_):
            raise RetryableTavilyError("quota", 432)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=fail_tavily,
            ddgs_search=lambda *_: {"success": False, "error": "ddgs down"},
        )

        result = provider.search("query")

        self.assertFalse(result["success"])
        self.assertIn("DDGS fallback also failed", result["error"])


if __name__ == "__main__":
    unittest.main()
