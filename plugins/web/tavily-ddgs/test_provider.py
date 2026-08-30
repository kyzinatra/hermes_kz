"""Regression tests for the cost-aware DDGS/Tavily router."""

from __future__ import annotations

import os
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

# Keep the plugin tests runnable from this standalone deployment checkout; the
# real classes/modules are used unchanged inside the Hermes image.
try:
    from agent.web_search_provider import WebSearchProvider as _HermesProvider
except ModuleNotFoundError:
    agent_module = types.ModuleType("agent")
    web_provider_module = types.ModuleType("agent.web_search_provider")

    class _HermesProvider:  # pragma: no cover - test bootstrap only
        pass

    web_provider_module.WebSearchProvider = _HermesProvider
    web_provider_module.get_provider_env = lambda _name: ""
    sys.modules.setdefault("agent", agent_module)
    sys.modules["agent.web_search_provider"] = web_provider_module

try:
    import httpx as _httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_module = types.ModuleType("httpx")

    class _TransportError(Exception):
        pass

    class _TimeoutException(_TransportError):
        pass

    class _ConnectError(_TransportError):
        def __init__(self, message, *args, **kwargs):
            super().__init__(message)

    class _Request:
        def __init__(self, method, url):
            self.method = method
            self.url = url

    httpx_module.TransportError = _TransportError
    httpx_module.TimeoutException = _TimeoutException
    httpx_module.ConnectError = _ConnectError
    httpx_module.Request = _Request
    httpx_module.post = lambda *args, **kwargs: None
    sys.modules["httpx"] = httpx_module

from provider import (
    RetryableTavilyError,
    TavilyDdgsWebSearchProvider,
    TavilyRequestError,
    _TAVILY_CONTENT_MARKER,
    _assess_ddgs_quality,
    _classify_query,
    _extract_tavily,
    _search_ddgs,
    _search_tavily,
)


def _success(provider: str = "test", query: str = "query", count: int = 3):
    return {
        "success": True,
        "data": {
            "web": [
                {
                    "title": f"{provider} result {index}: {query}",
                    "url": f"https://example.com/{provider}/{index}",
                    "description": f"Relevant information about {query}",
                    "position": index,
                }
                for index in range(1, count + 1)
            ]
        },
    }


class TavilyHttpTests(unittest.TestCase):
    def test_tavily_uses_bearer_header_and_requested_depth(self):
        response = Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"results":[]}',
        )
        response.json.return_value = {"results": []}

        with patch.dict(
            os.environ,
            {"TAVILY_BASE_URL": "https://credential-thief.invalid"},
        ):
            with patch("httpx.post", return_value=response) as post:
                result = _search_tavily(
                    "tvly-test-token",
                    "query",
                    5,
                    "basic",
                )

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "tavily")
        request = post.call_args
        self.assertEqual(request.args[0], "https://api.tavily.com/search")
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer tvly-test-token",
        )
        self.assertNotIn("api_key", request.kwargs["json"])
        self.assertEqual(request.kwargs["json"]["search_depth"], "basic")

    def test_tavily_extract_has_model_visible_provider_marker(self):
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

        with patch.dict(
            os.environ,
            {"TAVILY_BASE_URL": "https://credential-thief.invalid"},
        ):
            with patch("httpx.post", return_value=response) as post:
                documents = _extract_tavily(
                    "tvly-test-token",
                    ["https://example.com"],
                )

        request = post.call_args
        self.assertEqual(request.args[0], "https://api.tavily.com/extract")
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            "Bearer tvly-test-token",
        )
        self.assertNotIn("api_key", request.kwargs["json"])
        self.assertEqual(request.kwargs["json"]["extract_depth"], "advanced")
        self.assertTrue(documents[0]["content"].startswith(_TAVILY_CONTENT_MARKER))
        self.assertIn("# Example", documents[0]["content"])
        self.assertEqual(documents[0]["actual_provider"], "tavily")
        self.assertEqual(documents[0]["provider_used"], "tavily")
        self.assertEqual(documents[0]["metadata"]["provider"], "tavily")

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


