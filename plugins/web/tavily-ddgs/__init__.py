"""Cost-aware DDGS/Tavily search router and Tavily extractor."""

from .provider import TavilyDdgsWebSearchProvider


def register(ctx) -> None:
    """Register the composite provider with Hermes."""
    ctx.register_web_search_provider(TavilyDdgsWebSearchProvider())
