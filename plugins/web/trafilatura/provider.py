"""Trafilatura web extract — plugin form.

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.
This is an **extract-only** backend backed by the community ``trafilatura``
package, which strips boilerplate from an HTML page and emits clean Markdown.
No API key is required — like the ``ddgs`` search provider, ``trafilatura`` is
an optional Python dependency gated via :meth:`is_available`, and the plugin
registers either way so ``hermes tools`` can offer to install it.

Why trafilatura: it's a self-contained, "medium-weight" extractor (no hosted
service, no key) that produces high-quality Markdown. It fills the gap of a
local/offline extract backend alongside the hosted ones (firecrawl, tavily,
exa, parallel).

Fetching: unlike the hosted providers (which fetch from vendor infrastructure),
trafilatura fetches from the local host, so this provider re-applies the SSRF
guard (:func:`tools.url_safety.is_safe_url`) and the website-access policy
(:func:`tools.website_policy.check_website_access`) to the *final* URL after
redirects — mirroring the firecrawl provider's redirect-aware re-check. The
initial SSRF filter on the requested URLs is already applied by the
``web_extract`` tool wrapper before dispatch.

Output: returns the FULL clean Markdown per page (no truncation). Oversized
pages are compressed downstream by the ``web_extract`` tool's
``auxiliary.web_extract`` summarizer, exactly as for the other backends.

Config::

    web:
      extract_backend: "trafilatura"   # explicit per-capability selection
      # search_backend untouched — trafilatura is extract-only

Async note: ``extract()`` is ``async def``; each URL's blocking fetch +
parse runs in :func:`asyncio.to_thread` under a 60s
:func:`asyncio.wait_for` guard so a hung or huge page can't block the loop.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

# A realistic UA — some sites serve stub/blocked HTML to default python UAs.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 HermesBot/1.0"
)
_FETCH_TIMEOUT = 60


def _blocked_result(url: str, title: str, blocked: Dict[str, Any]) -> Dict[str, Any]:
    """Build a per-URL result dict for a website-policy block."""
    return {
        "url": url,
        "title": title,
        "content": "",
        "raw_content": "",
        "error": blocked["message"],
        "blocked_by_policy": {
            "host": blocked["host"],
            "rule": blocked["rule"],
            "source": blocked["source"],
        },
    }


class TrafilaturaWebExtractProvider(WebSearchProvider):
    """Extract-only provider that converts pages to Markdown via trafilatura."""

    @property
    def name(self) -> str:
        return "trafilatura"

    @property
    def display_name(self) -> str:
        return "Trafilatura"

    def is_available(self) -> bool:
        """Return True when the ``trafilatura`` package is importable.

        Uses :func:`importlib.util.find_spec` so we don't pay trafilatura's
        (lxml-backed) import cost at tool-registration time or on every
        ``hermes tools`` paint. Never performs network I/O.
        """
        try:
            return importlib.util.find_spec("trafilatura") is not None
        except (ImportError, ValueError):
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def _fetch_markdown(self, url: str, output_format: str) -> Dict[str, Any]:
        """Fetch *url* and convert to clean Markdown (blocking; runs in a thread).

        Returns ``{"markdown": str, "title": str, "final_url": str}``.
        Raises on network/HTTP error (caught by the caller).
        """
        import httpx
        import trafilatura

        with httpx.Client(
            follow_redirects=True,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()

        final_url = str(resp.url)
        html = resp.text

        # FULL content — no truncation. The web_extract wrapper's auxiliary
        # summarizer compresses oversized pages downstream.
        content = trafilatura.extract(
            html,
            output_format=output_format,
            include_links=True,
            include_tables=True,
            include_formatting=True,
            favor_recall=True,
            with_metadata=False,
        )

        title = ""
        try:
            meta = trafilatura.extract_metadata(html)
            if meta is not None and getattr(meta, "title", None):
                title = meta.title or ""
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass

        return {"markdown": content or "", "title": title, "final_url": final_url}

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract clean Markdown from one or more URLs.

        Per-URL failures (timeout, fetch error, SSRF/policy block on a
        redirect target, empty extraction) become items with an ``error``
        field rather than raising. Returns the legacy per-URL list-of-results
        shape consumed by the ``web_extract`` post-processing pipeline.

        Accepted kwargs (others ignored for forward compat):
          - ``format``: ``"html"`` returns cleaned HTML; anything else
            (incl. None) returns Markdown.
        """
        if importlib.util.find_spec("trafilatura") is None:
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": "trafilatura package is not installed — run `pip install trafilatura`",
                }
                for u in urls
            ]

        output_format = "html" if kwargs.get("format") == "html" else "markdown"
        results: List[Dict[str, Any]] = []

        for url in urls:
            # Pre-fetch website-policy gate.
            blocked = check_website_access(url)
            if blocked:
                logger.info(
                    "Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"]
                )
                results.append(_blocked_result(url, "", blocked))
                continue

            try:
                logger.info("Trafilatura extract: %s", url)
                fetched = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch_markdown, url, output_format),
                    timeout=_FETCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Trafilatura fetch timed out for %s", url)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": (
                            f"Fetch timed out after {_FETCH_TIMEOUT}s — page may be too "
                            "large or unresponsive. Try browser_navigate instead."
                        ),
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 — surface fetch/parse errors per-URL
                logger.debug("Trafilatura fetch failed for %s: %s", url, exc)
                results.append(
                    {"url": url, "title": "", "content": "", "error": f"trafilatura fetch failed: {exc}"}
                )
                continue

            final_url = fetched["final_url"]
            title = fetched["title"]

            # Redirect-aware SSRF + website-policy re-check on the final URL.
            if not is_safe_url(final_url):
                results.append(
                    {
                        "url": final_url,
                        "title": "",
                        "content": "",
                        "error": "Blocked: redirected to a private or internal network address",
                    }
                )
                continue
            final_blocked = check_website_access(final_url)
            if final_blocked:
                logger.info(
                    "Blocked redirected web_extract for %s by rule %s",
                    final_blocked["host"],
                    final_blocked["rule"],
                )
                results.append(_blocked_result(final_url, title, final_blocked))
                continue

            content = fetched["markdown"]
            if not content.strip():
                results.append(
                    {
                        "url": final_url,
                        "title": title,
                        "content": "",
                        "error": (
                            "No extractable content found (trafilatura returned empty). "
                            "The page may be JS-rendered or a non-HTML document (e.g. PDF) — "
                            "try browser_navigate instead."
                        ),
                    }
                )
                continue

            results.append(
                {
                    "url": final_url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": {"source": "trafilatura", "sourceURL": final_url},
                }
            )

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Trafilatura",
            "badge": "free · no key · extract only",
            "tag": (
                "Local HTML→Markdown extraction via the trafilatura package — "
                "no API key (pair with any search provider)"
            ),
            "env_vars": [],
            # Trigger `_run_post_setup("trafilatura")` after the user picks this
            # row so the trafilatura package gets pip-installed on first use.
            "post_setup": "trafilatura",
        }