class RouterDecisionTests(unittest.TestCase):
    def test_routine_intents_stay_on_ddgs(self):
        cases = (
            ("Кто написал Войну и мир?", "fact"),
            ("Последние новости Сеула сегодня", "news"),
            ("httpx timeout API documentation", "technical"),
            ("compare iPhone and Samsung", "fact"),
            (
                "Найди официальный сайт музея, часы работы, цену билета "
                "и адрес на сегодня",
                "news",
            ),
            (
                "Подскажи кафе рядом, адрес, часы работы, средний чек и "
                "как туда попасть",
                "fact",
            ),
        )
        for query, query_type in cases:
            with self.subTest(query=query):
                route = _classify_query(query)
                self.assertEqual(route.primary_provider, "ddgs")
                self.assertEqual(route.query_type, query_type)

    def test_explicit_complex_research_uses_tavily(self):
        route = _classify_query(
            "Проведи глубокое исследование рынка доставки в Корее, "
            "сравни тренды, отчёты и источники за пять лет"
        )

        self.assertEqual(route.primary_provider, "tavily")
        self.assertEqual(route.query_type, "research")

    def test_strong_research_overrides_incidental_api_keyword(self):
        route = _classify_query(
            "Deep comparative research into the OAuth API security landscape"
        )

        self.assertEqual(route.primary_provider, "tavily")
        self.assertEqual(route.query_type, "research")

    def test_direct_url_routes_to_local_browser(self):
        route = _classify_query("https://example.com/dynamic")

        self.assertEqual(route.primary_provider, "local-browser")
        self.assertEqual(route.query_type, "direct_url")

    def test_embedded_url_with_read_intent_routes_to_local_browser(self):
        route = _classify_query("прочитай https://example.com/dynamic, пожалуйста")

        self.assertEqual(route.primary_provider, "local-browser")
        self.assertEqual(route.query_type, "direct_url")

    def test_quality_requires_unique_relevant_results_with_snippets(self):
        good = _assess_ddgs_quality(_success("ddgs", "ramen", 3), "ramen", 5)
        poor = _assess_ddgs_quality(
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Unrelated",
                            "url": "https://example.com/same",
                            "description": "",
                        },
                        {
                            "title": "Duplicate",
                            "url": "https://example.com/same#fragment",
                            "description": "",
                        },
                    ]
                },
            },
            "ramen",
            5,
        )

        self.assertTrue(good.acceptable)
        self.assertFalse(poor.acceptable)
        self.assertEqual(poor.unique_results, 1)
        self.assertIn("low lexical relevance", " ".join(poor.reasons))


