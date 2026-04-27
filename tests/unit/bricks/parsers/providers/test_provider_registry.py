"""Tests for ProviderRegistry auto_discover behavior."""

import sys

import pytest

from nexus.bricks.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.bricks.parsers.providers.registry import ProviderRegistry
from nexus.bricks.parsers.types import ParseResult


class _Provider(ParseProvider):
    def __init__(self, name: str, priority: int) -> None:
        self._name = name
        super().__init__(ProviderConfig(name=name, priority=priority))

    @property
    def name(self) -> str:
        return self._name

    @property
    def default_formats(self) -> list[str]:
        return [".pdf"]

    def is_available(self) -> bool:
        return True

    async def parse(self, content: bytes, file_path: str, metadata=None) -> ParseResult:
        return ParseResult(text=self.name)


def test_register_overwrite_replaces_priority_order() -> None:
    registry = ProviderRegistry()
    first = _Provider("same", priority=100)
    replacement = _Provider("same", priority=1)

    registry.register(first)
    registry.register(replacement)

    assert registry.get_provider("doc.pdf") is replacement
    assert registry.get_all_providers() == [replacement]


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


def test_auto_discover_pdf_inspector_registered_for_pdf(monkeypatch):
    pytest.importorskip("pdf_inspector")
    monkeypatch.delenv("UNSTRUCTURED_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_CLOUD_API_KEY", raising=False)

    registry = ProviderRegistry()
    registry.auto_discover()

    chosen = registry.get_provider("doc.pdf")
    assert chosen is not None
    assert chosen.name == "pdf-inspector"
