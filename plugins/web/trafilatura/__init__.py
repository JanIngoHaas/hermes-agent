"""Trafilatura web extract plugin — bundled, auto-loaded.

Backed by the community ``trafilatura`` Python package which converts an HTML
page to clean Markdown locally. No API key required, but the package itself
must be installed (it's an optional dep — gated via :meth:`is_available`).

Extract-only: pair with any search provider (e.g. ``searxng``, ``ddgs``) via
``web.search_backend`` while using ``web.extract_backend: trafilatura``.
"""

from __future__ import annotations

from plugins.web.trafilatura.provider import TrafilaturaWebExtractProvider


def register(ctx) -> None:
    """Register the Trafilatura extract provider with the plugin context."""
    ctx.register_web_search_provider(TrafilaturaWebExtractProvider())