class TavilyDdgsProviderTests(unittest.TestCase):
    def test_routine_success_uses_ddgs_without_touching_tavily(self):
        calls = []
        query = "кто написал Войну и мир"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key") or "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs") or _success("ddgs", query),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertEqual(result["provider_used"], "ddgs")
        self.assertEqual(result["meta"]["actual_provider"], "ddgs")
        self.assertEqual(result["meta"]["provider_used"], "ddgs")
        self.assertEqual(result["meta"]["query_type"], "fact")
        self.assertTrue(result["meta"]["tavily_spend_protected"])
        self.assertEqual(calls, ["ddgs"])

    def test_technical_query_stays_on_ddgs(self):
        calls = []
        query = "Python httpx timeout API documentation"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: _success("ddgs", query),
        )

        result = provider.search(query)

        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertEqual(result["meta"]["query_type"], "technical")
        self.assertEqual(calls, [])

    def test_complex_research_calls_tavily_advanced_first(self):
        calls = []
        query = "Deep research and comprehensive analysis of the EV battery market"

        def tavily(*args):
            calls.append(args)
            return _success("tavily", query)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=tavily,
            ddgs_search=lambda *_: self.fail("DDGS should not be called"),
        )

        result = provider.search(query)

        self.assertEqual(result["actual_provider"], "tavily")
        self.assertEqual(result["meta"]["tavily_trigger"], "complex_research")
        self.assertEqual(calls[0][3], "advanced")

    def test_low_quality_ddgs_escalates_to_basic_tavily(self):
        calls = []
        query = "ramen nearby"

        def tavily(*args):
            calls.append(args)
            return _success("tavily", query)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=tavily,
            ddgs_search=lambda *_: _success("ddgs", query, 1),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "tavily")
        self.assertEqual(result["meta"]["fallback_from"], "ddgs")
        self.assertEqual(result["meta"]["tavily_trigger"], "ddgs_quality")
        self.assertFalse(result["meta"]["ddgs_quality"]["acceptable"])
        self.assertEqual(calls[0][3], "basic")

    def test_ddgs_failure_is_a_quality_escalation_not_only_exception_fallback(self):
        query = "ordinary fact"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=lambda *_: _success("tavily", query),
            ddgs_search=lambda *_: {"success": False, "error": "ddgs down"},
        )

        result = provider.search(query)

        self.assertEqual(result["actual_provider"], "tavily")
        self.assertEqual(result["meta"]["fallback_from"], "ddgs")
        self.assertIn("ddgs down", result["meta"]["fallback_reason"])

    def test_low_quality_ddgs_without_key_is_returned_as_degraded(self):
        query = "ramen nearby"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "",
            tavily_search=lambda *_: self.fail("Tavily should not be called"),
            ddgs_search=lambda *_: _success("ddgs", query, 1),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertTrue(result["meta"]["degraded"])
        self.assertEqual(
            result["meta"]["tavily_escalation"],
            "skipped_missing_tavily_key",
        )

    def test_direct_url_is_typed_success_and_uses_no_external_provider(self):
        calls = []
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key") or "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("https://example.com/app")

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "none")
        self.assertEqual(result["provider_used"], "none")
        self.assertFalse(result["meta"]["external_provider_used"])
        self.assertTrue(result["meta"]["local_browser_recommended"])
        self.assertEqual(result["data"]["web"][0]["url"], "https://example.com/app")
        self.assertEqual(calls, [])

    def test_embedded_url_read_intent_uses_no_search_provider(self):
        calls = []
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key") or "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("прочитай https://example.com/app, пожалуйста")

        self.assertTrue(result["success"])
        self.assertEqual(result["provider_used"], "none")
        self.assertEqual(result["data"]["web"][0]["url"], "https://example.com/app")
        self.assertTrue(result["meta"]["local_browser_recommended"])
        self.assertEqual(calls, [])

    def test_url_containing_lookup_cannot_quality_escalate_to_tavily(self):
        calls = []
        query = "reviews for https://example.com/product"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key") or "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs") or _success("ddgs", query, 1),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["provider_used"], "ddgs")
        self.assertEqual(result["meta"]["tavily_escalation"], "blocked_url_query")
        self.assertTrue(result["meta"]["tavily_spend_protected"])
        self.assertEqual(calls, ["ddgs"])

    def test_quota_and_rate_errors_on_research_fall_back_to_ddgs(self):
        query = "Comprehensive research and analysis of housing market reports"
        for status in (429, 432, 433):
            with self.subTest(status=status):
                calls = []

                def fail_tavily(*_):
                    raise RetryableTavilyError("quota", status)

                provider = TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "dummy",
                    tavily_search=fail_tavily,
                    ddgs_search=lambda *_: calls.append("ddgs")
                    or _success("ddgs", query),
                )

                result = provider.search(query)

                self.assertTrue(result["success"])
                self.assertEqual(result["actual_provider"], "ddgs")
                self.assertTrue(result["meta"]["fallback_used"])
                self.assertTrue(result["meta"]["tavily_attempted"])
                self.assertEqual(result["meta"]["tavily_status_code"], status)
                self.assertEqual(calls, ["ddgs"])

    def test_missing_key_on_research_uses_ddgs_without_tavily_attempt(self):
        query = "Deep research and comprehensive analysis of battery reports"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "",
            tavily_search=lambda *_: self.fail("Tavily should not be called"),
            ddgs_search=lambda *_: _success("ddgs", query),
        )

        result = provider.search(query)

        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertFalse(result["meta"]["tavily_attempted"])

    def test_bad_key_is_visible_and_research_falls_back_to_ddgs(self):
        calls = []
        query = "Deep research and comprehensive analysis of battery reports"

        def fail_tavily(*_):
            raise TavilyRequestError("bad key", 401)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=fail_tavily,
            ddgs_search=lambda *_: calls.append("ddgs") or _success(
                "ddgs", query
            ),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertEqual(result["provider_used"], "ddgs")
        self.assertEqual(result["meta"]["tavily_status_code"], 401)
        self.assertIn("bad key", result["meta"]["fallback_reason"])
        self.assertEqual(calls, ["ddgs"])

    def test_failed_tavily_escalation_preserves_partial_ddgs_output(self):
        query = "ramen nearby"

        def fail_tavily(*_):
            raise TavilyRequestError("bad key", 401)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=fail_tavily,
            ddgs_search=lambda *_: _success("ddgs", query, 1),
        )

        result = provider.search(query)

        self.assertTrue(result["success"])
        self.assertEqual(result["actual_provider"], "ddgs")
        self.assertEqual(result["meta"]["tavily_escalation"], "failed")
        self.assertTrue(result["meta"]["tavily_attempted"])
        self.assertEqual(result["meta"]["tavily_status_code"], 401)

    def test_double_failure_is_reported(self):
        query = "ordinary fact"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_search=lambda *_: (_ for _ in ()).throw(
                RetryableTavilyError("quota", 432)
            ),
            ddgs_search=lambda *_: {"success": False, "error": "ddgs down"},
        )

        result = provider.search(query)

        self.assertFalse(result["success"])
        self.assertIn("also failed", result["error"])

    def test_empty_query_never_calls_a_provider(self):
        calls = []
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key"),
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("   ")

        self.assertFalse(result["success"])
        self.assertEqual(result["actual_provider"], "none")
        self.assertEqual(result["provider_used"], "none")
        self.assertEqual(calls, [])

    def test_large_hermes_result_bucket_is_clamped_before_ddgs(self):
        calls = []
        query = "ordinary fact"

        def ddgs(_query, limit):
            calls.append(("ddgs", limit))
            return _success("ddgs", query, limit)

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append(("key", None)) or "dummy",
            tavily_search=lambda *_: calls.append(("tavily", None)),
            ddgs_search=ddgs,
        )

        result = provider.search(query, limit=50)

        self.assertTrue(result["success"])
        self.assertEqual(result["provider_used"], "ddgs")
        self.assertEqual(calls, [("ddgs", 20)])

    def test_overlong_query_calls_no_provider(self):
        calls = []
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: calls.append("key") or "dummy",
            tavily_search=lambda *_: calls.append("tavily"),
            ddgs_search=lambda *_: calls.append("ddgs"),
        )

        result = provider.search("x" * 4_001)

        self.assertFalse(result["success"])
        self.assertEqual(result["provider_used"], "none")
        self.assertEqual(result["meta"]["attempted_providers"], [])
        self.assertEqual(calls, [])

    def test_provider_used_contract_covers_success_degraded_and_error(self):
        routine_query = "ordinary fact"
        research_query = "Deep research and comprehensive market analysis"
        cases = (
            (
                TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "dummy",
                    ddgs_search=lambda *_: _success("ddgs", routine_query),
                ).search(routine_query),
                "ddgs",
            ),
            (
                TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "dummy",
                    tavily_search=lambda *_: _success("tavily", research_query),
                ).search(research_query),
                "tavily",
            ),
            (
                TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "",
                    ddgs_search=lambda *_: _success("ddgs", routine_query, 1),
                ).search(routine_query),
                "ddgs",
            ),
            (
                TavilyDdgsWebSearchProvider(
                    key_lookup=lambda: "dummy",
                    tavily_search=lambda *_: (_ for _ in ()).throw(
                        TavilyRequestError("bad key", 401)
                    ),
                    ddgs_search=lambda *_: {
                        "success": False,
                        "error": "ddgs unavailable",
                    },
                ).search(research_query),
                "none",
            ),
            (
                TavilyDdgsWebSearchProvider().search("https://example.com"),
                "none",
            ),
        )

        for result, expected in cases:
            with self.subTest(expected=expected, result=result):
                self.assertEqual(result["provider_used"], expected)
                self.assertEqual(result["meta"]["provider_used"], expected)


