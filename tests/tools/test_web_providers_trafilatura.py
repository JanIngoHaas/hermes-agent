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
    def test_is_backend_available_true_when_package_importable(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_trafilatura_package_importable", lambda: True)
        assert web_tools._is_backend_available("trafilatura") is True

    def test_is_backend_available_false_when_package_missing(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_trafilatura_package_importable", lambda: False)
        assert web_tools._is_backend_available("trafilatura") is False

    def test_configured_extract_backend_resolves_to_trafilatura(self, monkeypatch):
        """web.extract_backend=trafilatura is honored when the package is present."""
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "ddgs", "search_backend": "ddgs", "extract_backend": "trafilatura"},
        )
        monkeypatch.setattr(web_tools, "_trafilatura_package_importable", lambda: True)
        assert web_tools._get_extract_backend() == "trafilatura"

    def test_unavailable_extract_backend_falls_back(self, monkeypatch):
        """If trafilatura isn't installed, extract selection falls back to shared backend."""
        from tools import web_tools
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "firecrawl", "search_backend": "", "extract_backend": "trafilatura"},
        )
        monkeypatch.setattr(web_tools, "_trafilatura_package_importable", lambda: False)
        assert web_tools._get_extract_backend() == "firecrawl"
