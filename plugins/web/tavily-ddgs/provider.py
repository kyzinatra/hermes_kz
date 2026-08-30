"""Cost-aware DDGS/Tavily web routing and Tavily extraction.

Search is deliberately DDGS-first for facts, documentation, news, and
technical lookups.  Tavily is used only for clearly research-shaped queries or
when the DDGS response fails an objective quality check.  This avoids silently
spending Tavily credits on routine searches while retaining a higher-quality
research path.

Hermes' local browser is a separate, task-scoped toolset; a web provider does
not receive the browser task/session id.  Consequently direct URLs are never
silently sent to Tavily by :meth:`search`: the result tells the agent to use
``browser_navigate``/``browser_snapshot``.  Explicit ``web_extract`` calls stay
on Tavily because DDGS has no extraction capability.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

_DDGS_TIMEOUT_SECONDS = 30
_DDGS_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_SEARCH_QUERY_CHARS = 4_000
_MAX_SEARCH_RESULTS = 20
_MAX_EXTRACT_URLS = 20
_TAVILY_API_BASE_URL = "https://api.tavily.com"
_TAVILY_RETRYABLE_STATUS_CODES = {408, 429, 432, 433}
_TAVILY_CONTENT_MARKER = "[source_provider: tavily]"

_DIRECT_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s<>\[\]\"']+", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)

# The DDGS package is less trusted than the credentialed gateway process.  Its
# child receives only process/TLS basics, never provider keys or bot tokens.
_DDGS_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

_DIRECT_PAGE_PATTERNS: Tuple[str, ...] = (
    r"\b(?:read|open|extract|check|visit|inspect|summari[sz]e)\w*",
    r"\b(?:прочитай|открой|извлеки|проверь|посмотри|перейди|прочти)\w*",
)

# Documentation/technical queries stay on DDGS even when they contain words
# such as "compare" or "analyse".  This is both cheaper and generally produces
# better links to primary docs, repositories, and issue trackers.
_TECHNICAL_PATTERNS: Tuple[str, ...] = (
    r"\bapi\b",
    r"\bsdk\b",
    r"\brfc\s*\d*\b",
    r"\b(?:docs?|documentation|manual|reference)\b",
    r"\b(?:github|gitlab|stackoverflow)\b",
    r"\b(?:error|exception|traceback|stack\s*trace|bug)\b",
    r"\b(?:install|configure|configuration|setup|upgrade|migration)\b",
    r"\b(?:python|javascript|typescript|node\.?js|rust|golang|java|kotlin)\b",
    r"\b(?:sql|postgres(?:ql)?|mysql|redis|docker|kubernetes|linux)\b",
    r"\b(?:httpx?|oauth|json|yaml|regex|cli|mcp)\b",
    r"\b(?:документац|справочник|руководств|мануал)\w*",
    r"\b(?:ошибк|исключен|трейсбек|стек\s+вызов)\w*",
    r"\b(?:установ|настро|конфигурац|обновлен|миграц)\w*",
    r"\b(?:код|библиотек|фреймворк|репозитор)\w*",
)

_NEWS_PATTERNS: Tuple[str, ...] = (
    r"\b(?:news|headline|breaking|today|yesterday|latest|current)\b",
    r"\b(?:новост|сегодня|вчера|последн(?:ие|яя)|свеж(?:ие|ая)|текущ)\w*",
    r"\b(?:что\s+произошло|what\s+happened)\b",
)

_STRONG_RESEARCH_PATTERNS: Tuple[str, ...] = (
    r"\b(?:deep|comprehensive|in[- ]depth)(?:\s+\w+){0,2}\s+(?:research|analysis|review)\b",
    r"\b(?:systematic|literature)\s+review\b",
    r"\bdue\s+diligence\b",
    r"\bmarket\s+(?:research|analysis|landscape)\b",
    r"\b(?:глубок|комплексн|подробн)\w*(?:\s+[^\W_]+){0,2}\s+(?:исследован|анализ|обзор)\w*",
    r"\bсравнительн\w*\s+анализ\w*",
    r"\bобзор\w*\s+рынк\w*",
    r"\bсистематическ\w*\s+обзор\w*",
)

_RESEARCH_PATTERNS: Tuple[str, ...] = (
    r"\b(?:research|investigate|analyse|analyze|evaluate|assess|synthesi[sz]e)\w*",
    r"\b(?:compare|comparison|versus|alternatives?|trade[- ]?offs?)\b",
    r"\b(?:evidence|studies|reports|sources|citations|methodology)\b",
    r"\b(?:trend|forecast|scenario|market|industry|landscape)\w*",
    r"\b(?:исслед|проанализ|синтезир|оцени|сравни|сопостав|изуч)\w*",
    r"\b(?:источник|исследован|отч[её]т|доказательств|методолог)\w*",
    r"\b(?:тренд|прогноз|сценари|рынок|отрасл)\w*",
    r"\b(?:плюс\w*\s+и\s+минус|за\s+и\s+против)\b",
)

_QUERY_STOPWORDS = {
    "and",
    "are",
    "for",
    "from",
    "how",
    "latest",
    "the",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "без",
    "где",
    "для",
    "как",
    "какой",
    "когда",
    "кто",
    "между",
    "что",
    "это",
}


@dataclass(frozen=True)
class QueryRoute:
    """Deterministic, inspectable routing decision for one search query."""

    query_type: str
    primary_provider: str
    reason: str


@dataclass(frozen=True)
class SearchQuality:
    """Objective DDGS result quality signals used before Tavily escalation."""

    acceptable: bool
    score: float
    returned_results: int
    usable_results: int
    unique_results: int
    results_with_snippets: int
    relevant_results: int
    reasons: Tuple[str, ...]


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


def _matches_any(query: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, query, re.IGNORECASE) for pattern in patterns)


def _matching_pattern_count(query: str, patterns: Sequence[str]) -> int:
    return sum(
        1 for pattern in patterns if re.search(pattern, query, re.IGNORECASE)
    )


def _embedded_urls(query: str) -> Tuple[str, ...]:
    # Sentence punctuation and closing brackets are not normally part of a
    # copied URL. Preserve URL query strings, including their ``?`` separator.
    urls = []
    for match in _EMBEDDED_URL_RE.findall(query):
        normalized = match.rstrip(".,;:!)]}")
        if normalized:
            urls.append(normalized)
    return tuple(urls)


def _classify_query(query: str) -> QueryRoute:
    """Choose a conservative primary provider from query intent.

    The classifier intentionally requires explicit research language plus
    complexity before it authorizes Tavily.  Unknown and short queries remain
    on DDGS.  The function is deterministic so routing is explainable in the
    returned metadata and straightforward to regression-test.
    """
    normalized = " ".join(str(query or "").strip().split())
    lowered = normalized.casefold()
    urls = _embedded_urls(normalized)

    if _DIRECT_URL_RE.fullmatch(normalized):
        return QueryRoute(
            query_type="direct_url",
            primary_provider="local-browser",
            reason="a concrete URL should be read with the task-scoped local browser",
        )

    if len(urls) == 1 and _matches_any(lowered, _DIRECT_PAGE_PATTERNS):
        return QueryRoute(
            query_type="direct_url",
            primary_provider="local-browser",
            reason="explicit read/open intent for a concrete URL",
        )

    if urls:
        return QueryRoute(
            query_type="url_lookup",
            primary_provider="ddgs",
            reason="URL-containing search is locked to DDGS to prevent silent Tavily use",
        )

    strong_research = _matches_any(lowered, _STRONG_RESEARCH_PATTERNS)
    if strong_research:
        return QueryRoute(
            query_type="research",
            primary_provider="tavily",
            reason="explicit complex research intent",
        )

    if _matches_any(lowered, _TECHNICAL_PATTERNS):
        return QueryRoute(
            query_type="technical",
            primary_provider="ddgs",
            reason="technical/documentation lookup",
        )

    research_signals = _matching_pattern_count(lowered, _RESEARCH_PATTERNS)
    word_count = len(_WORD_RE.findall(lowered))
    clause_markers = len(re.findall(r"[,;:]|\b(?:and|or|versus|и|или|также)\b", lowered))
    complexity_signals = int(word_count >= 10) + int(clause_markers >= 2)
    complexity_signals += int(research_signals >= 2)

    if research_signals >= 1 and complexity_signals >= 2:
        return QueryRoute(
            query_type="research",
            primary_provider="tavily",
            reason="multiple research and complexity signals",
        )

    if _matches_any(lowered, _NEWS_PATTERNS):
        return QueryRoute(
            query_type="news",
            primary_provider="ddgs",
            reason="routine current-events/news lookup",
        )

    return QueryRoute(
        query_type="fact",
        primary_provider="ddgs",
        reason="routine factual lookup",
    )


def _query_terms(query: str) -> Tuple[str, ...]:
    terms = {
        token.casefold()
        for token in _WORD_RE.findall(query)
        if token.casefold() not in _QUERY_STOPWORDS
    }
    return tuple(sorted(terms))


def _canonical_result_url(value: Any) -> str:
    """Return a comparable public HTTP(S) URL, or an empty string."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    # Fragments do not identify a different search result.  Keep query strings
    # because they may identify a distinct document or language/version.
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _web_results(result: Any) -> List[Any]:
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    web = data.get("web")
    return web if isinstance(web, list) else []