class ExtractContractTests(unittest.TestCase):
    def test_extract_without_key_recommends_local_browser(self):
        provider = TavilyDdgsWebSearchProvider(key_lookup=lambda: "")

        result = provider.extract(["https://example.com"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["actual_provider"], "none")
        self.assertIn("TAVILY_API_KEY", result[0]["error"])
        self.assertIn("local browser", result[0]["error"])

    def test_injected_extract_is_labeled_before_hermes_trimming(self):
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: [
                {
                    "url": "https://example.com",
                    "title": "Example",
                    "content": "Page body",
                }
            ],
        )

        result = provider.extract(["https://example.com"])
        # Mirrors Hermes v2026.8.27's final projection. Provider/metadata are
        # dropped, therefore the compact content marker is part of the contract.
        trimmed = {
            "url": result[0].get("url", ""),
            "title": result[0].get("title", ""),
            "content": result[0].get("content", ""),
            "error": result[0].get("error"),
        }

        self.assertEqual(result[0]["actual_provider"], "tavily")
        self.assertEqual(result[0]["provider_used"], "tavily")
        self.assertEqual(result[0]["metadata"]["provider"], "tavily")
        self.assertTrue(trimmed["content"].startswith(_TAVILY_CONTENT_MARKER))
        self.assertIn("Page body", trimmed["content"])

    def test_extract_rejects_more_than_twenty_without_spend(self):
        calls = []

        def extract(_key, urls):
            calls.append(list(urls))
            return [
                {"url": url, "title": "", "content": f"body {url}"}
                for url in urls
            ]

        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=extract,
        )
        urls = [f"https://example.com/{index}" for index in range(45)]

        result = provider.extract(urls)

        self.assertEqual(calls, [])
        self.assertEqual(len(result), 45)
        self.assertTrue(all(item["provider_used"] == "none" for item in result))
        self.assertTrue(all("rejected before Tavily" in item["error"] for item in result))

    def test_extract_restores_input_order_for_mixed_results(self):
        bad_url = "https://example.com/bad"
        good_url = "https://example.com/good"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: [
                {"url": good_url, "title": "Good", "content": "body"},
                {"url": bad_url, "error": "blocked", "content": ""},
            ],
        )

        result = provider.extract([bad_url, good_url])

        self.assertEqual([item["url"] for item in result], [bad_url, good_url])
        self.assertIn("blocked", result[0]["error"])
        self.assertIn("body", result[1]["content"])

    def test_extract_preserves_duplicate_inputs_from_one_vendor_result(self):
        url = "https://example.com/duplicate"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: [
                {"url": url, "title": "One", "content": "body"}
            ],
        )

        result = provider.extract([url, url])

        self.assertEqual(len(result), 2)
        self.assertEqual([item["url"] for item in result], [url, url])
        self.assertTrue(all("body" in item["content"] for item in result))

    def test_extract_fills_missing_result_and_prefers_success_over_failure(self):
        found_url = "https://example.com/found"
        missing_url = "https://example.com/missing"
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: [
                {"url": found_url, "error": "duplicate failure"},
                {"url": found_url, "content": "usable content"},
            ],
        )

        result = provider.extract([found_url, missing_url])

        self.assertEqual(len(result), 2)
        self.assertNotIn("error", result[0])
        self.assertIn("usable content", result[0]["content"])
        self.assertEqual(result[1]["url"], missing_url)
        self.assertIn("no result", result[1]["error"])

    def test_extract_failure_has_visible_provider_in_error(self):
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: (_ for _ in ()).throw(
                RetryableTavilyError("quota", 432)
            ),
        )

        result = provider.extract(["https://example.com"])

        self.assertEqual(result[0]["actual_provider"], "tavily")
        self.assertIn(_TAVILY_CONTENT_MARKER, result[0]["error"])
        self.assertIn("HTTP 432", result[0]["error"])

    def test_empty_tavily_content_is_not_reported_as_a_marker_only_success(self):
        provider = TavilyDdgsWebSearchProvider(
            key_lookup=lambda: "dummy",
            tavily_extract=lambda *_: [
                {"url": "https://example.com", "title": "", "content": ""}
            ],
        )

        result = provider.extract(["https://example.com"])

        self.assertEqual(result[0]["content"], "")
        self.assertIn(_TAVILY_CONTENT_MARKER, result[0]["error"])
        self.assertIn("empty content", result[0]["error"])


class DdgsWorkerContractTests(unittest.TestCase):
    def test_repeated_timeouts_use_killable_subprocesses(self):
        timeout = subprocess.TimeoutExpired(cmd=["python", "worker"], timeout=30)
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "telegram-secret",
                "KAKAO_REST_API_KEY": "kakao-secret",
                "TAVILY_API_KEY": "tavily-secret",
            },
        ):
            with patch("provider.subprocess.run", side_effect=timeout) as run:
                results = [_search_ddgs("ordinary query", 5) for _ in range(20)]

        self.assertEqual(run.call_count, 20)
        self.assertTrue(all(not result["success"] for result in results))
        self.assertTrue(all("timed out" in result["error"] for result in results))
        for call in run.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertEqual(call.args[0][1], "-I")
            self.assertTrue(call.args[0][2].endswith("ddgs_worker.py"))
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertNotIn("shell", call.kwargs)
            for secret_name in (
                "TELEGRAM_BOT_TOKEN",
                "KAKAO_REST_API_KEY",
                "TAVILY_API_KEY",
            ):
                self.assertNotIn(secret_name, call.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
