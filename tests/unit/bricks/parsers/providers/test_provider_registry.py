"""Tests for ProviderRegistry auto_discover behavior."""

import sys

import pytest

from nexus.bricks.parsers.providers.registry import ProviderRegistry


def test_auto_discover_registers_pdf_inspector_when_available(monkeypatch):
    pytest.importorskip("pdf_inspector")
    # Force unstructured/llamaparse off so we only see the local providers.
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    registry = ProviderRegistry()
    registry.auto_discover()

    names = [p.name for p in registry.get_all_providers()]
    assert "pdf-inspector" in names

    pdf_provider = registry.get_provider_by_name("pdf-inspector")
    assert pdf_provider is not None
    assert pdf_provider.priority == 20
    assert ".pdf" in pdf_provider.supported_formats


def test_auto_discover_skips_pdf_inspector_when_import_fails(monkeypatch):
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "pdf_inspector", None)

    registry = ProviderRegistry()
    registry.auto_discover()

    names = [p.name for p in registry.get_all_providers()]
    assert "pdf-inspector" not in names


def test_auto_discover_pdf_inspector_outranks_markitdown_for_pdf(monkeypatch):
    pytest.importorskip("pdf_inspector")
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    registry = ProviderRegistry()
    registry.auto_discover()

    chosen = registry.get_provider("doc.pdf")
    assert chosen is not None
    assert chosen.name == "pdf-inspector"
