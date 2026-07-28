"""Tavily-primary web search with a local DDGS fallback and Tavily extract.

Hermes selects one web provider per request and does not natively retry a
second provider after a quota error. This provider keeps that policy local to
the project without patching the upstream image. URL extraction stays on
Tavily because DDGS only provides search results.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Callable, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

_DDGS_TIMEOUT_SECONDS = 30
_TAVILY_RETRYABLE_STATUS_CODES = {408, 429, 432, 433}


class RetryableTavilyError(RuntimeError):
    """A recoverable Tavily failure."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TavilyRequestError(RuntimeError):
    """A non-retryable Tavily configuration or request failure."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _default_key_lookup() -> str:
    return get_provider_env("TAVILY_API_KEY")


def _raise_for_tavily_error(response: Any, operation: str) -> None:
    """Preserve Tavily status details for search fallback and extract errors."""
    status = response.status_code
    content_type = response.headers.get("content-type", "").lower()
    response_prefix = response.text.lstrip()[:20].lower()
    if status == 403 and (
        "text/html" in content_type or response_prefix.startswith("<html")
    ):
        raise RetryableTavilyError(
            "Tavily edge/WAF rejected the request",
            status,
        )
    if status in _TAVILY_RETRYABLE_STATUS_CODES:
        reason = {
            408: "request timed out",
            429: "rate limit reached",
            432: "plan credits exhausted",
            433: "pay-as-you-go limit reached",
        }[status]
        raise RetryableTavilyError(reason, status)
    if status >= 500:
        raise RetryableTavilyError("Tavily service error", status)
    if status >= 400:
        if status in {401, 403}:
            message = "Tavily rejected the API key or access permissions"
        else:
            message = f"Tavily rejected the {operation} request"
        raise TavilyRequestError(message, status)


def _response_list(payload: Any, key: str) -> List[Any]:
    """Return a response list or classify a malformed 200 as retryable."""
    if not isinstance(payload, dict):
        raise RetryableTavilyError("Tavily returned unexpected JSON schema")
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise RetryableTavilyError("Tavily returned unexpected JSON schema")
    return value


def _search_tavily(api_key: str, query: str, limit: int) -> Dict[str, Any]:
    """Call Tavily Search while retaining the HTTP status for failover."""
    import httpx

    base_url = get_provider_env("TAVILY_BASE_URL") or "https://api.tavily.com"
    url = f"{base_url.rstrip('/')}/search"
    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": min(limit, 20),
        "include_raw_content": False,
        "include_images": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=60)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise RetryableTavilyError(
            f"temporary Tavily transport failure: {type(exc).__name__}"
        ) from exc

    _raise_for_tavily_error(response, "search")

    try:
        payload_out = response.json()
    except ValueError as exc:
        raise RetryableTavilyError("Tavily returned invalid JSON") from exc

    raw_results = _response_list(payload_out, "results")
    if any(not isinstance(item, dict) for item in raw_results):
        raise RetryableTavilyError("Tavily returned unexpected JSON schema")

    web_results = []
    for index, item in enumerate(raw_results):
        web_results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("content", ""),
                "position": index + 1,
            }
        )

    return {
        "success": True,
        "data": {"web": web_results},
        "meta": {"provider": "tavily", "fallback_used": False},
    }


def _extract_tavily(api_key: str, urls: List[str]) -> List[Dict[str, Any]]:
    """Extract up to 20 URLs through Tavily using current Bearer auth."""
    import httpx

    base_url = get_provider_env("TAVILY_BASE_URL") or "https://api.tavily.com"
    endpoint = f"{base_url.rstrip('/')}/extract"
    payload = {
        "urls": urls,
        "extract_depth": "basic",
        "include_images": False,
        "include_favicon": False,
        "format": "markdown",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=60,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise RetryableTavilyError(
            f"temporary Tavily transport failure: {type(exc).__name__}"
        ) from exc

    _raise_for_tavily_error(response, "extract")
    try:
        payload_out = response.json()
    except ValueError as exc:
        raise RetryableTavilyError("Tavily returned invalid JSON") from exc

    raw_results = _response_list(payload_out, "results")
    failed_results = _response_list(payload_out, "failed_results")
    failed_urls = _response_list(payload_out, "failed_urls")
    if any(not isinstance(item, dict) for item in raw_results):
        raise RetryableTavilyError("Tavily returned unexpected JSON schema")

    documents: List[Dict[str, Any]] = []
    for item in raw_results:
        url = str(item.get("url", ""))
        raw_content = item.get("raw_content", "") or item.get("content", "")
        documents.append(
            {
                "url": url,
                "title": item.get("title", ""),
                "content": raw_content,
                "raw_content": raw_content,
                "metadata": {
                    "sourceURL": url,
                    "title": item.get("title", ""),
                },
            }
        )

    for failed in failed_results:
        if isinstance(failed, dict):
            failed_url = str(failed.get("url", ""))
            error = str(failed.get("error", "extraction failed"))
        else:
            failed_url = str(failed)
            error = "extraction failed"
        documents.append(
            {
                "url": failed_url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": error,
                "metadata": {"sourceURL": failed_url},
            }
        )

    for failed_url in failed_urls:
        url = str(failed_url)
        documents.append(
            {
                "url": url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "extraction failed",
                "metadata": {"sourceURL": url},
            }
        )

    return documents


def _run_ddgs(query: str, limit: int) -> Dict[str, Any]:
    """Run the keyless fallback with a hard wall-clock timeout."""
    from ddgs import DDGS

    results = []
    with DDGS(timeout=10) as client:
        for index, item in enumerate(client.text(query, max_results=limit)):
            if index >= limit:
                break
            results.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("href") or item.get("url") or ""),
                    "description": str(item.get("body", "")),
                    "position": index + 1,
                }
            )
    return {"success": True, "data": {"web": results}}


def _search_ddgs(query: str, limit: int) -> Dict[str, Any]:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_run_ddgs, query, limit)
        return future.result(timeout=_DDGS_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        return {
            "success": False,
            "error": f"DDGS search timed out after {_DDGS_TIMEOUT_SECONDS}s",
        }
    except Exception as exc:  # noqa: BLE001 - ddgs has provider-specific errors
        return {
            "success": False,
            "error": f"DDGS search failed: {type(exc).__name__}: {exc}",
        }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class TavilyDdgsWebSearchProvider(WebSearchProvider):
    """Use Tavily first and DDGS for recoverable Tavily failures."""

    def __init__(
        self,
        *,
        key_lookup: Callable[[], str] = _default_key_lookup,
        tavily_search: Callable[[str, str, int], Dict[str, Any]] = _search_tavily,
        tavily_extract: Callable[[str, List[str]], List[Dict[str, Any]]] = (
            _extract_tavily
        ),
        ddgs_search: Callable[[str, int], Dict[str, Any]] = _search_ddgs,
    ) -> None:
        self._key_lookup = key_lookup
        self._tavily_search = tavily_search
        self._tavily_extract = tavily_extract
        self._ddgs_search = ddgs_search

    @property
    def name(self) -> str:
        return "tavily-ddgs"

    @property
    def display_name(self) -> str:
        return "Tavily → DDGS fallback"

    def is_available(self) -> bool:
        if self._key_lookup():
            return True
        try:
            import ddgs  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _fallback(
        self,
        query: str,
        limit: int,
        *,
        reason: str,
        status_code: Optional[int] = None,
    ) -> Dict[str, Any]:
        logger.warning("Tavily unavailable (%s); using DDGS fallback", reason)
        result = self._ddgs_search(query, limit)
        if result.get("success"):
            result["meta"] = {
                "provider": "ddgs",
                "fallback_used": True,
                "fallback_from": "tavily",
                "fallback_reason": reason,
            }
            if status_code is not None:
                result["meta"]["tavily_status_code"] = status_code
            return result

        return {
            "success": False,
            "error": (
                f"Tavily unavailable ({reason}); "
                f"DDGS fallback also failed: {result.get('error', 'unknown error')}"
            ),
        }

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}
        except ImportError:
            pass

        try:
            safe_limit = min(max(int(limit), 1), 100)
        except (TypeError, ValueError):
            safe_limit = 5

        api_key = self._key_lookup()
        if not api_key:
            return self._fallback(
                query,
                safe_limit,
                reason="TAVILY_API_KEY is not configured",
            )

        try:
            return self._tavily_search(api_key, query, safe_limit)
        except RetryableTavilyError as exc:
            return self._fallback(
                query,
                safe_limit,
                reason=str(exc),
                status_code=exc.status_code,
            )
        except TavilyRequestError as exc:
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
            return {"success": False, "error": f"{exc}{suffix}"}
        except Exception as exc:  # noqa: BLE001 - never break the tool loop
            logger.exception("Unexpected Tavily/DDGS provider error")
            return {
                "success": False,
                "error": f"Unexpected Tavily provider error: {type(exc).__name__}",
            }

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract URLs with Tavily; DDGS has no extraction capability."""
        safe_urls = [str(url).strip() for url in urls if str(url).strip()]
        if not safe_urls:
            return []

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": url, "title": "", "content": "", "error": "Interrupted"}
                    for url in safe_urls
                ]
        except ImportError:
            pass

        api_key = self._key_lookup()
        if not api_key:
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "error": "TAVILY_API_KEY is not configured",
                }
                for url in safe_urls
            ]

        documents: List[Dict[str, Any]] = []
        for offset in range(0, len(safe_urls), 20):
            batch = safe_urls[offset : offset + 20]
            try:
                documents.extend(self._tavily_extract(api_key, batch))
            except (RetryableTavilyError, TavilyRequestError) as exc:
                suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
                documents.extend(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"Tavily extract unavailable: {exc}{suffix}",
                    }
                    for url in batch
                )
            except Exception as exc:  # noqa: BLE001 - keep tool output typed
                logger.exception("Unexpected Tavily extract provider error")
                documents.extend(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": (
                            "Unexpected Tavily extract error: "
                            f"{type(exc).__name__}"
                        ),
                    }
                    for url in batch
                )
        return documents

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "Tavily search/extract · DDGS search fallback",
            "tag": "DDGS fallback applies to search; URL extraction stays on Tavily.",
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key",
                    "url": "https://app.tavily.com/home",
                }
            ],
        }
