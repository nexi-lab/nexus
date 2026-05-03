"""txtai backend BM25-only fast-path when model is None (Issue #3997).

When the search lifespan resolver returns ``(None, None)`` because the
operator hasn't opted into either an API embedding model (no key) or a
local model, the txtai backend must skip the heavy
``Embeddings(path="sentence-transformers/...")`` load entirely (~900 MB
RSS) and start in keyword-only BM25 mode.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Inject a fake ``txtai`` module exposing a recording ``Embeddings``.

    The real ``txtai`` package is not always installed in the unit-test env;
    the backend imports it lazily inside ``_startup_impl``. We register a
    stub module so ``from txtai import Embeddings`` resolves to the fake.
    Returns the list of configs each constructed instance was given.
    """
    import sys
    import types

    captured: list[dict[str, Any]] = []

    class _FakeEmbeddings:
        def __init__(self, config: Any = None) -> None:
            if isinstance(config, dict):
                captured.append(dict(config))
            elif config is None:
                captured.append({})
            else:
                captured.append({"_path_form": config})

        def exists(self, *_a: Any, **_kw: Any) -> bool:  # pragma: no cover
            return False

        def load(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
            return None

        def count(self) -> int:  # pragma: no cover
            return 0

        def close(self) -> None:  # pragma: no cover
            return None

    fake_txtai = types.ModuleType("txtai")
    # ``setattr`` instead of attribute assignment avoids mypy's
    # ``[attr-defined]`` complaint on the freshly-minted module type.
    fake_txtai.Embeddings = _FakeEmbeddings
    monkeypatch.setitem(sys.modules, "txtai", fake_txtai)
    return captured


@pytest.mark.asyncio
async def test_bm25_only_path_when_model_is_none(
    fake_embeddings: list[dict[str, Any]],
) -> None:
    """model=None -> single Embeddings({keyword:True, ...}) call, no model path."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    backend = TxtaiBackend(
        database_url=None,
        model=None,
        vectors=None,
        hybrid=True,
        graph=False,
        reranker_model=None,
        sparse=False,
    )
    await backend._startup_impl()

    # Exactly one Embeddings(...) call — no probe, no path-mode build.
    assert len(fake_embeddings) == 1, fake_embeddings
    cfg = fake_embeddings[0]
    assert cfg.get("keyword") is True
    assert "path" not in cfg
    # BM25-only: hybrid is forced off, no reranker task, backend ready.
    assert backend._hybrid is False
    assert backend._started is True


@pytest.mark.asyncio
async def test_model_set_takes_normal_path(
    fake_embeddings: list[dict[str, Any]],
) -> None:
    """model='openai/...' -> Embeddings(config_with_path) called normally."""
    from nexus.bricks.search.txtai_backend import TxtaiBackend

    backend = TxtaiBackend(
        database_url=None,
        model="openai/text-embedding-3-small",
        vectors={"api_key": "sk-x"},
        hybrid=True,
        graph=False,
        reranker_model=None,
        sparse=False,
    )
    await backend._startup_impl()

    # The non-fast-path always builds a config dict that includes ``path``.
    assert any(
        "path" in c and c["path"] == "openai/text-embedding-3-small" for c in fake_embeddings
    )
    assert backend._started is True
