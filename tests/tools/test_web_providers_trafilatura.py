"""Tests for the Trafilatura (extract-only) web provider.

Covers:
- TrafilaturaWebExtractProvider.is_available() — reflects package importability
- Capability flags — extract-only (supports_extract True, supports_search False)
- extract() — happy path (full markdown, no truncation), missing package,
  empty extraction, pre-fetch policy block, redirect SSRF re-check, fetch error
- get_setup_schema() — picker row shape + post_setup auto-install key
- Registry integration — selectable as web.extract_backend; search-only callers
  fall through (supports_search False)
"""
from __future__ import annotations

import asyncio
import importlib.util

import pytest

from plugins.web.trafilatura.provider import TrafilaturaWebExtractProvider


def _force_available(monkeypatch, available: bool = True):
    """Make importlib.util.find_spec('trafilatura') report (un)available."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == "trafilatura":
            return object() if available else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


# ---------------------------------------------------------------------------
# Availability + capabilities
# ---------------------------------------------------------------------------


class TestTrafilaturaAvailability:
    def test_available_when_package_importable(self, monkeypatch):
        _force_available(monkeypatch, True)
        assert TrafilaturaWebExtractProvider().is_available() is True

    def test_not_available_when_package_missing(self, monkeypatch):
        _force_available(monkeypatch, False)
        assert TrafilaturaWebExtractProvider().is_available() is False


class TestTrafilaturaCapabilities:
    def test_extract_only(self):
        p = TrafilaturaWebExtractProvider()
        assert p.name == "trafilatura"
        assert p.display_name == "Trafilatura"
        assert p.supports_extract() is True
        assert p.supports_search() is False

    def test_search_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            TrafilaturaWebExtractProvider().search("anything")


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestTrafilaturaExtract:
    def test_happy_path_returns_full_markdown(self, monkeypatch):
        _force_available(monkeypatch, True)
        p = TrafilaturaWebExtractProvider()

        long_md = "# Title\n\n" + ("paragraph body. " * 2000)  # ~32k chars
        monkeypatch.setattr(
            p, "_fetch_markdown",
            lambda url, fmt: {"markdown": long_md, "title": "A Page", "final_url": url},
        )
        monkeypatch.setattr("plugins.web.trafilatura.provider.check_website_access", lambda u: None)
        monkeypatch.setattr("plugins.web.trafilatura.provider.is_safe_url", lambda u: True)

        results = asyncio.run(p.extract(["https://example.com/article"]))
        assert len(results) == 1
        r = results[0]
        assert r.get("error") is None
        assert r["title"] == "A Page"
        assert r["url"] == "https://example.com/article"
        # FULL markdown preserved — provider must not truncate.
        assert r["content"] == long_md
        assert r["raw_content"] == long_md
        assert r["metadata"]["source"] == "trafilatura"

    def test_missing_package_returns_error_items(self, monkeypatch):
        _force_available(monkeypatch, False)
        results = asyncio.run(
            TrafilaturaWebExtractProvider().extract(["https://example.com/"])
        )
        assert len(results) == 1
        assert "not installed" in results[0]["error"]

    def test_empty_extraction_is_an_error_item(self, monkeypatch):
        _force_available(monkeypatch, True)
        p = TrafilaturaWebExtractProvider()
        monkeypatch.setattr(
            p, "_fetch_markdown",
            lambda url, fmt: {"markdown": "   ", "title": "", "final_url": url},
        )
        monkeypatch.setattr("plugins.web.trafilatura.provider.check_website_access", lambda u: None)
        monkeypatch.setattr("plugins.web.trafilatura.provider.is_safe_url", lambda u: True)

        results = asyncio.run(p.extract(["https://example.com/empty"]))
        assert "No extractable content" in results[0]["error"]
        assert results[0]["content"] == ""

    def test_pre_fetch_policy_block(self, monkeypatch):
        _force_available(monkeypatch, True)
        p = TrafilaturaWebExtractProvider()
        block = {"host": "blocked.test", "rule": "deny", "source": "config", "message": "blocked by policy"}
        monkeypatch.setattr("plugins.web.trafilatura.provider.check_website_access", lambda u: block)
        # _fetch_markdown must NOT be called when the pre-fetch gate blocks.
        monkeypatch.setattr(
            p, "_fetch_markdown",
            lambda url, fmt: pytest.fail("should not fetch a policy-blocked URL"),
        )

        results = asyncio.run(p.extract(["https://blocked.test/x"]))
        assert results[0]["error"] == "blocked by policy"
        assert results[0]["blocked_by_policy"]["host"] == "blocked.test"

    def test_redirect_to_private_address_blocked(self, monkeypatch):
        _force_available(monkeypatch, True)
        p = TrafilaturaWebExtractProvider()
        monkeypatch.setattr(
            p, "_fetch_markdown",
            lambda url, fmt: {"markdown": "# x\n\nbody", "title": "t", "final_url": "http://169.254.169.254/"},
        )
        monkeypatch.setattr("plugins.web.trafilatura.provider.check_website_access", lambda u: None)
        # Initial URL safe; redirect target (metadata IP) unsafe.
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.is_safe_url",
            lambda u: "169.254" not in u,
        )

        results = asyncio.run(p.extract(["https://example.com/redirector"]))
        assert "private or internal" in results[0]["error"]

    def test_fetch_error_becomes_error_item(self, monkeypatch):
        _force_available(monkeypatch, True)
        p = TrafilaturaWebExtractProvider()

        def boom(url, fmt):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(p, "_fetch_markdown", boom)
        monkeypatch.setattr("plugins.web.trafilatura.provider.check_website_access", lambda u: None)

        results = asyncio.run(p.extract(["https://example.com/flaky"]))
        assert "trafilatura fetch failed" in results[0]["error"]


# ---------------------------------------------------------------------------
# Picker schema + registry integration
# ---------------------------------------------------------------------------


class TestTrafilaturaSetupSchema:
    def test_schema_shape(self):
        schema = TrafilaturaWebExtractProvider().get_setup_schema()
        assert schema["name"] == "Trafilatura"
        assert schema["env_vars"] == []          # no API key
        assert schema["post_setup"] == "trafilatura"
        assert "extract only" in schema["badge"]


class TestTrafilaturaRegistryIntegration:
    def test_selectable_as_extract_backend(self):
        from agent.web_search_registry import get_provider, _reset_for_tests, register_provider

        _reset_for_tests()
        register_provider(TrafilaturaWebExtractProvider())
        try:
            prov = get_provider("trafilatura")
            assert prov is not None
            assert prov.supports_extract() is True
            assert prov.supports_search() is False
        finally:
            _reset_for_tests()


# ---------------------------------------------------------------------------
# Integration: _is_backend_available / _get_extract_backend wiring
# ---------------------------------------------------------------------------


class TestTrafilaturaBackendWiring:
    """Availability now flows through the plugin registry seam.

    Upstream generalized ``_is_backend_available``: any backend outside
    ``_LEGACY_WEB_BACKENDS`` delegates to the registered provider's
    ``is_available()``. trafilatura is plugin-registered, so we patch that
    seam rather than a trafilatura-specific probe (which no longer exists).
    """

    @staticmethod
    def _patch_availability(monkeypatch, web_tools, available: bool):
        monkeypatch.setattr(
            web_tools,
            "_registered_web_provider_available",
            lambda backend: available if backend == "trafilatura" else None,
        )

    def test_is_backend_available_true_when_package_importable(self, monkeypatch):
        from tools import web_tools
        self._patch_availability(monkeypatch, web_tools, True)
        assert web_tools._is_backend_available("trafilatura") is True

    def test_is_backend_available_false_when_package_missing(self, monkeypatch):
        from tools import web_tools
        self._patch_availability(monkeypatch, web_tools, False)
        assert web_tools._is_backend_available("trafilatura") is False

    def test_configured_extract_backend_resolves_to_trafilatura(self, monkeypatch):
        """web.extract_backend=trafilatura is honored when the package is present."""
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "ddgs", "search_backend": "ddgs", "extract_backend": "trafilatura"},
        )
        self._patch_availability(monkeypatch, web_tools, True)
        assert web_tools._get_extract_backend() == "trafilatura"

    def test_unavailable_extract_backend_is_still_honored(self, monkeypatch):
        """A stored extract_backend wins even when unavailable (strict selection).

        Upstream made per-capability selection strict on purpose: a
        selected-but-broken backend must surface the vendor path's honest
        error rather than being silently swapped for whatever the
        credential ladder finds. This previously asserted a fallback to
        the shared backend; that behavior is gone by design.
        """
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "firecrawl", "search_backend": "", "extract_backend": "trafilatura"},
        )
        self._patch_availability(monkeypatch, web_tools, False)
        assert web_tools._get_extract_backend() == "trafilatura"

    def test_extract_backend_falls_through_to_shared_when_unset(self, monkeypatch):
        """With no per-capability override stored, the shared backend is used."""
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "firecrawl", "search_backend": "", "extract_backend": ""},
        )
        assert web_tools._get_extract_backend() == "firecrawl"


# ---------------------------------------------------------------------------
# _fetch_markdown — manual per-hop redirect SSRF validation
# ---------------------------------------------------------------------------


class TestFetchMarkdownRedirectSSRF:
    """The real fetch path follows redirects manually and validates each hop
    with ``is_safe_url`` *before* issuing the next request. All cases use
    httpx.MockTransport — no real network, no attack payloads.
    """

    def _patch_httpx(self, monkeypatch, handler):
        import httpx

        real_client = httpx.Client

        def _client(**kwargs):
            kwargs.pop("follow_redirects", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "Client", _client)

    @staticmethod
    def _reject_private(url: str) -> bool:
        """Stand-in for the real is_safe_url: treat obvious private hosts as
        unsafe and everything else as safe."""
        return not any(p in url for p in ("10.0.0.", "127.0.0.", "192.168."))

    def test_redirect_to_private_host_is_blocked_before_body_read(self, monkeypatch):
        import httpx
        _force_available(monkeypatch, True)

        def handler(request):
            if request.url.host == "start.example.com":
                return httpx.Response(302, headers={"location": "http://10.0.0.1/dashboard"})
            # If the guard worked, this internal hop is never requested.
            return httpx.Response(200, text="internal page body")

        self._patch_httpx(monkeypatch, handler)
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.is_safe_url", self._reject_private
        )
        p = TrafilaturaWebExtractProvider()
        with pytest.raises(RuntimeError, match="private or internal"):
            p._fetch_markdown("https://start.example.com/go", "markdown")

    def test_multi_hop_safe_redirects_are_followed(self, monkeypatch):
        import httpx
        _force_available(monkeypatch, True)

        # a -> b -> c (two hops), all public/safe; c serves the content.
        hops = {
            "a.example.com": (301, "https://b.example.com/2"),
            "b.example.com": (302, "https://c.example.com/3"),
        }

        def handler(request):
            nxt = hops.get(request.url.host)
            if nxt:
                return httpx.Response(nxt[0], headers={"location": nxt[1]})
            return httpx.Response(
                200,
                text=(
                    "<html><body><article>Final destination article body "
                    "for extraction.</article></body></html>"
                ),
            )

        self._patch_httpx(monkeypatch, handler)
        monkeypatch.setattr("plugins.web.trafilatura.provider.is_safe_url", lambda u: True)
        p = TrafilaturaWebExtractProvider()
        result = p._fetch_markdown("https://a.example.com/1", "markdown")
        assert result["final_url"] == "https://c.example.com/3"
        assert result["markdown"].strip()

    def test_redirect_loop_is_capped(self, monkeypatch):
        import httpx
        _force_available(monkeypatch, True)

        self._patch_httpx(
            monkeypatch,
            lambda request: httpx.Response(
                302, headers={"location": "https://loop.example.com/x"}
            ),
        )
        monkeypatch.setattr("plugins.web.trafilatura.provider.is_safe_url", lambda u: True)
        p = TrafilaturaWebExtractProvider()
        with pytest.raises(RuntimeError, match="too many redirects"):
            p._fetch_markdown("https://loop.example.com/x", "markdown")


# ---------------------------------------------------------------------------
# extract() fetches URLs concurrently, not one after another
# ---------------------------------------------------------------------------


class TestExtractConcurrency:
    def test_multiple_urls_are_fetched_concurrently(self, monkeypatch):
        """N URLs must cost ~one page's latency, not N pages'.

        Regression guard: extract() used to await each URL in a plain for
        loop, so a 5-URL call (web_extract's max) could serialize into
        5 x _FETCH_TIMEOUT in the worst case.
        """
        import asyncio as _asyncio
        import time
        from plugins.web.trafilatura.provider import TrafilaturaWebExtractProvider

        provider = TrafilaturaWebExtractProvider()
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.check_website_access", lambda url: None
        )
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.is_safe_url", lambda url: True
        )

        DELAY = 0.25
        urls = [f"https://example.com/{i}" for i in range(5)]

        def _slow_fetch(url, output_format):
            time.sleep(DELAY)
            return {"markdown": f"# page {url}", "title": "t", "final_url": url}

        monkeypatch.setattr(provider, "_fetch_markdown", _slow_fetch)

        started = time.monotonic()
        results = _asyncio.run(provider.extract(urls))
        elapsed = time.monotonic() - started

        assert [r["url"] for r in results] == urls, "result order must match input order"
        assert all(r.get("content") for r in results)
        # Sequential would be >= 5 * DELAY; concurrent stays near one DELAY.
        assert elapsed < DELAY * 3, (
            f"extract() appears to serialize: {elapsed:.2f}s for {len(urls)} URLs "
            f"at {DELAY}s each (concurrent should be ~{DELAY}s)"
        )

    def test_one_failing_url_does_not_discard_the_others(self, monkeypatch):
        """A raising page is reported in its own slot; siblings still return."""
        import asyncio as _asyncio
        from plugins.web.trafilatura.provider import TrafilaturaWebExtractProvider

        provider = TrafilaturaWebExtractProvider()
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.check_website_access", lambda url: None
        )
        monkeypatch.setattr(
            "plugins.web.trafilatura.provider.is_safe_url", lambda url: True
        )

        def _fetch(url, output_format):
            if url.endswith("bad"):
                raise RuntimeError("boom")
            return {"markdown": "# ok", "title": "t", "final_url": url}

        monkeypatch.setattr(provider, "_fetch_markdown", _fetch)

        urls = ["https://example.com/good", "https://example.com/bad"]
        results = _asyncio.run(provider.extract(urls))

        assert [r["url"] for r in results] == urls
        assert results[0]["content"] == "# ok"
        assert results[1]["error"]
        assert not results[1]["content"]


# ---------------------------------------------------------------------------
# An extract-only provider must never become the shared/search backend
# ---------------------------------------------------------------------------


class TestExtractOnlyProviderNotChosenForSearch:
    """Regression: registering trafilatura hijacked the shared web backend.

    ``_get_backend()``'s final plugin walk returned the first *available*
    registered non-legacy provider without asking whether it can search.
    trafilatura is extract-only, so on a never-configured install with the
    package present it won the shared slot, and ``_get_search_backend()``
    (which falls through to ``_get_backend()``) resolved web_search to a
    backend that cannot serve it.
    """

    def _unconfigured(self, monkeypatch, web_tools):
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
        monkeypatch.setattr(
            "tools.tool_backend_helpers.selection_exists", lambda _k: False
        )
        # No credentials for any built-in backend.
        monkeypatch.setattr(web_tools, "_has_env", lambda _n: False)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
        monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)

    def test_available_extract_only_provider_is_skipped(self, monkeypatch):
        from tools import web_tools

        class _ExtractOnly:
            name = "trafilatura"
            def supports_search(self): return False
            def supports_extract(self): return True
            def is_available(self): return True

        self._unconfigured(monkeypatch, web_tools)
        monkeypatch.setattr(
            web_tools, "_list_registered_web_providers", lambda: [_ExtractOnly()]
        )
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)

        assert web_tools._get_backend() != "trafilatura"
        assert web_tools._get_search_backend() != "trafilatura"

    def test_search_capable_plugin_provider_is_still_chosen(self, monkeypatch):
        """The walk must still find genuine search-capable plugin backends."""
        from tools import web_tools

        class _SearchCapable:
            name = "custom-search"
            def supports_search(self): return True
            def supports_extract(self): return False
            def is_available(self): return True

        self._unconfigured(monkeypatch, web_tools)
        monkeypatch.setattr(
            web_tools, "_list_registered_web_providers", lambda: [_SearchCapable()]
        )
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)

        assert web_tools._get_backend() == "custom-search"

    def test_extract_only_still_selectable_explicitly(self, monkeypatch):
        """Explicit web.extract_backend remains the supported way to pick it."""
        from tools import web_tools

        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "", "extract_backend": "trafilatura"},
        )
        assert web_tools._get_extract_backend() == "trafilatura"
