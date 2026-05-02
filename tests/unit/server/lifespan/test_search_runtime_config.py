"""Three-way auto-resolution for txtai runtime (Issue #3997).

Default (no env): BM25 keyword-only — no model load.
OPENAI_API_KEY set: API embeddings — ~0 RAM.
NEXUS_TXTAI_MODEL=local: opt-in heavy load.
"""

import pytest

from nexus.server.lifespan.search import _resolve_txtai_runtime_config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "NEXUS_TXTAI_MODEL",
        "NEXUS_TXTAI_USE_API_EMBEDDINGS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_default_returns_bm25():
    """No env -> (None, None) -> BM25 keyword-only path."""
    assert _resolve_txtai_runtime_config() == (None, None)


def test_openai_key_only(monkeypatch):
    """OPENAI_API_KEY alone -> openai/text-embedding-3-small with key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test"}


def test_openai_key_with_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test", "api_base": "https://proxy.example/v1"}


def test_explicit_local_model_wins_over_key(monkeypatch):
    """User-set local model overrides API mode even when key is present."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "sentence-transformers/all-MiniLM-L6-v2"
    assert vectors is None


def test_explicit_local_model_no_key(monkeypatch):
    """User-set local model without key still loads locally."""
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "sentence-transformers/all-mpnet-base-v2")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "sentence-transformers/all-mpnet-base-v2"
    assert vectors is None


def test_explicit_openai_model_uses_key(monkeypatch):
    """Explicit openai/* model + key -> use API."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_MODEL", "openai/text-embedding-3-large")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-large"
    assert vectors == {"api_key": "sk-test"}


def test_use_api_flag_with_key(monkeypatch):
    """NEXUS_TXTAI_USE_API_EMBEDDINGS=true + key -> default openai model."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "true")
    model, vectors = _resolve_txtai_runtime_config()
    assert model == "openai/text-embedding-3-small"
    assert vectors == {"api_key": "sk-test"}


def test_use_api_flag_no_key(monkeypatch):
    """NEXUS_TXTAI_USE_API_EMBEDDINGS=true without key -> still BM25 (no key to use)."""
    monkeypatch.setenv("NEXUS_TXTAI_USE_API_EMBEDDINGS", "true")
    assert _resolve_txtai_runtime_config() == (None, None)
