from __future__ import annotations

from unittest.mock import patch

from nexus.server.lifespan.search import _resolve_txtai_runtime_config


class TestResolveTxtaiRuntimeConfig:
    def test_defaults_to_local_model(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            model, vectors = _resolve_txtai_runtime_config()

        assert model == "sentence-transformers/all-MiniLM-L6-v2"
        assert vectors is None

    def test_keeps_local_default_without_openai_key(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert model == "sentence-transformers/all-MiniLM-L6-v2"
        assert vectors is None

    def test_enables_openai_api_embeddings_when_opted_in(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_BASE_URL": "https://api.openai.example/v1",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        # Default model is 3-large (3072d native, Matryoshka-truncated to 1536
        # so pgvector hnsw stays under its 2000d cap) — picked for recall
        # rather than the cheaper 3-small.
        assert model == "openai/text-embedding-3-large"
        assert vectors == {
            "api_key": "sk-test",
            "api_base": "https://api.openai.example/v1",
            "dimensions": 1536,
        }

    def test_respects_explicit_txtai_model_for_api_mode(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-large",
                "OPENAI_API_KEY": "sk-test",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        # Issue #3980: 3-large is 3072d native, but pgvector hnsw caps at 2000.
        # The default Matryoshka truncation to 1536 lets the daemon come up.
        assert model == "openai/text-embedding-3-large"
        assert vectors == {"api_key": "sk-test", "dimensions": 1536}

    def test_3_small_skips_default_dimensions_when_opted_in_explicitly(self) -> None:
        """text-embedding-3-small is 1536d native — fits hnsw, no truncation
        needed. We only auto-truncate the model that crashes without it
        (3-large). When operators explicitly select 3-small, no dimensions
        kwarg should be set."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-small",
                "OPENAI_API_KEY": "sk-test",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert model == "openai/text-embedding-3-small"
        assert vectors == {"api_key": "sk-test"}

    def test_explicit_dimensions_override(self) -> None:
        """Operators can opt into smaller embeddings (cost) or higher quality
        within the cap via NEXUS_TXTAI_DIMENSIONS."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-large",
                "OPENAI_API_KEY": "sk-test",
                "NEXUS_TXTAI_DIMENSIONS": "1024",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert vectors == {"api_key": "sk-test", "dimensions": 1024}

    def test_dimensions_clamped_to_pgvector_cap(self) -> None:
        """A typo like NEXUS_TXTAI_DIMENSIONS=3072 must NOT take the daemon
        down — clamp to the hnsw cap and warn."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-large",
                "OPENAI_API_KEY": "sk-test",
                "NEXUS_TXTAI_DIMENSIONS": "3072",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert vectors is not None
        assert vectors["dimensions"] == 2000

    def test_dimensions_clamped_to_3_small_native_max(self) -> None:
        """The pgvector cap is not the only limit: 3-small is 1536d native."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-small",
                "OPENAI_API_KEY": "sk-test",
                "NEXUS_TXTAI_DIMENSIONS": "2000",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert model == "openai/text-embedding-3-small"
        assert vectors is not None
        assert vectors["dimensions"] == 1536

    def test_invalid_dimensions_falls_back_to_model_default(self) -> None:
        """A non-int NEXUS_TXTAI_DIMENSIONS is logged and ignored — model
        default still applies."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-large",
                "OPENAI_API_KEY": "sk-test",
                "NEXUS_TXTAI_DIMENSIONS": "not-a-number",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert vectors == {"api_key": "sk-test", "dimensions": 1536}

    def test_zero_dimensions_ignored(self) -> None:
        """Zero/negative dims are nonsensical — fall back to model default."""
        with patch.dict(
            "os.environ",
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-large",
                "OPENAI_API_KEY": "sk-test",
                "NEXUS_TXTAI_DIMENSIONS": "0",
            },
            clear=True,
        ):
            model, vectors = _resolve_txtai_runtime_config()

        assert vectors == {"api_key": "sk-test", "dimensions": 1536}