def _assess_ddgs_quality(
    result: Dict[str, Any],
    query: str,
    limit: int,
) -> SearchQuality:
    """Score DDGS output using volume, uniqueness, snippets, and relevance."""
    raw_results = _web_results(result)
    if not result.get("success"):
        return SearchQuality(
            acceptable=False,
            score=0.0,
            returned_results=len(raw_results),
            usable_results=0,
            unique_results=0,
            results_with_snippets=0,
            relevant_results=0,
            reasons=(str(result.get("error") or "DDGS request failed"),),
        )

    query_terms = _query_terms(query)
    usable = 0
    snippets = 0
    relevant = 0
    unique_urls = set()

    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = _canonical_result_url(item.get("url"))
        if not title or not url:
            continue
        usable += 1
        unique_urls.add(url)
        description = str(item.get("description") or "").strip()
        if description:
            snippets += 1
        haystack = f"{title} {description} {url}".casefold()
        if not query_terms or any(term in haystack for term in query_terms):
            relevant += 1

    unique_count = len(unique_urls)
    expected_minimum = 1 if limit <= 1 else 2
    volume_ratio = min(unique_count / expected_minimum, 1.0)
    snippet_ratio = snippets / usable if usable else 0.0
    relevance_ratio = relevant / usable if usable else 0.0
    score = round(
        (0.45 * volume_ratio) + (0.25 * snippet_ratio) + (0.30 * relevance_ratio),
        3,
    )

    reasons: List[str] = []
    if unique_count < expected_minimum:
        reasons.append(
            f"only {unique_count} unique usable result(s); need {expected_minimum}"
        )
    if usable and snippet_ratio < 0.5:
        reasons.append("fewer than half of usable results have snippets")
    if usable and query_terms and relevance_ratio < 0.34:
        reasons.append("low lexical relevance to the query")
    if not usable:
        reasons.append("no usable titled HTTP(S) results")

    acceptable = (
        unique_count >= expected_minimum
        and snippet_ratio >= 0.5
        and (not query_terms or relevance_ratio >= 0.34)
        and score >= 0.60
    )
    if not acceptable and not reasons:
        reasons.append(f"quality score {score:.3f} is below 0.600")

    return SearchQuality(
        acceptable=acceptable,
        score=score,
        returned_results=len(raw_results),
        usable_results=usable,
        unique_results=unique_count,
        results_with_snippets=snippets,
        relevant_results=relevant,
        reasons=tuple(reasons),
    )


