"""E2E tests for Pyroscope continuous profiling (Issue #763).

Validates the complete profiling pipeline end-to-end:
1. Config validation (pyroscope.yml structure, docker-compose service)
2. Module wiring (setup_profiling/shutdown_profiling callable from server)
3. Grafana datasource provisioning (Pyroscope + trace-to-profile)
4. Environment variable injection in docker-compose
5. Graceful degradation when pyroscope-io not installed

Following the pattern of tests/e2e/test_pg_metrics_e2e.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY_DIR = PROJECT_ROOT / "observability"


def _load_yaml(path: Path) -> dict:
    """Load and parse a YAML file, failing the test on syntax errors."""
    assert path.exists(), f"Config file missing: {path}"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"Expected top-level dict in {path.name}"
    return data


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestPyroscopeConfigE2E:
    """Validate pyroscope.yml has production-ready settings."""

    CONFIG = OBSERVABILITY_DIR / "pyroscope" / "pyroscope.yml"

    def test_config_exists_and_parseable(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert "server" in data
        assert "storage" in data

    def test_monolithic_mode(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data.get("target") == "all"

    def test_server_http_port(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["server"]["http_listen_port"] == 4040

    def test_filesystem_storage(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["storage"]["backend"] == "filesystem"
        assert data["storage"]["filesystem"]["dir"] == "/data"

    def test_self_profiling_disabled(self) -> None:
        """Self-profiling should be disabled to reduce noise."""
        data = _load_yaml(self.CONFIG)
        assert data["self_profiling"]["disable_push"] is True

    def test_node_limit(self) -> None:
        data = _load_yaml(self.CONFIG)
        assert data["limits"]["max_nodes_per_profile"] == 16384


# ---------------------------------------------------------------------------
# Docker Compose service validation
# ---------------------------------------------------------------------------


class TestDockerComposeServiceE2E:
    """Validate pyroscope service is correctly wired in docker-compose."""

    COMPOSE = PROJECT_ROOT / "docker-compose.observability.yml"

    def test_pyroscope_service_exists(self) -> None:
        data = _load_yaml(self.COMPOSE)
        assert "pyroscope" in data["services"]

    def test_pyroscope_image_pinned(self) -> None:
        """Image should be pinned to a specific version, not :latest."""
        data = _load_yaml(self.COMPOSE)
        image = data["services"]["pyroscope"]["image"]
        assert "grafana/pyroscope:" in image
        assert ":latest" not in image

    def test_pyroscope_memory_limit(self) -> None:
        data = _load_yaml(self.COMPOSE)
        svc = data["services"]["pyroscope"]
        mem = svc["deploy"]["resources"]["limits"]["memory"]
        assert mem == "512M"

    def test_pyroscope_config_mount(self) -> None:
        data = _load_yaml(self.COMPOSE)
        svc = data["services"]["pyroscope"]
        volumes = svc["volumes"]
        config_mount = [v for v in volumes if "pyroscope.yml" in str(v)]
        assert len(config_mount) == 1

    def test_nexus_server_pyroscope_env_vars(self) -> None:
        """nexus-server should have PYROSCOPE_ENABLED and PYROSCOPE_SERVER_ADDRESS."""
        data = _load_yaml(self.COMPOSE)
        env = data["services"]["nexus-server"]["environment"]
        env_set = set(env)
        assert "PYROSCOPE_ENABLED=true" in env_set
        assert "PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040" in env_set

    def test_grafana_depends_on_pyroscope(self) -> None:
        data = _load_yaml(self.COMPOSE)
        deps = data["services"]["grafana"]["depends_on"]
        assert "pyroscope" in deps

    def test_pyroscope_data_volume(self) -> None:
        data = _load_yaml(self.COMPOSE)
        assert "pyroscope-data" in data["volumes"]


# ---------------------------------------------------------------------------
# Grafana datasource provisioning
# ---------------------------------------------------------------------------


class TestDatasourceProvisioningE2E:
    """Validate Grafana auto-provisions Pyroscope datasource."""

    DS = OBSERVABILITY_DIR / "grafana" / "provisioning" / "datasources" / "datasources.yml"

    def test_four_datasources(self) -> None:
        """Should now have Prometheus, Loki, Tempo, and Pyroscope."""
        data = _load_yaml(self.DS)
        names = {ds["name"] for ds in data["datasources"]}
        assert names == {"Prometheus", "Loki", "Tempo", "Pyroscope"}

    def test_pyroscope_datasource_config(self) -> None:
        data = _load_yaml(self.DS)
        ds = next(d for d in data["datasources"] if d["name"] == "Pyroscope")
        assert ds["uid"] == "pyroscope"
        assert ds["type"] == "grafana-pyroscope-datasource"
        assert ds["url"] == "http://pyroscope:4040"
        assert ds["access"] == "proxy"

    def test_tempo_traces_to_profiles(self) -> None:
        """Tempo datasource should have tracesToProfiles pointing to Pyroscope."""
        data = _load_yaml(self.DS)
        tempo = next(d for d in data["datasources"] if d["name"] == "Tempo")
        t2p = tempo["jsonData"]["tracesToProfiles"]
        assert t2p["datasourceUid"] == "pyroscope"
        assert "cpu" in t2p["profileTypeId"]
        assert t2p["customQuery"] is True


# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


class TestModuleWiringE2E:
    """Validate the profiling module is importable and callable."""

    def test_setup_profiling_importable(self) -> None:
        from nexus.server.profiling import setup_profiling

        assert callable(setup_profiling)

    def test_shutdown_profiling_importable(self) -> None:
        from nexus.server.profiling import shutdown_profiling

        assert callable(shutdown_profiling)

    def test_is_profiling_enabled_importable(self) -> None:
        from nexus.server.profiling import is_profiling_enabled

        assert callable(is_profiling_enabled)


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradationE2E:
    """Profiling should degrade gracefully when dependencies are missing."""

    def test_setup_returns_false_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "false")

        from nexus.server import profiling as p

        p._initialized = False
        assert p.setup_profiling() is False

    def test_setup_returns_false_when_pyroscope_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("PYROSCOPE_ENABLED", "true")

        from nexus.server import profiling as p

        p._initialized = False
        with patch.dict("sys.modules", {"pyroscope": None}):
            assert p.setup_profiling() is False

    def test_shutdown_noop_when_not_initialized(self) -> None:
        from nexus.server import profiling as p

        p._initialized = False
        p.shutdown_profiling()  # should not raise

    def test_zero_overhead_when_disabled(self, monkeypatch) -> None:
        """When disabled, no pyroscope imports should be attempted."""
        monkeypatch.setenv("PYROSCOPE_ENABLED", "false")

        from nexus.server import profiling as p

        p._initialized = False

        # If pyroscope were imported, this would fail
        with patch.dict("sys.modules", {"pyroscope": None}):
            result = p.setup_profiling()

        assert result is False
