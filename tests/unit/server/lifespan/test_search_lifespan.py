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

        assert model == "openai/text-embedding-3-small"
        assert vectors == {
            "api_key": "sk-test",
            "api_base": "https://api.openai.example/v1",
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

        assert model == "openai/text-embedding-3-large"
        assert vectors == {"api_key": "sk-test"}