def _quality_dict(quality: SearchQuality) -> Dict[str, Any]:
    payload = asdict(quality)
    payload["reasons"] = list(quality.reasons)
    return payload


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


def _search_tavily(
    api_key: str,
    query: str,
    limit: int,
    search_depth: str = "advanced",
) -> Dict[str, Any]:
    """Call Tavily Search while retaining the HTTP status for failover."""
    import httpx

    safe_depth = "advanced" if search_depth == "advanced" else "basic"
    # Credentials must never be forwarded to a configurable destination.
    # Development proxies need their own explicit client and credential rather
    # than reusing the production TAVILY_API_KEY.
    url = f"{_TAVILY_API_BASE_URL}/search"
    payload = {
        "query": query,
        "search_depth": safe_depth,
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
        "provider": "tavily",
        "actual_provider": "tavily",
        "provider_used": "tavily",
        "meta": {
            "provider": "tavily",
            "actual_provider": "tavily",
            "provider_used": "tavily",
            "search_depth": safe_depth,
            "fallback_used": False,
        },
    }


def _extract_tavily(api_key: str, urls: List[str]) -> List[Dict[str, Any]]:
    """Extract up to 20 URLs through Tavily using current Bearer auth."""
    import httpx

    endpoint = f"{_TAVILY_API_BASE_URL}/extract"
    payload = {
        "urls": urls,
        # web_extract is an explicit paid action.  Use Tavily's higher-success
        # path here; routine/direct-page reading is routed to local browser by
        # agent policy instead of spending an extraction credit.
        "extract_depth": "advanced",
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
        raw_content = str(
            item.get("raw_content", "") or item.get("content", "") or ""
        )
        # Hermes v2026.8.27 trims web_extract entries to
        # url/title/content/error.  Keep a compact model-visible marker in the
        # content itself; provider/metadata remain available to direct callers.
        marked_content = (
            f"{_TAVILY_CONTENT_MARKER}\n\n{raw_content}" if raw_content else ""
        )
        documents.append(
            {
                "url": url,
                "title": item.get("title", ""),
                "content": marked_content,
                "raw_content": marked_content,
                **(
                    {}
                    if raw_content
                    else {
                        "error": (
                            f"{_TAVILY_CONTENT_MARKER} Tavily returned empty content"
                        )
                    }
                ),
                "provider": "tavily",
                "actual_provider": "tavily",
                "provider_used": "tavily",
                "metadata": {
                    "sourceURL": url,
                    "title": item.get("title", ""),
                    "provider": "tavily",
                    "actual_provider": "tavily",
                    "provider_used": "tavily",
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
                "provider": "tavily",
                "actual_provider": "tavily",
                "provider_used": "tavily",
                "metadata": {
                    "sourceURL": failed_url,
                    "provider": "tavily",
                    "actual_provider": "tavily",
                    "provider_used": "tavily",
                },
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
                "provider": "tavily",
                "actual_provider": "tavily",
                "provider_used": "tavily",
                "metadata": {
                    "sourceURL": url,
                    "provider": "tavily",
                    "actual_provider": "tavily",
                    "provider_used": "tavily",
                },
            }
        )

    return documents


def _label_tavily_documents(documents: Any) -> List[Dict[str, Any]]:
    """Ensure injected/custom Tavily clients satisfy the visible label contract."""
    if not isinstance(documents, list):
        raise RetryableTavilyError("Tavily extract returned an unexpected response type")

    labeled: List[Dict[str, Any]] = []
    for value in documents:
        if not isinstance(value, dict):
            raise RetryableTavilyError("Tavily extract returned an unexpected schema")
        item = dict(value)
        item["provider"] = "tavily"
        item["actual_provider"] = "tavily"
        item["provider_used"] = "tavily"
        metadata = item.get("metadata")
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        normalized_metadata.update(
            {
                "provider": "tavily",
                "actual_provider": "tavily",
                "provider_used": "tavily",
            }
        )
        item["metadata"] = normalized_metadata

        if item.get("error"):
            error = str(item["error"])
            item["error"] = (
                error
                if error.startswith(_TAVILY_CONTENT_MARKER)
                else f"{_TAVILY_CONTENT_MARKER} {error}"
            )
        else:
            raw = str(item.get("raw_content") or item.get("content") or "")
            if raw:
                marked = (
                    raw
                    if raw.startswith(_TAVILY_CONTENT_MARKER)
                    else f"{_TAVILY_CONTENT_MARKER}\n\n{raw}"
                )
                item["content"] = marked
                item["raw_content"] = marked
            else:
                item["content"] = ""
                item["raw_content"] = ""
                item["error"] = (
                    f"{_TAVILY_CONTENT_MARKER} Tavily returned empty content"
                )
        labeled.append(item)
    return labeled


def _missing_extract_document(url: str) -> Dict[str, Any]:
    """Return a typed Tavily failure for a missing positional response."""
    return {
        "url": url,
        "title": "",
        "content": "",
        "raw_content": "",
        "error": (
            f"{_TAVILY_CONTENT_MARKER} Tavily returned no result for this URL"
        ),
        "provider": "tavily",
        "actual_provider": "tavily",
        "provider_used": "tavily",
        "metadata": {
            "sourceURL": url,
            "provider": "tavily",
            "actual_provider": "tavily",
            "provider_used": "tavily",
        },
    }


def _align_tavily_documents(
    requested_urls: Sequence[str],
    documents: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Restore Hermes' one-result-per-input positional extract contract.

    Tavily returns successes and failures in separate collections and may omit
    or duplicate entries. Hermes caches results by list position, so returning
    provider order can cache one page under another URL. Build a stable map,
    prefer usable content, and clone the best response for duplicate inputs.
    """
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for document in documents:
        metadata = document.get("metadata")
        source_url = document.get("url")
        if not source_url and isinstance(metadata, dict):
            source_url = metadata.get("sourceURL")
        raw_url = str(source_url or "").strip()
        key = _canonical_result_url(raw_url) or raw_url
        if key:
            candidates.setdefault(key, []).append(document)

    aligned: List[Dict[str, Any]] = []
    for requested_url in requested_urls:
        key = _canonical_result_url(requested_url) or requested_url
        matches = candidates.get(key, [])
        if not matches:
            aligned.append(_missing_extract_document(requested_url))
            continue

        # A success beats a duplicated failed_results/failed_urls entry.
        source = next(
            (
                item
                for item in matches
                if not item.get("error")
                and bool(item.get("content") or item.get("raw_content"))
            ),
            matches[0],
        )
        item = dict(source)
        item["url"] = requested_url
        metadata = item.get("metadata")
        normalized_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        normalized_metadata["sourceURL"] = requested_url
        item["metadata"] = normalized_metadata
        aligned.append(item)
    return aligned


def _search_ddgs(query: str, limit: int) -> Dict[str, Any]:
    """Run DDGS in a killable child so a hung request cannot leak threads."""
    worker = Path(__file__).resolve().with_name("ddgs_worker.py")
    worker_env = {
        name: os.environ[name]
        for name in _DDGS_ENV_ALLOWLIST
        if os.environ.get(name)
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(worker)],
            input=json.dumps(
                {"query": str(query), "limit": int(limit)},
                ensure_ascii=False,
            ).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=worker_env,
            timeout=_DDGS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"DDGS search timed out after {_DDGS_TIMEOUT_SECONDS}s",
        }
    except Exception as exc:  # noqa: BLE001 - ddgs has provider-specific errors
        return {
            "success": False,
            "error": f"DDGS worker failed: {type(exc).__name__}",
        }

    if completed.returncode != 0:
        return {
            "success": False,
            "error": f"DDGS worker exited with status {completed.returncode}",
        }
    if len(completed.stdout) > _DDGS_MAX_OUTPUT_BYTES:
        return {"success": False, "error": "DDGS worker response was too large"}
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"success": False, "error": "DDGS worker returned invalid JSON"}
    if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
        return {"success": False, "error": "DDGS worker returned invalid schema"}
    return result


class TavilyDdgsWebSearchProvider(WebSearchProvider):
    """Route routine search to DDGS and reserve Tavily for justified use."""

    def __init__(
        self,
        *,
        key_lookup: Callable[[], str] = _default_key_lookup,
        tavily_search: Callable[..., Dict[str, Any]] = _search_tavily,
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
        return "DDGS → Tavily smart router"

    def is_available(self) -> bool:
        if self._key_lookup():
            return True
        return importlib.util.find_spec("ddgs") is not None

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    @staticmethod
    def _label_search_result(
        result: Dict[str, Any],
        *,
        actual_provider: str,
        route: QueryRoute,
        fallback_used: bool,
        **metadata: Any,
    ) -> Dict[str, Any]:
        """Attach a stable provider label without discarding vendor metadata."""
        labeled = dict(result)
        labeled["provider"] = actual_provider
        labeled["actual_provider"] = actual_provider
        labeled["provider_used"] = actual_provider
        existing_meta = labeled.get("meta")
        meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
        meta.update(
            {
                "provider": actual_provider,
                "actual_provider": actual_provider,
                "provider_used": actual_provider,
                "query_type": route.query_type,
                "primary_provider": route.primary_provider,
                "routing_reason": route.reason,
                "fallback_used": fallback_used,
            }
        )
        meta.update({key: value for key, value in metadata.items() if value is not None})
        if "attempted_providers" not in meta:
            meta["attempted_providers"] = (
                [actual_provider] if actual_provider not in {"none", "router"} else []
            )
        labeled["meta"] = meta
        return labeled

    @staticmethod
    def _direct_url_result(query: str, route: QueryRoute) -> Dict[str, Any]:
        """Return a useful typed result without pretending a page was fetched."""
        return {
            "success": True,
            "provider": "none",
            "actual_provider": "none",
            "provider_used": "none",
            "data": {
                "web": [
                    {
                        "title": "Direct URL — open with the local browser",
                        "url": query.strip(),
                        "description": (
                            "No external search provider was called. Use "
                            "browser_navigate and browser_snapshot to read this "
                            "specific or dynamic page without Tavily usage."
                        ),
                        "position": 1,
                    }
                ]
            },
            "meta": {
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "query_type": route.query_type,
                "primary_provider": route.primary_provider,
                "routing_reason": route.reason,
                "external_provider_used": False,
                "attempted_providers": [],
                "local_browser_recommended": True,
                "tavily_used": False,
                "tavily_spend_protected": True,
                "fallback_used": False,
            },
        }

    def _run_ddgs_safely(self, query: str, limit: int) -> Dict[str, Any]:
        try:
            result = self._ddgs_search(query, limit)
        except Exception as exc:  # noqa: BLE001 - injected/back-end exceptions
            logger.exception("Unexpected DDGS provider error")
            return {
                "success": False,
                "error": f"DDGS search failed: {type(exc).__name__}: {exc}",
            }
        if not isinstance(result, dict):
            return {
                "success": False,
                "error": "DDGS search returned an unexpected response type",
            }
        return result

    def _fallback_to_ddgs(
        self,
        query: str,
        limit: int,
        *,
        route: QueryRoute,
        reason: str,
        status_code: Optional[int] = None,
        tavily_attempted: bool = True,
    ) -> Dict[str, Any]:
        logger.warning("Tavily unavailable (%s); using DDGS fallback", reason)
        result = self._run_ddgs_safely(query, limit)
        quality = _assess_ddgs_quality(result, query, limit)
        attempted_providers = (
            ["tavily", "ddgs"] if tavily_attempted else ["ddgs"]
        )
        if result.get("success") and _web_results(result):
            return self._label_search_result(
                result,
                actual_provider="ddgs",
                route=route,
                fallback_used=True,
                fallback_from="tavily",
                fallback_reason=reason,
                tavily_status_code=status_code,
                tavily_used=tavily_attempted,
                tavily_attempted=tavily_attempted,
                ddgs_quality=_quality_dict(quality),
                attempted_providers=attempted_providers,
            )

        return {
            "success": False,
            "provider": "none",
            "actual_provider": "none",
            "provider_used": "none",
            "error": (
                f"Tavily unavailable ({reason}); "
                f"DDGS fallback also failed: {result.get('error', 'unknown error')}"
            ),
            "meta": {
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "query_type": route.query_type,
                "primary_provider": route.primary_provider,
                "routing_reason": route.reason,
                "fallback_used": True,
                "fallback_from": "tavily",
                "fallback_reason": reason,
                "tavily_status_code": status_code,
                "tavily_used": tavily_attempted,
                "tavily_attempted": tavily_attempted,
                "ddgs_quality": _quality_dict(quality),
                "attempted_providers": attempted_providers,
            },
        }

    def _degraded_ddgs_result(
        self,
        result: Dict[str, Any],
        *,
        route: QueryRoute,
        quality: SearchQuality,
        escalation_status: str,
        escalation_error: Optional[str] = None,
        tavily_status_code: Optional[int] = None,
        tavily_attempted: bool = False,
        tavily_spend_protected: bool = False,
    ) -> Dict[str, Any]:
        return self._label_search_result(
            result,
            actual_provider="ddgs",
            route=route,
            fallback_used=False,
            ddgs_quality=_quality_dict(quality),
            degraded=True,
            tavily_used=tavily_attempted,
            tavily_attempted=tavily_attempted,
            tavily_escalation=escalation_status,
            tavily_escalation_error=escalation_error,
            tavily_status_code=tavily_status_code,
            tavily_spend_protected=tavily_spend_protected,
            attempted_providers=(
                ["ddgs", "tavily"] if tavily_attempted else ["ddgs"]
            ),
        )

    def _search_research(
        self,
        query: str,
        limit: int,
        route: QueryRoute,
    ) -> Dict[str, Any]:
        api_key = self._key_lookup()
        if not api_key:
            return self._fallback_to_ddgs(
                query,
                limit,
                route=route,
                reason="TAVILY_API_KEY is not configured",
                tavily_attempted=False,
            )

        logger.info("Routing explicit complex research query to Tavily advanced search")
        try:
            result = self._tavily_search(api_key, query, limit, "advanced")
            if not isinstance(result, dict):
                raise RetryableTavilyError(
                    "Tavily search returned an unexpected response type"
                )
            if result.get("success") and _web_results(result):
                return self._label_search_result(
                    result,
                    actual_provider="tavily",
                    route=route,
                    fallback_used=False,
                    tavily_used=True,
                    tavily_trigger="complex_research",
                    search_depth="advanced",
                    attempted_providers=["tavily"],
                )
            reason = str(result.get("error") or "Tavily returned no search results")
            return self._fallback_to_ddgs(
                query,
                limit,
                route=route,
                reason=reason,
            )
        except RetryableTavilyError as exc:
            return self._fallback_to_ddgs(
                query,
                limit,
                route=route,
                reason=str(exc),
                status_code=exc.status_code,
            )
        except TavilyRequestError as exc:
            # A stale key or rejected paid request must not make research
            # unusable when the free provider is still available.
            return self._fallback_to_ddgs(
                query,
                limit,
                route=route,
                reason=str(exc),
                status_code=exc.status_code,
            )

    def _search_ddgs_first(
        self,
        query: str,
        limit: int,
        route: QueryRoute,
    ) -> Dict[str, Any]:
        ddgs_result = self._run_ddgs_safely(query, limit)
        quality = _assess_ddgs_quality(ddgs_result, query, limit)
        if quality.acceptable:
            return self._label_search_result(
                ddgs_result,
                actual_provider="ddgs",
                route=route,
                fallback_used=False,
                ddgs_quality=_quality_dict(quality),
                tavily_used=False,
                tavily_escalation="not_needed",
                tavily_spend_protected=True,
                attempted_providers=["ddgs"],
            )

        quality_reason = "; ".join(quality.reasons) or "low DDGS quality"
        if route.query_type == "url_lookup":
            if ddgs_result.get("success") and _web_results(ddgs_result):
                return self._degraded_ddgs_result(
                    ddgs_result,
                    route=route,
                    quality=quality,
                    escalation_status="blocked_url_query",
                    tavily_spend_protected=True,
                )
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": (
                    f"DDGS search failed ({quality_reason}); Tavily was not called "
                    "for a URL-containing query. Use the local browser instead."
                ),
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "query_type": route.query_type,
                    "primary_provider": route.primary_provider,
                    "routing_reason": route.reason,
                    "ddgs_quality": _quality_dict(quality),
                    "tavily_used": False,
                    "tavily_escalation": "blocked_url_query",
                    "tavily_spend_protected": True,
                    "local_browser_recommended": True,
                    "fallback_used": False,
                    "attempted_providers": ["ddgs"],
                },
            }

        api_key = self._key_lookup()
        if not api_key:
            if ddgs_result.get("success") and _web_results(ddgs_result):
                return self._degraded_ddgs_result(
                    ddgs_result,
                    route=route,
                    quality=quality,
                    escalation_status="skipped_missing_tavily_key",
                )
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": (
                    f"DDGS search failed ({quality_reason}); Tavily escalation "
                    "is unavailable because TAVILY_API_KEY is not configured"
                ),
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "query_type": route.query_type,
                    "primary_provider": route.primary_provider,
                    "routing_reason": route.reason,
                    "ddgs_quality": _quality_dict(quality),
                    "tavily_used": False,
                    "tavily_escalation": "skipped_missing_tavily_key",
                    "fallback_used": False,
                    "attempted_providers": ["ddgs"],
                },
            }

        logger.info("Escalating low-quality DDGS search to Tavily: %s", quality_reason)
        try:
            tavily_result = self._tavily_search(api_key, query, limit, "basic")
            if not isinstance(tavily_result, dict):
                raise RetryableTavilyError(
                    "Tavily search returned an unexpected response type"
                )
            if tavily_result.get("success") and _web_results(tavily_result):
                return self._label_search_result(
                    tavily_result,
                    actual_provider="tavily",
                    route=route,
                    fallback_used=True,
                    fallback_from="ddgs",
                    fallback_reason=quality_reason,
                    ddgs_quality=_quality_dict(quality),
                    tavily_used=True,
                    tavily_trigger="ddgs_quality",
                    search_depth="basic",
                    attempted_providers=["ddgs", "tavily"],
                )
            escalation_error = str(
                tavily_result.get("error") or "Tavily returned no search results"
            )
            if ddgs_result.get("success") and _web_results(ddgs_result):
                return self._degraded_ddgs_result(
                    ddgs_result,
                    route=route,
                    quality=quality,
                    escalation_status="failed",
                    escalation_error=escalation_error,
                    tavily_attempted=True,
                )
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": (
                    f"DDGS search failed ({quality_reason}); Tavily escalation "
                    f"also failed: {escalation_error}"
                ),
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "query_type": route.query_type,
                    "primary_provider": route.primary_provider,
                    "routing_reason": route.reason,
                    "ddgs_quality": _quality_dict(quality),
                    "attempted_providers": ["ddgs", "tavily"],
                },
            }
        except (RetryableTavilyError, TavilyRequestError) as exc:
            if ddgs_result.get("success") and _web_results(ddgs_result):
                return self._degraded_ddgs_result(
                    ddgs_result,
                    route=route,
                    quality=quality,
                    escalation_status="failed",
                    escalation_error=str(exc),
                    tavily_status_code=exc.status_code,
                    tavily_attempted=True,
                )
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": (
                    f"DDGS search failed ({quality_reason}); Tavily escalation "
                    f"also failed: {exc}{suffix}"
                ),
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "query_type": route.query_type,
                    "primary_provider": route.primary_provider,
                    "routing_reason": route.reason,
                    "ddgs_quality": _quality_dict(quality),
                    "tavily_status_code": exc.status_code,
                    "attempted_providers": ["ddgs", "tavily"],
                },
            }
        except Exception as exc:  # noqa: BLE001 - preserve usable DDGS output
            logger.exception("Unexpected Tavily quality-escalation error")
            if ddgs_result.get("success") and _web_results(ddgs_result):
                return self._degraded_ddgs_result(
                    ddgs_result,
                    route=route,
                    quality=quality,
                    escalation_status="failed",
                    escalation_error=f"{type(exc).__name__}: {exc}",
                    tavily_attempted=True,
                )
            raise

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {
                    "success": False,
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "error": "Interrupted",
                    "meta": {
                        "provider": "none",
                        "actual_provider": "none",
                        "provider_used": "none",
                        "attempted_providers": [],
                    },
                }
        except ImportError:
            pass

        try:
            # Hermes may request one of its 10/20/50/100 result buckets, while
            # both DDGS and Tavily are intentionally capped at 20 here.  Clamp
            # before dispatch so a routine request cannot make the stricter
            # DDGS worker fail and thereby unlock a paid Tavily escalation.
            safe_limit = min(max(int(limit), 1), _MAX_SEARCH_RESULTS)
        except (TypeError, ValueError):
            safe_limit = 5

        safe_query = str(query or "").strip()
        if not safe_query:
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": "Search query must not be empty",
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "external_provider_used": False,
                    "tavily_used": False,
                    "tavily_spend_protected": True,
                    "attempted_providers": [],
                },
            }
        if len(safe_query) > _MAX_SEARCH_QUERY_CHARS:
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": (
                    f"Search query exceeds the {_MAX_SEARCH_QUERY_CHARS}-character limit"
                ),
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "external_provider_used": False,
                    "tavily_used": False,
                    "tavily_spend_protected": True,
                    "attempted_providers": [],
                },
            }

        route = _classify_query(safe_query)
        if route.primary_provider == "local-browser":
            urls = _embedded_urls(safe_query)
            target_url = urls[0] if len(urls) == 1 else safe_query
            return self._direct_url_result(target_url, route)
        try:
            if route.primary_provider == "tavily":
                return self._search_research(safe_query, safe_limit, route)
            return self._search_ddgs_first(safe_query, safe_limit, route)
        except Exception as exc:  # noqa: BLE001 - never break the tool loop
            logger.exception("Unexpected Tavily/DDGS provider error")
            return {
                "success": False,
                "provider": "none",
                "actual_provider": "none",
                "provider_used": "none",
                "error": f"Unexpected web router error: {type(exc).__name__}",
                "meta": {
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "query_type": route.query_type,
                    "primary_provider": route.primary_provider,
                    "routing_reason": route.reason,
                    "attempted_providers": [],
                },
            }

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract explicit URLs with Tavily; DDGS has no extract capability.

        Dynamic or user-supplied concrete pages should normally be handled by
        Hermes' separate local-browser toolset before this method is called.
        A call is limited to one paid Tavily request of at most 20 URLs.
        """
        safe_urls = [str(url).strip() for url in urls if str(url).strip()]
        if not safe_urls:
            return []
        if len(safe_urls) > _MAX_EXTRACT_URLS:
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "error": (
                        "[source_provider: none] web_extract accepts at most "
                        f"{_MAX_EXTRACT_URLS} URLs per call; the oversized request "
                        "was rejected before Tavily"
                    ),
                }
                for url in safe_urls
            ]

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "provider": "none",
                        "actual_provider": "none",
                        "provider_used": "none",
                        "error": "[source_provider: none] Interrupted",
                    }
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
                    "provider": "none",
                    "actual_provider": "none",
                    "provider_used": "none",
                    "error": (
                        "[source_provider: none] TAVILY_API_KEY is not configured; "
                        "use the local browser for a concrete page"
                    ),
                }
                for url in safe_urls
            ]

        try:
            labeled = _label_tavily_documents(
                self._tavily_extract(api_key, safe_urls)
            )
            return _align_tavily_documents(safe_urls, labeled)
        except (RetryableTavilyError, TavilyRequestError) as exc:
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "provider": "tavily",
                    "actual_provider": "tavily",
                    "provider_used": "tavily",
                    "error": (
                        f"{_TAVILY_CONTENT_MARKER} Tavily extract "
                        f"unavailable: {exc}{suffix}"
                    ),
                }
                for url in safe_urls
            ]
        except Exception as exc:  # noqa: BLE001 - keep tool output typed
            logger.exception("Unexpected Tavily extract provider error")
            return [
                {
                    "url": url,
                    "title": "",
                    "content": "",
                    "provider": "tavily",
                    "actual_provider": "tavily",
                    "provider_used": "tavily",
                    "error": (
                        f"{_TAVILY_CONTENT_MARKER} Unexpected Tavily extract error: "
                        f"{type(exc).__name__}"
                    ),
                }
                for url in safe_urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "DDGS routine search · Tavily research/extract",
            "tag": (
                "Tavily is called only for explicit complex research, low-quality "
                "DDGS output, or explicit URL extraction."
            ),
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key",
                    "url": "https://app.tavily.com/home",
                }
            ],
        }
