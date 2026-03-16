import asyncio
import importlib.util
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[3] / "scripts" / "init_semantic_search.py"
    spec = importlib.util.spec_from_file_location("init_semantic_search_script", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ainitialize_semantic_search(self, **kwargs):
        self.calls.append(kwargs)


class _FakeNx:
    def __init__(self, search) -> None:
        self._search = search
        self.closed = False

    def service(self, name: str):
        if name == "search":
            return self._search
        return None

    def close(self) -> None:
        self.closed = True


def test_init_semantic_search_uses_configured_connect(monkeypatch, tmp_path) -> None:
    module = _load_module()
    fake_search = _FakeSearch()
    fake_nx = _FakeNx(fake_search)
    captured: dict[str, object] = {}

    def fake_connect(*, config=None):
        captured["config"] = config
        return fake_nx

    config_file = tmp_path / "nexus.yaml"
    config_file.write_text("profile: full\n", encoding="utf-8")

    monkeypatch.setattr(module, "connect", fake_connect)
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://skillhub:skillhub@db:5432/nexus")
    monkeypatch.setenv("NEXUS_DATA_DIR", "/tmp/nexus-data")
    monkeypatch.setenv("NEXUS_CONFIG_FILE", str(config_file))

    assert asyncio.run(module.init_semantic_search()) is True
    assert captured["config"] == str(config_file)
    assert fake_search.calls[0]["nx"] is fake_nx
    assert fake_search.calls[0]["record_store_engine"] is None
    assert fake_search.calls[0]["embedding_provider"] is None
    assert fake_nx.closed is True


def test_init_semantic_search_fails_when_search_service_missing(monkeypatch) -> None:
    module = _load_module()
    fake_nx = _FakeNx(search=None)

    def fake_connect(*, config=None):
        return fake_nx

    monkeypatch.setattr(module, "connect", fake_connect)
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://skillhub:skillhub@db:5432/nexus")
    monkeypatch.delenv("NEXUS_CONFIG_FILE", raising=False)

    assert asyncio.run(module.init_semantic_search()) is False
    assert fake_nx.closed is True


def test_init_semantic_search_inline_config_relies_on_env_database_url(monkeypatch) -> None:
    module = _load_module()
    fake_search = _FakeSearch()
    fake_nx = _FakeNx(fake_search)
    captured: dict[str, object] = {}

    def fake_connect(*, config=None):
        captured["config"] = config
        return fake_nx

    monkeypatch.setattr(module, "connect", fake_connect)
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://skillhub:skillhub@db:5432/nexus")
    monkeypatch.setenv("NEXUS_DATA_DIR", "/tmp/nexus-data")
    monkeypatch.delenv("NEXUS_CONFIG_FILE", raising=False)

    assert asyncio.run(module.init_semantic_search()) is True
    assert captured["config"] == {
        "profile": "full",
        "backend": "local",
        "data_dir": "/tmp/nexus-data",
        "features": {"search": True},
    }
    assert fake_search.calls[0]["nx"] is fake_nx
    assert fake_search.calls[0]["record_store_engine"] is None
    assert fake_nx.closed is True
